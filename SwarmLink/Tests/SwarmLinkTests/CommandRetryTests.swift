import XCTest
@preconcurrency import Network
@testable import SwarmLink

/// A minimal stand-in for a duck-agent's UDP socket: an `NWListener` on an
/// ephemeral port that silently drops the first `ackOnAttempt - 1` copies
/// of a command and then ACKs it, so we can exercise SwarmMaster's real
/// retry-until-ack logic end to end instead of mocking it out.
private actor TestDuckResponder {
    enum ResponderError: Error { case noPort }

    private let ackOnAttempt: Int
    private let queue = DispatchQueue(label: "SwarmLinkTests.TestDuckResponder")
    private var listener: NWListener?
    private var connections: [ObjectIdentifier: NWConnection] = [:]
    private var attemptCount = 0
    private(set) var receivedCmdIDs: [String] = []

    init(ackOnAttempt: Int) {
        self.ackOnAttempt = ackOnAttempt
    }

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
                        continuation.resume(throwing: ResponderError.noPort)
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
        guard case .cmd(let cmd) = SwarmMessage.decode(data) else { return }
        attemptCount += 1
        receivedCmdIDs.append(cmd.cmdID)
        guard attemptCount >= ackOnAttempt else { return } // drop earlier attempts on the floor
        let ack = AckMessage(duck: "duck-01", cmdID: cmd.cmdID, ok: true, error: nil)
        guard let ackData = try? JSONEncoder().encode(ack) else { return }
        connection.send(content: ackData, completion: .contentProcessed { _ in })
    }

    func stop() {
        listener?.cancel()
        for connection in connections.values { connection.cancel() }
        connections.removeAll()
    }
}

final class CommandRetryTests: XCTestCase {
    func testLoadRetriesUntilAckOnThirdAttemptWithIdenticalCmdID() async throws {
        let responder = TestDuckResponder(ackOnAttempt: 3)
        let port = try await responder.start()

        let tmpDir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: tmpDir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: tmpDir) }

        let showJSON = """
        {"format":"duckshow/1",
         "meta":{"name":"retry-test","author":"test","created":"2026-01-01","duration":1.0},
         "requires":{"policies":[]},
         "cast":[{"role":"lead"}],
         "tracks":{"lead":{}}}
        """
        let showURL = tmpDir.appendingPathComponent("retry.duckshow.json")
        try Data(showJSON.utf8).write(to: showURL)

        let roster = [RosterEntry(id: "duck-01", host: "127.0.0.1", port: port, role: "lead")]
        let rosterURL = tmpDir.appendingPathComponent("roster.json")
        try roster.save(to: rosterURL)

        // masterPort: 0 -> let the OS pick an ephemeral local port for this
        // test's single connection, so parallel test runs never collide on
        // the real protocol port (47800) or on each other.
        let master = SwarmMaster(masterPort: 0)
        let outcomes = try await master.load(show: showURL, roster: rosterURL)

        let outcome = try XCTUnwrap(outcomes[DuckID("duck-01")])
        XCTAssertEqual(outcome.status, .ok)

        let attempts = await responder.receivedCmdIDs
        XCTAssertEqual(attempts.count, 3, "expected exactly 3 attempts before the responder ACKed, got \(attempts)")
        XCTAssertEqual(Set(attempts).count, 1, "every retry must reuse the identical cmd_id (dedup-friendly)")

        await responder.stop()
    }

    func testLoadTimesOutWhenResponderNeverAcks() async throws {
        let responder = TestDuckResponder(ackOnAttempt: .max) // never acks
        let port = try await responder.start()

        let tmpDir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: tmpDir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: tmpDir) }

        let showJSON = """
        {"format":"duckshow/1",
         "meta":{"name":"retry-test","author":"test","created":"2026-01-01","duration":1.0},
         "requires":{"policies":[]},
         "cast":[{"role":"lead"}],
         "tracks":{"lead":{}}}
        """
        let showURL = tmpDir.appendingPathComponent("retry.duckshow.json")
        try Data(showJSON.utf8).write(to: showURL)

        let roster = [RosterEntry(id: "duck-01", host: "127.0.0.1", port: port, role: "lead")]
        let rosterURL = tmpDir.appendingPathComponent("roster.json")
        try roster.save(to: rosterURL)

        let master = SwarmMaster(masterPort: 0)
        let outcomes = try await master.load(show: showURL, roster: rosterURL)

        let outcome = try XCTUnwrap(outcomes[DuckID("duck-01")])
        XCTAssertEqual(outcome.status, .timeout)

        let attempts = await responder.receivedCmdIDs
        XCTAssertEqual(attempts.count, SwarmLinkInfo.commandMaxAttempts)

        await responder.stop()
    }
}
