// SwarmMaster.swift
//
// The master-side engine for docs/swarmlink-protocol.md: one UDP endpoint
// per roster duck, time-sync replies, 5 Hz state broadcast while
// armed/playing, idempotent-by-cmd_id command fan-out with retry, and
// telemetry ingestion with a 5 s "lost" watchdog.
//
// Designed to be the engine behind a future StageWizard `RobotSwarmPlayer:
// MediaPlayback` (docs/architecture.md): `load` does all the heavy lifting
// (parse show, verify roster, open sockets, pre-arm every duck) so that
// `play`/`stop`/`panic` are cheap, fire-and-forget-safe calls a transport
// button can call directly.

import Foundation
@preconcurrency import Network

// MARK: - Public result / telemetry types

public struct LoadOutcome: Sendable, Equatable {
    public enum Status: Sendable, Equatable {
        case ok
        case nacked(String)
        case timeout
        case connectionFailed(String)
    }

    public var status: Status

    public var isOK: Bool { status == .ok }
}

public struct DuckTelemetry: Sendable, Equatable {
    public var duck: DuckID
    public var state: AgentState
    public var show: String?
    public var showTime: Double
    public var clockOffsetMs: Double
    public var clockRttMs: Double
    public var policiesOk: Bool
    public var batteryPct: Double?
    public var rssiDbm: Double?
    public var lastError: String?
    /// Master-monotonic ns at which this snapshot was last refreshed by an
    /// actual telemetry datagram.
    public var lastSeenMasterNs: Int64
    /// True once 5 s have elapsed with no telemetry from this duck
    /// (docs/swarmlink-protocol.md §4: "Master marks a duck lost after 5 s
    /// without telemetry").
    public var lost: Bool
}

public enum TelemetryEvent: Sendable, Equatable {
    case updated(DuckTelemetry)
    case lost(DuckID)
}

/// Errors surfaced synchronously from `load` (i.e. before any per-duck fan
/// out — malformed inputs, not per-duck network failures, which show up as
/// `LoadOutcome.Status` instead).
public enum SwarmMasterError: Error, Sendable, Equatable, CustomStringConvertible {
    case notLoaded

    public var description: String {
        switch self {
        case .notLoaded: return "no show is loaded"
        }
    }
}

// MARK: - SwarmMaster

