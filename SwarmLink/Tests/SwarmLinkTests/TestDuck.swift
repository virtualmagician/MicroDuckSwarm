import Foundation
@preconcurrency import Network
@testable import SwarmLink

/// A scriptable stand-in for one duck-agent's UDP socket, richer than the
/// responder in CommandRetryTests: it records every command / state tick
/// it receives, can drop the first N copies of a command, NACK chosen
/// commands with an error string, and send telemetry back to the master —
/// so SwarmMaster's transport, watchdog and supersede semantics can be
/// exercised end to end over real sockets.
actor TestDuck {
    enum TestDuckError: Error { case noPort, noPeer }

    let id: DuckID
    /// Per command name: how many copies to silently drop before ACKing.
    var dropFirst: [String: Int] = [:]
    /// Per command name: NACK with this error instead of ACKing ok.
    var nackWith: [String: String] = [:]

    private let queue: DispatchQueue
    private var listener: NWListener?
    private var connections: [ObjectIdentifier: NWConnection] = [:]
    private var peer: NWConnection?
    private var attempts: [String: Int] = [:]

    private(set) var received: [CommandMessage] = []
    private(set) var receivedStates: [StateMessage] = []
    private(set) var receivedTimeResponses: [TimeResponse] = []

    init(id: DuckID = "duck-01") {
        self.id = id
        self.queue = DispatchQueue(label: "SwarmLinkTests.TestDuck.\(id.raw)")
    }

    func setDropFirst(_ count: Int, of command: String) { dropFirst[command] = count }
    func setNack(_ command: String, error: String) { nackWith[command] = error }

    func start() async throws -> UInt16 {
        let params = NWParameters.udp
        params.allowLocalEndpointReuse = true
        let listener = try NWListener(using: params, on: .any)
        self.listener = listener

        listener.newConnectionHandler = { [weak self] connection in
            guard let self else { return }
            Task { await self.accept(connection) }
        }

        return try await withCheckedThrowingContinuation { continuation in
            listener.stateUpdateHandler = { state in
                switch state {
                case .ready:
                    if let port = listener.port?.rawValue {
                        continuation.resume(returning: port)
                    } else {
                        continuation.resume(throwing: TestDuckError.noPort)
                    }
                case .failed(let error):
                    continuation.resume(throwing: error)
                default:
                    break
                }
            }
            listener.start(queue: queue)
        }
    }

    func stop() {
        listener?.cancel()
        for connection in connections.values { connection.cancel() }
        connections.removeAll()
        peer = nil
    }

    // MARK: Observations

    func commands(named name: String) -> [CommandMessage] {
        received.filter { $0.payload.cmdName == name }
    }

    /// Polls until at least `count` commands named `name` have arrived.
    @discardableResult
    func waitForCommands(named name: String, count: Int = 1, timeoutMs: Int = 1500) async -> [CommandMessage] {
        let deadline = Date().addingTimeInterval(Double(timeoutMs) / 1000)
        while Date() < deadline {
            let matching = commands(named: name)
            if matching.count >= count { return matching }
            try? await Task.sleep(nanoseconds: 10_000_000)
        }
        return commands(named: name)
    }

    /// Polls until at least `count` state ticks have arrived.
    @discardableResult
    func waitForStates(count: Int = 1, timeoutMs: Int = 1500) async -> [StateMessage] {
        let deadline = Date().addingTimeInterval(Double(timeoutMs) / 1000)
        while Date() < deadline, receivedStates.count < count {
            try? await Task.sleep(nanoseconds: 10_000_000)
        }
        return receivedStates
    }

    // MARK: Agent → master

    func sendTelemetry(
        state: AgentState, show: String? = nil, showTime: Double = 0, lastError: String? = nil
    ) throws {
        let message = TelemetryMessage(
            duck: id, seq: received.count, state: state, show: show, showTime: showTime,
            clockOffsetMs: 0.5, clockRttMs: 2.0, policiesOk: true, lastError: lastError
        )
        try send(.telemetry(message))
    }

    func sendTimeRequest(t0: Int64) throws {
        try send(.timeRequest(TimeRequest(duck: id, t0: t0)))
    }

    private func send(_ envelope: Envelope) throws {
        guard let peer else { throw TestDuckError.noPeer }
        let data = try SwarmMessage.encode(envelope)
        peer.send(content: data, completion: .contentProcessed { _ in })
    }

    // MARK: Socket plumbing

    private func accept(_ connection: NWConnection) {
        connections[ObjectIdentifier(connection)] = connection
        connection.start(queue: queue)
        receive(on: connection)
    }

    private func receive(on connection: NWConnection) {
        connection.receiveMessage { [weak self] data, _, _, error in
            guard let self else { return }
            Task {
                if let data, !data.isEmpty {
                    await self.handle(data, connection: connection)
                }
                if error == nil {
                    await self.receive(on: connection)
                }
            }
        }
    }

    private func handle(_ data: Data, connection: NWConnection) {
        peer = connection
        switch SwarmMessage.decode(data) {
        case .cmd(let cmd):
            received.append(cmd)
            let name = cmd.payload.cmdName
            let attempt = (attempts[cmd.cmdID] ?? 0) + 1
            attempts[cmd.cmdID] = attempt
            if attempt <= (dropFirst[name] ?? 0) { return }
            let ack: AckMessage
            if let error = nackWith[name] {
                ack = AckMessage(duck: id, cmdID: cmd.cmdID, ok: false, error: error)
            } else {
                ack = AckMessage(duck: id, cmdID: cmd.cmdID, ok: true, error: nil)
            }
            guard let ackData = try? JSONEncoder().encode(ack) else { return }
            connection.send(content: ackData, completion: .contentProcessed { _ in })
        case .state(let state):
            receivedStates.append(state)
        case .timeResponse(let response):
            receivedTimeResponses.append(response)
        default:
            break
        }
    }
}

// MARK: - Fixtures

enum Fixtures {
    /// A minimal, valid show with the given roles; written to a fresh temp
    /// dir (removed by the returned cleanup closure).
    static func writeShow(
        named name: String = "fixture", roles: [String] = ["lead"], duration: Double = 1.0
    ) throws -> (dir: URL, show: URL) {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let cast = roles.map { "{\"role\":\"\($0)\"}" }.joined(separator: ",")
        let tracks = roles.map { "\"\($0)\":{}" }.joined(separator: ",")
        let json = """
        {"format":"duckshow/1",
         "meta":{"name":"\(name)","author":"test","created":"2026-01-01","duration":\(duration)},
         "requires":{"policies":[]},
         "cast":[\(cast)],
         "tracks":{\(tracks)}}
        """
        let show = dir.appendingPathComponent("\(name).duckshow.json")
        try Data(json.utf8).write(to: show)
        return (dir, show)
    }

    static func writeRoster(_ entries: [RosterEntry], in dir: URL) throws -> URL {
        let url = dir.appendingPathComponent("roster-\(UUID().uuidString).json")
        try entries.save(to: url)
        return url
    }

    /// A random high local port for tests that need the production
    /// "every connection pinned to one fixed master port" configuration,
    /// away from the protocol port (47800) and the e2e demo's ports.
    static func randomMasterPort() -> UInt16 {
        UInt16.random(in: 20000...39999)
    }
}

extension Task where Success == Never, Failure == Never {
    static func sleepMs(_ ms: Int) async {
        try? await Task.sleep(nanoseconds: UInt64(ms) * 1_000_000)
    }
}