/// The master engine. One instance owns one UDP "socket set" (one
/// `NWConnection` per roster duck, all bound to the same local master
/// port) for the lifetime of a show.
///
/// Every per-duck connection is dialed with `requiredLocalEndpoint` pinned
/// to `masterPort` and `allowLocalEndpointReuse`, so all of them share the
/// fixed local port docs/swarmlink-protocol.md requires ("master listens on
/// UDP 47800") for both outbound (state/cmd/time_resp) and inbound
/// (time_req/ack/telemetry) traffic — a plain `NWListener` was considered
/// but the full roster is always known up front here, so dialing every duck
/// directly at `load` time (pre-arming, per the API contract) is simpler
/// than demuxing listener-side flows.
public actor SwarmMaster {
    private let masterPort: UInt16
    private let networkQueue = DispatchQueue(label: "SwarmLink.SwarmMaster.network")

    private var connections: [DuckID: NWConnection] = [:]
    private var roster: [DuckID: RosterEntry] = [:]

    private var show: Show?
    private var showID: String = ""
    private var showSHA256: String = ""

    private var clock = MasterClock()
    private var transport: Transport = .stopped
    private var stateSeq: Int = 0
    /// Show-time to resume from on the next `play()`; set by `seek()` while
    /// not playing, consumed (and reset to 0) by the next `play()`.
    private var cueShowTime: Double = 0.0

    private var telemetryStore: [DuckID: DuckTelemetry] = [:]
    private var lastTelemetryNs: [DuckID: Int64] = [:]
    private var telemetryContinuations: [UUID: AsyncStream<TelemetryEvent>.Continuation] = [:]

    private var pendingAcks: [String: (AckMessage?) -> Void] = [:]

    private var stateLoopTask: Task<Void, Never>?
    private var housekeepingTask: Task<Void, Never>?

    public init(masterPort: UInt16 = SwarmLinkInfo.defaultMasterPort) {
        self.masterPort = masterPort
    }

    deinit {
        stateLoopTask?.cancel()
        housekeepingTask?.cancel()
        for continuation in telemetryContinuations.values { continuation.finish() }
        for connection in connections.values { connection.cancel() }
    }

    // MARK: Public API

    /// Parses `showURL`, loads the roster from `rosterURL`, opens one UDP
    /// connection per duck (pinned to `masterPort`), and fans out `load`
    /// commands — retried up to `SwarmLinkInfo.commandMaxAttempts` times at
    /// `SwarmLinkInfo.commandRetryIntervalMs` — waiting for every duck's
    /// ACK/NACK/timeout. This is the "heavy" call: by the time it returns,
    /// every reachable duck is pre-armed and `play()` only has to send one
    /// more small unicast per duck.
    public func load(show showURL: URL, roster rosterURL: URL) async throws -> [DuckID: LoadOutcome] {
        let decodedShow = try Show.load(contentsOf: showURL)
        let sha = try Show.sha256(of: showURL)
        let entries = try [RosterEntry].load(contentsOf: rosterURL)

        teardownConnections()

        self.show = decodedShow
        self.showID = showURL.deletingPathExtension().deletingPathExtension().lastPathComponent
        self.showSHA256 = sha
        self.roster = entries.indexedByID()
        self.transport = .stopped
        self.stateSeq = 0
        self.cueShowTime = 0
        clock.stop()

        let castRoles = Set(decodedShow.cast.map(\.role))

        for entry in entries {
            let connection = makeConnection(for: entry)
            connections[entry.id] = connection
            lastTelemetryNs[entry.id] = nil
            startReceiving(duckID: entry.id, connection: connection)
        }
        startHousekeepingIfNeeded()

        let cmdID = UUID().uuidString
        let showIDForLoad = self.showID
        var results: [DuckID: LoadOutcome] = [:]
        await withTaskGroup(of: (DuckID, LoadOutcome).self) { group in
            for entry in entries {
                group.addTask {
                    if !castRoles.contains(entry.role) {
                        return (entry.id, LoadOutcome(status: .connectionFailed(
                            "role '\(entry.role)' is not in the show's cast")))
                    }
                    let message = CommandMessage(
                        cmdID: cmdID,
                        payload: .load(show: showIDForLoad, sha256: sha, role: entry.role)
                    )
                    let status = await self.sendWithRetry(message, to: entry.id)
                    return (entry.id, LoadOutcome(status: status))
                }
            }
            for await (id, outcome) in group {
                results[id] = outcome
            }
        }
        return results
    }

    /// Schedules playback to start `leadTimeNs` in the future (giving the
    /// `play` command time to be delivered/retried before it's due), sends
    /// `play` to every duck, and returns immediately — it does not await
    /// ACKs. Resumes from the show-time set by a prior `seek()`, if any,
    /// else from 0.
    public func play(at leadTimeNs: Int64 = 300_000_000) {
        guard show != nil else { return }
        let now = MasterClock.nowNanoseconds()
        let atMasterTime = now + max(0, leadTimeNs)
        let fromShowTime = cueShowTime
        cueShowTime = 0

        transport = .armed
        startStateLoopIfNeeded()

        fanOutFireAndForget(payload: .play(show: showID, atMasterTime: atMasterTime, fromShowTime: fromShowTime))

        let delayNs = atMasterTime - now
        Task { [weak self] in
            if delayNs > 0 {
                try? await Task.sleep(nanoseconds: UInt64(delayNs))
            }
            await self?.beginPlaying(atMasterTime: atMasterTime, fromShowTime: fromShowTime)
        }
    }

    /// Immediately re-anchors the local and every duck's show clock to
    /// `showTime`. If not currently playing, the position is remembered and
    /// used by the next `play()` instead.
    public func seek(to showTime: Double) {
        let now = MasterClock.nowNanoseconds()
        cueShowTime = showTime
        if transport != .stopped {
            clock.seek(to: showTime, atMasterTimeNs: now)
        }
        fanOutFireAndForget(payload: .seek(showTime: showTime, atMasterTime: now))
    }

    /// Graceful stop: fans out `stop` and returns immediately
    /// (fire-and-forget safe — never throws, never blocks on ACKs).
    public func stop() {
        transport = .stopped
        clock.stop()
        fanOutFireAndForget(payload: .stop)
    }

    /// Highest-priority stop, valid from any state, never NACKed by agents.
    /// Fire-and-forget, same as `stop()`.
    public func panic() {
        transport = .stopped
        clock.stop()
        fanOutFireAndForget(payload: .panic)
    }

    /// Snapshot of the last known telemetry per duck.
    public var telemetry: [DuckID: DuckTelemetry] {
        telemetryStore
    }

    /// A live feed of telemetry updates and lost-duck transitions. Each
    /// call returns an independent stream; finishing/cancelling it
    /// unregisters cleanly.
    public func telemetryEvents() -> AsyncStream<TelemetryEvent> {
        let id = UUID()
        return AsyncStream { continuation in
            telemetryContinuations[id] = continuation
            continuation.onTermination = { [weak self] _ in
                Task { await self?.removeTelemetryContinuation(id) }
            }
        }
    }

    /// Current transport state, mirrored locally from master-side intent
    /// (not agent-confirmed — see per-duck `telemetry` for that).
    public var currentTransport: Transport {
        transport
    }

    /// Master's own show-time estimate right now, if playing/armed.
    public func currentShowTime() -> Double? {
        clock.showTime()
    }

    // MARK: Playback epoch

    private func beginPlaying(atMasterTime: Int64, fromShowTime: Double) {
        guard transport == .armed else { return } // stopped/panicked/seeked away before start
        clock.play(at: atMasterTime, fromShowTime: fromShowTime)
        transport = .playing
    }

    // MARK: Connection lifecycle

    private func makeConnection(for entry: RosterEntry) -> NWConnection {
        let params = NWParameters.udp
        params.allowLocalEndpointReuse = true
        params.requiredLocalEndpoint = NWEndpoint.hostPort(
            host: "0.0.0.0",
            port: NWEndpoint.Port(rawValue: masterPort) ?? NWEndpoint.Port(rawValue: SwarmLinkInfo.defaultMasterPort)!
        )
        let host = NWEndpoint.Host(entry.host)
        let port = NWEndpoint.Port(rawValue: entry.port)
            ?? NWEndpoint.Port(rawValue: SwarmLinkInfo.defaultAgentPort)!
        let connection = NWConnection(host: host, port: port, using: params)
        connection.start(queue: networkQueue)
        return connection
    }

    private func teardownConnections() {
        stateLoopTask?.cancel()
        stateLoopTask = nil
        for connection in connections.values { connection.cancel() }
        connections.removeAll()
    }

    private func startReceiving(duckID: DuckID, connection: NWConnection) {
        connection.receiveMessage { [weak self] data, _, _, error in
            guard let self else { return }
            Task { await self.onReceive(duckID: duckID, connection: connection, data: data, error: error) }
        }
    }

    private func onReceive(duckID: DuckID, connection: NWConnection, data: Data?, error: NWError?) {
        // Only keep pumping this connection's receive loop while it's still
        // the one on file for this duck (load() may have torn it down).
        guard connections[duckID] === connection else { return }
        if let data, !data.isEmpty {
            handleDatagram(data, from: duckID)
        }
        if error == nil {
            startReceiving(duckID: duckID, connection: connection)
        }
        // On error: stop pumping this connection. The 5 s telemetry
        // watchdog will surface the duck as lost; a future load()/retry
        // will redial.
    }

    private func send(_ data: Data, on connection: NWConnection) {
        connection.send(content: data, completion: .contentProcessed { _ in })
    }

    // MARK: Inbound message handling

    private func handleDatagram(_ data: Data, from duckID: DuckID) {
        guard let envelope = SwarmMessage.decode(data) else { return }
        switch envelope {
        case .timeRequest(let request):
            respond(to: request, duckID: duckID)
        case .ack(let ack):
            resolveAck(ack, key: ackKey(cmdID: ack.cmdID, duck: duckID))
        case .telemetry(let telemetry):
            ingest(telemetry, duckID: duckID)
        case .timeResponse, .state, .cmd:
            break // master never receives these; ignore per "unknown/unexpected is dropped"
        }
    }

    private func respond(to request: TimeRequest, duckID: DuckID) {
        guard let connection = connections[duckID] else { return }
        let t1 = MasterClock.nowNanoseconds()
        let response = TimeResponse(t0: request.t0, t1: t1, t2: MasterClock.nowNanoseconds())
        guard let data = try? JSONEncoder().encode(response) else { return }
        send(data, on: connection)
    }

    private func ingest(_ message: TelemetryMessage, duckID: DuckID) {
        let now = MasterClock.nowNanoseconds()
        lastTelemetryNs[duckID] = now
        let entry = DuckTelemetry(
            duck: duckID,
            state: message.state,
            show: message.show,
            showTime: message.showTime,
            clockOffsetMs: message.clockOffsetMs,
            clockRttMs: message.clockRttMs,
            policiesOk: message.policiesOk,
            batteryPct: message.batteryPct,
            rssiDbm: message.rssiDbm,
            lastError: message.lastError,
            lastSeenMasterNs: now,
            lost: false
        )
        telemetryStore[duckID] = entry
        publish(.updated(entry))
    }

    // MARK: Command fan-out

    private func ackKey(cmdID: String, duck: DuckID) -> String { "\(cmdID)|\(duck.raw)" }

    private func fanOutFireAndForget(payload: CommandMessage.Payload) {
        let message = CommandMessage(cmdID: UUID().uuidString, payload: payload)
        for duckID in connections.keys {
            Task { [weak self] in
                _ = await self?.sendWithRetry(message, to: duckID)
            }
        }
    }

    /// Sends `message` to `duckID`, retrying up to
    /// `SwarmLinkInfo.commandMaxAttempts` times at
    /// `SwarmLinkInfo.commandRetryIntervalMs` until an ACK/NACK arrives.
    /// The same `cmd_id` is reused across every attempt, matching the
    /// protocol's "agents deduplicate by cmd_id" contract.
    private func sendWithRetry(_ message: CommandMessage, to duckID: DuckID) async -> LoadOutcome.Status {
        guard let connection = connections[duckID] else {
            return .connectionFailed("no connection for duck \(duckID)")
        }
        guard let data = try? JSONEncoder().encode(message) else {
            return .connectionFailed("failed to encode command")
        }
        let key = ackKey(cmdID: message.cmdID, duck: duckID)
        for attempt in 1...SwarmLinkInfo.commandMaxAttempts {
            send(data, on: connection)
            if let ack = await waitForAck(key: key, timeoutMs: SwarmLinkInfo.commandRetryIntervalMs) {
                return ack.ok ? .ok : .nacked(ack.error ?? "nack")
            }
            if attempt == SwarmLinkInfo.commandMaxAttempts {
                return .timeout
            }
        }
        return .timeout
    }

    private func waitForAck(key: String, timeoutMs: UInt64) async -> AckMessage? {
        await withCheckedContinuation { (continuation: CheckedContinuation<AckMessage?, Never>) in
            pendingAcks[key] = { ack in continuation.resume(returning: ack) }
            Task { [weak self] in
                try? await Task.sleep(nanoseconds: timeoutMs * 1_000_000)
                await self?.resolveTimeout(key: key)
            }
        }
    }

    private func resolveAck(_ ack: AckMessage, key: String) {
        if let handler = pendingAcks.removeValue(forKey: key) {
            handler(ack)
        }
    }

    private func resolveTimeout(key: String) {
        if let handler = pendingAcks.removeValue(forKey: key) {
            handler(nil)
        }
    }

    // MARK: State broadcast (5 Hz while armed/playing)

    private func startStateLoopIfNeeded() {
        guard stateLoopTask == nil else { return }
        stateLoopTask = Task { [weak self] in
            while true {
                guard let self else { return }
                let shouldContinue = await self.publishStateTick()
                if !shouldContinue { break }
                let intervalNs = UInt64(1_000_000_000.0 / SwarmLinkInfo.stateBroadcastHz)
                try? await Task.sleep(nanoseconds: intervalNs)
            }
            await self?.finishStateLoop()
        }
    }

    private func finishStateLoop() {
        stateLoopTask = nil
    }

    /// Sends one `state` tick to every duck; returns whether the loop
    /// should keep running (only while armed/playing, per the API contract).
    private func publishStateTick() -> Bool {
        guard transport == .armed || transport == .playing else { return false }
        stateSeq += 1
        let message = StateMessage(
            seq: stateSeq,
            show: showID,
            transport: transport,
            showTime: clock.showTime() ?? 0,
            masterTime: MasterClock.nowNanoseconds()
        )
        guard let data = try? JSONEncoder().encode(message) else { return true }
        for connection in connections.values { send(data, on: connection) }
        return true
    }

    // MARK: Housekeeping (5 s lost watchdog)

    private func startHousekeepingIfNeeded() {
        guard housekeepingTask == nil else { return }
        housekeepingTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 1_000_000_000)
                guard let self else { return }
                await self.sweepLostDucks()
            }
        }
    }

    private func sweepLostDucks() {
        let now = MasterClock.nowNanoseconds()
        let thresholdNs = Int64(SwarmLinkInfo.telemetryLostThresholdSeconds * 1_000_000_000)
        for (duckID, lastSeen) in lastTelemetryNs {
            guard now - lastSeen > thresholdNs else { continue }
            guard var entry = telemetryStore[duckID], !entry.lost else { continue }
            entry.lost = true
            telemetryStore[duckID] = entry
            publish(.lost(duckID))
        }
    }

    // MARK: Telemetry fan-out

    private func publish(_ event: TelemetryEvent) {
        for continuation in telemetryContinuations.values {
            continuation.yield(event)
        }
    }

    private func removeTelemetryContinuation(_ id: UUID) {
        telemetryContinuations.removeValue(forKey: id)
    }
}
