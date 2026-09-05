// OSCFacade.swift
//
// docs/osc-facade.md: exposes one `SwarmMaster` over OSC 1.0 / UDP so any
// rig (QLab, TouchDesigner, a lighting desk, StageWizard's OSC network
// cues) can load, arm and fire the flock without linking this package.
// Conventions mirror the StageWizard ↔ StageWand contract: `/duckswarm/…`
// address prefix, ping-renewed subscriptions with a 5 s TTL, pushed status
// feedback, Bonjour advertising.
//
// The facade owns only the wire mechanics — listener, per-flow
// connections, subscriber registry, status cadence, ack/error replies.
// Every command's *semantics* are the master's (repeated, ACKed,
// idempotent unicast per duck); the facade adds no queueing of its own.
//
// Concurrency: an actor. Network.framework delivers listener/connection
// callbacks on a private background queue; datagrams are decoded there
// (`OSCCodec.decodePacket` is pure and nonisolated) and only the resulting
// value-type `OSCMessage`s cross into the actor. Each inbound command
// runs in its own Task so a slow `load` (up to 5 × 100 ms of retries per
// duck, and its file reads, which the master keeps off its actor) never
// delays a `panic` arriving behind it — panic always works, from any
// state.
//
// Flows: UDP has no close, so the listener keeps one flow per source
// port. Flows are deliberately NOT hung up on for being idle: after the
// facade cancels an inbound UDP flow, Network.framework does not reliably
// spawn a fresh one for the next datagram from the same source port
// (observed on CI), and a real rig such as QLab talks from one fixed port
// with long gaps — its next `/duckswarm/go` must never be the datagram
// that gets black-holed. The table is bounded instead: at the 64-flow cap
// the least-recently-heard non-subscriber is evicted for the newcomer, so
// a sender is never refused and a `/duckswarm/panic` from a socket the
// facade has never seen is always heard. Rigs that want their flow
// protected from eviction simply ping (subscribers are evicted last).

import Foundation
@preconcurrency import Network

// MARK: - Configuration / errors

public struct OSCFacadeConfiguration: Sendable {
    /// Roster the master dials at `start()` and re-reads on every `load`.
    public var rosterURL: URL
    /// Directory show ids resolve in: `<dir>/<id>.duckshow.json`, then
    /// `<dir>/<id>/<id>.duckshow.json` — exactly like the duck-agent.
    public var showsDirectory: URL
    /// UDP port the OSC listener binds (0 = ephemeral, for tests).
    public var oscPort: UInt16
    /// The SwarmLink master port, advertised in the Bonjour TXT record
    /// (`master=<port>`). The master itself is configured by its owner.
    public var masterPort: UInt16
    /// Advertise `_duckswarm._udp` on the OSC port (TXT `v=1`, `master=…`).
    public var advertiseBonjour: Bool
    /// Lead time for `/duckswarm/play` without an argument and for
    /// `/duckswarm/go`.
    public var defaultLeadSeconds: Double
    /// How long a `/duckswarm/ping` keeps its sender subscribed — and how
    /// long a silent inbound flow is kept before it is hung up on.
    public var subscriberTTLSeconds: Double
    /// Status push rate while armed/playing.
    public var activeStatusHz: Double
    /// Status push rate while stopped.
    public var idleStatusHz: Double

    public init(
        rosterURL: URL,
        showsDirectory: URL,
        oscPort: UInt16 = OSCFacade.defaultOSCPort,
        masterPort: UInt16 = SwarmLinkInfo.defaultMasterPort,
        advertiseBonjour: Bool = true,
        defaultLeadSeconds: Double = 1.5,
        subscriberTTLSeconds: Double = 5.0,
        activeStatusHz: Double = 2.0,
        idleStatusHz: Double = 0.5
    ) {
        self.rosterURL = rosterURL
        self.showsDirectory = showsDirectory
        self.oscPort = oscPort
        self.masterPort = masterPort
        self.advertiseBonjour = advertiseBonjour
        self.defaultLeadSeconds = defaultLeadSeconds
        self.subscriberTTLSeconds = subscriberTTLSeconds
        self.activeStatusHz = activeStatusHz
        self.idleStatusHz = idleStatusHz
    }
}

public enum OSCFacadeError: Error, Sendable, Equatable, CustomStringConvertible {
    case alreadyStarted
    case invalidPort(UInt16)
    case bindFailed(port: UInt16, reason: String)
    case startTimedOut(port: UInt16)

    public var description: String {
        switch self {
        case .alreadyStarted: return "OSC facade is already started"
        case .invalidPort(let port): return "invalid OSC port \(port)"
        case .bindFailed(let port, let reason): return "could not bind OSC port udp/\(port): \(reason)"
        case .startTimedOut(let port): return "OSC listener on udp/\(port) did not become ready"
        }
    }
}

/// Where the facade's one-line log messages go (nil = silent).
public typealias OSCFacadeLog = @Sendable (String) -> Void

// MARK: - Subscriber registry

/// Ping-renewed feedback subscribers: any endpoint that pinged within the
/// last `ttl` seconds is live. A pure value type with an injectable clock
/// (unit-testable without sockets), the same shape as StageWizard's
/// `OSCSubscriberRegistry`; `OSCFacade` holds one keyed by `NWEndpoint`.
public struct OSCSubscriberRegistry<Endpoint: Hashable> {
    public let ttl: Double
    private var lastHeard: [Endpoint: Double] = [:]
    private let now: @Sendable () -> Double

    /// - Parameters:
    ///   - ttl: seconds a ping keeps its sender subscribed.
    ///   - now: monotonic seconds; tests supply their own.
    public init(ttl: Double, now: @escaping @Sendable () -> Double = { Double(MasterClock.nowNanoseconds()) / 1e9 }) {
        self.ttl = ttl
        self.now = now
    }

    /// Records a ping from `endpoint`. Returns `true` when this is a *new*
    /// subscriber (first contact, or a return after expiring) — the cue for
    /// an immediate full status push to that endpoint alone.
    @discardableResult
    public mutating func touch(_ endpoint: Endpoint) -> Bool {
        let t = now()
        prune(at: t)
        let isNew = lastHeard[endpoint] == nil
        lastHeard[endpoint] = t
        return isNew
    }

    /// Endpoints heard from within the last `ttl` seconds, pruning first.
    public mutating func liveEndpoints() -> Set<Endpoint> {
        prune(at: now())
        return Set(lastHeard.keys)
    }

    private mutating func prune(at t: Double) {
        lastHeard = lastHeard.filter { t - $0.value <= ttl }
    }
}

extension OSCSubscriberRegistry: Sendable where Endpoint: Sendable {}

// MARK: - Facade

public actor OSCFacade {
    /// docs/osc-facade.md: OSC listens on UDP 53300 (StageWizard uses
    /// 53100/53200; the family stays adjacent).
    public static let defaultOSCPort: UInt16 = 53300
    public static let addressPrefix = "/duckswarm"
    public static let bonjourServiceType = "_duckswarm._udp"
    /// Cap on concurrently tracked UDP flows — hostile-input hardening.
    /// Never a refusal: at the cap the least-recently-heard flow is evicted
    /// for the newcomer (see `accept`).
    public static let maxTrackedFlows = 64
    /// How long `start()` waits for the listener to report `.ready`.
    private static let startTimeoutNs: UInt64 = 5_000_000_000

    /// One inbound UDP flow (one sender endpoint) and when it last spoke.
    private struct Flow {
        let connection: NWConnection
        var lastHeardNs: Int64
    }

    private let master: SwarmMaster
    private let configuration: OSCFacadeConfiguration
    private let log: OSCFacadeLog?
    private let queue = DispatchQueue(label: "SwarmLink.OSCFacade.network")

    private var listener: NWListener?
    private var flows: [ObjectIdentifier: Flow] = [:]
    private var subscribers: OSCSubscriberRegistry<NWEndpoint>
    private var startContinuation: CheckedContinuation<UInt16, any Error>?
    /// Bumped by every `start()`; the start timeout only fails the attempt
    /// it was armed for, never a later restart.
    private var startAttempt = 0
    private var statusLoopTask: Task<Void, Never>?
    private var transportWatchTask: Task<Void, Never>?
    private var failureContinuations: [UUID: AsyncStream<OSCFacadeError>.Continuation] = [:]
    /// See `quiesce()`.
    private var isQuiescing = false

    /// The UDP port the listener is bound to while running.
    public private(set) var boundPort: UInt16?

    /// - Parameters:
    ///   - master: the engine every command is dispatched to. The facade
    ///     dials `configuration.rosterURL` on it at `start()`, so `panic`,
    ///     `stop`, `seek` and telemetry work before any show is loaded.
    ///   - log: one-line operator log sink (unknown addresses, commands,
    ///     subscriber churn, flow eviction); nil for `--quiet`.
    public init(master: SwarmMaster, configuration: OSCFacadeConfiguration, log: OSCFacadeLog? = nil) {
        self.master = master
        self.configuration = configuration
        self.log = log
        self.subscribers = OSCSubscriberRegistry(ttl: configuration.subscriberTTLSeconds)
    }

    deinit {
        statusLoopTask?.cancel()
        transportWatchTask?.cancel()
        for flow in flows.values { flow.connection.cancel() }
        listener?.cancel()
        for continuation in failureContinuations.values { continuation.finish() }
    }

    // MARK: Lifecycle

    /// Dials the roster on the master, binds the OSC listener and starts
    /// the feedback loops. Returns the bound port. Throws
    /// `OSCFacadeError.bindFailed` when the port is taken, or whatever
    /// reading the roster threw. A failed start leaves nothing behind: the
    /// next `start()` binds afresh.
    @discardableResult
    public func start() async throws -> UInt16 {
        guard listener == nil else { throw OSCFacadeError.alreadyStarted }
        try await master.connect(roster: configuration.rosterURL)

        let params = NWParameters.udp
        let listener: NWListener
        do {
            if configuration.oscPort == 0 {
                listener = try NWListener(using: params, on: .any)
            } else {
                guard let port = NWEndpoint.Port(rawValue: configuration.oscPort) else {
                    throw OSCFacadeError.invalidPort(configuration.oscPort)
                }
                listener = try NWListener(using: params, on: port)
            }
        } catch let error as OSCFacadeError {
            throw error
        } catch {
            throw OSCFacadeError.bindFailed(port: configuration.oscPort, reason: "\(error)")
        }

        if configuration.advertiseBonjour {
            let txt = NWTXTRecord(["v": "1", "master": String(configuration.masterPort)])
            listener.service = NWListener.Service(name: nil, type: Self.bonjourServiceType, domain: nil, txtRecord: txt)
        }
        listener.stateUpdateHandler = { [weak self] state in
            guard let self else { return }
            Task { await self.handleListenerState(state, listener: listener) }
        }
        listener.newConnectionHandler = { [weak self] connection in
            guard let self else { return }
            Task { await self.accept(connection) }
        }
        self.listener = listener
        isQuiescing = false
        startAttempt += 1
        let attempt = startAttempt

        let requestedPort = configuration.oscPort
        let port: UInt16 = try await withCheckedThrowingContinuation { continuation in
            startContinuation = continuation
            listener.start(queue: queue)
            Task { [weak self] in
                try? await Task.sleep(nanoseconds: Self.startTimeoutNs)
                await self?.failStartIfStillPending(attempt: attempt, port: requestedPort)
            }
        }
        boundPort = port
        startTransportWatch()
        restartStatusLoop()
        log?("osc: listening on udp/\(port)" + (configuration.advertiseBonjour ? " (bonjour \(Self.bonjourServiceType))" : ""))
        return port
    }

    /// Closes the listener and every flow, drops all subscribers and stops
    /// the feedback loops. The master is left untouched (its transport is
    /// the owner's business — see `swarmctl serve`'s shutdown).
    public func stop() {
        statusLoopTask?.cancel()
        statusLoopTask = nil
        transportWatchTask?.cancel()
        transportWatchTask = nil
        for flow in flows.values { flow.connection.cancel() }
        flows.removeAll()
        subscribers = OSCSubscriberRegistry(ttl: configuration.subscriberTTLSeconds)
        listener?.stateUpdateHandler = nil
        listener?.newConnectionHandler = nil
        listener?.cancel()
        listener = nil
        boundPort = nil
        resumeStart(with: .failure(OSCFacadeError.bindFailed(port: configuration.oscPort, reason: "stopped")))
    }

    /// Stops admitting the commands that could (re)arm the flock — `load`,
    /// `play`, `go`, `seek` are answered with `/duckswarm/error "shutting
    /// down"` — while `stop`, `panic`, `ping`, `status` and every feedback
    /// push keep working. `swarmctl serve` calls this before its graceful
    /// final stop so a GO arriving in that window cannot re-arm the ducks
    /// behind the master's back and leave them performing with no master.
    public func quiesce() {
        isQuiescing = true
        log?("osc: quiescing — load/play/go/seek are refused from now on")
    }

    /// A live feed of listener failures *after* a successful `start()`
    /// (interface torn down, resource error): by the time one arrives the
    /// facade has already `stop()`ped itself — no OSC, panic included, can
    /// reach the master until the owner rebinds or exits. Same lifecycle
    /// as `SwarmMaster.transportEvents()`: independent per call, finishes
    /// on cancel or deinit.
    public func listenerFailures() -> AsyncStream<OSCFacadeError> {
        let id = UUID()
        return AsyncStream(OSCFacadeError.self, bufferingPolicy: .bufferingNewest(4)) { continuation in
            failureContinuations[id] = continuation
            continuation.onTermination = { [weak self] _ in
                Task { await self?.removeFailureContinuation(id) }
            }
        }
    }

    /// Number of currently live feedback subscribers (test hook).
    public var subscriberCount: Int {
        subscribers.liveEndpoints().count
    }

    /// Number of inbound UDP flows currently tracked (test hook).
    public var trackedFlowCount: Int {
        flows.count
    }

    // MARK: Show-id resolution (pure)

    /// `<dir>/<id>.duckshow.json`, then `<dir>/<id>/<id>.duckshow.json`
    /// — the duck-agent's `_resolve_show_path`. Ids that could escape the
    /// shows directory are refused: a rig only ever names a show by id.
    public static func resolveShow(id: String, in directory: URL) -> URL? {
        guard isValidShowID(id) else { return nil }
        let flat = directory.appendingPathComponent("\(id).duckshow.json")
        if FileManager.default.fileExists(atPath: flat.path) { return flat }
        let nested = directory.appendingPathComponent(id).appendingPathComponent("\(id).duckshow.json")
        if FileManager.default.fileExists(atPath: nested.path) { return nested }
        return nil
    }

    /// Non-empty, no path separators, no leading dot (so no `..`).
    public static func isValidShowID(_ id: String) -> Bool {
        !id.isEmpty && !id.hasPrefix(".") && !id.contains("/") && !id.contains("\\") && !id.contains("\0")
    }

    // MARK: Listener / flows

    private func resumeStart(with result: Result<UInt16, any Error>) {
        guard let continuation = startContinuation else { return }
        startContinuation = nil
        continuation.resume(with: result)
    }

    /// The start timeout: only the attempt it was armed for, and only while
    /// that attempt is still pending. The listener never reached `.ready`,
    /// so it is torn down like the `.failed` branch does — a later
    /// `start()` must not find a half-started listener in its way, and a
    /// listener that came up late must not serve with no feedback loops.
    private func failStartIfStillPending(attempt: Int, port: UInt16) {
        guard attempt == startAttempt, startContinuation != nil else { return }
        listener?.stateUpdateHandler = nil
        listener?.newConnectionHandler = nil
        listener?.cancel()
        listener = nil
        resumeStart(with: .failure(OSCFacadeError.startTimedOut(port: port)))
    }

    private func handleListenerState(_ state: NWListener.State, listener: NWListener) {
        guard self.listener === listener else { return } // stale callback from a previous listener
        switch state {
        case .ready:
            resumeStart(with: .success(listener.port?.rawValue ?? configuration.oscPort))
        case .failed(let error):
            let failure = OSCFacadeError.bindFailed(port: configuration.oscPort, reason: "\(error)")
            if startContinuation != nil {
                self.listener = nil
                listener.cancel()
                resumeStart(with: .failure(failure))
            } else {
                log?("osc: listener failed: \(error)")
                stop()
                for continuation in failureContinuations.values { continuation.yield(failure) }
            }
        case .waiting(let error):
            log?("osc: listener waiting: \(error)")
        default:
            break
        }
    }

    private func removeFailureContinuation(_ id: UUID) {
        failureContinuations.removeValue(forKey: id)
    }

    private func accept(_ connection: NWConnection) {
        guard listener != nil else {
            connection.cancel()
            return
        }
        if flows.count >= Self.maxTrackedFlows {
            evictOneFlow(toAdmit: connection.endpoint)
        }
        let key = ObjectIdentifier(connection)
        flows[key] = Flow(connection: connection, lastHeardNs: MasterClock.nowNanoseconds())
        connection.stateUpdateHandler = { [weak self, weak connection] state in
            guard let self, let connection else { return }
            switch state {
            case .failed, .cancelled:
                Task { await self.forgetFlow(key, ifStill: connection) }
            default:
                break
            }
        }
        connection.start(queue: queue)
        receiveNext(on: connection, key: key)
    }

    /// The table is full: hang up on the flow heard from least recently —
    /// a live subscriber's only as a last resort — so the newcomer (a fresh
    /// `osc_send.py` process, a restarted rig, a panic from a socket never
    /// seen before) is always admitted. Logged: an operator should know
    /// when the rig is churning through that many source ports.
    private func evictOneFlow(toAdmit newcomer: NWEndpoint) {
        let live = subscribers.liveEndpoints()
        let oldestFirst = flows.sorted { $0.value.lastHeardNs < $1.value.lastHeardNs }
        guard let victim = oldestFirst.first(where: { !live.contains($0.value.connection.endpoint) }) ?? oldestFirst.first else {
            return
        }
        log?("osc: flow table full (\(Self.maxTrackedFlows)) — hanging up on \(victim.value.connection.endpoint) to admit \(newcomer)")
        flows.removeValue(forKey: victim.key)
        victim.value.connection.cancel()
    }

    /// Only the connection the key was registered for may remove it: a
    /// late `.cancelled` from a flow that was already replaced must not
    /// evict its successor.
    private func forgetFlow(_ key: ObjectIdentifier, ifStill connection: NWConnection) {
        guard flows[key]?.connection === connection else { return }
        flows.removeValue(forKey: key)
    }

    /// Pumps one UDP flow: each datagram is decoded on the network queue
    /// and only the value-type result crosses into the actor.
    private func receiveNext(on connection: NWConnection, key: ObjectIdentifier) {
        connection.receiveMessage { [weak self] data, _, _, error in
            guard let self else { return }
            var decoded: Result<[OSCMessage], any Error>?
            if let data, !data.isEmpty {
                decoded = Result { try OSCCodec.decodePacket(data) }
            }
            Task { await self.onReceive(key: key, connection: connection, decoded: decoded, error: error) }
        }
    }

    private func onReceive(key: ObjectIdentifier, connection: NWConnection, decoded: Result<[OSCMessage], any Error>?, error: NWError?) {
        guard flows[key]?.connection === connection else { return }
        flows[key]?.lastHeardNs = MasterClock.nowNanoseconds()
        switch decoded {
        case .success(let messages)?:
            for message in messages {
                handle(message, from: connection)
            }
        case .failure(let failure)?:
            log?("osc: dropped malformed packet from \(connection.endpoint): \(failure)")
        case nil:
            break
        }
        if error == nil {
            receiveNext(on: connection, key: key)
        } else {
            connection.cancel()
            flows.removeValue(forKey: key)
        }
    }

    // MARK: Inbound command table (docs/osc-facade.md)

    private func handle(_ message: OSCMessage, from sender: NWConnection) {
        switch message.address {
        case "/duckswarm/ping":
            if subscribers.touch(sender.endpoint) {
                log?("osc: subscriber \(sender.endpoint) (ttl \(configuration.subscriberTTLSeconds) s)")
                Task { await self.pushStatus(to: [sender]) }
            }

        case "/duckswarm/status":
            Task { await self.pushStatus(to: [sender]) }

        case "/duckswarm/load":
            guard let id = message.args.first?.stringValue else {
                reply(error: "load requires a string show-id", to: sender)
                return
            }
            guard admitArmingCommand(from: sender) else { return }
            logCommand(message, from: sender)
            Task { await self.performLoad(id: id, sender: sender) }

        case "/duckswarm/play":
            var lead = configuration.defaultLeadSeconds
            if let first = message.args.first {
                guard let value = first.numberValue, value.isFinite, value >= 0, value <= Self.maxLeadSeconds else {
                    reply(error: "play lead must be a float 0–\(Int(Self.maxLeadSeconds)) seconds", to: sender)
                    return
                }
                lead = value
            }
            guard admitArmingCommand(from: sender) else { return }
            logCommand(message, from: sender)
            Task { await self.performPlay(leadSeconds: lead, sender: sender) }

        case "/duckswarm/go":
            guard admitArmingCommand(from: sender) else { return }
            logCommand(message, from: sender)
            Task { await self.performPlay(leadSeconds: self.configuration.defaultLeadSeconds, sender: sender) }

        case "/duckswarm/seek":
            guard let showTime = message.args.first?.numberValue, showTime.isFinite, showTime >= 0 else {
                reply(error: "seek requires a float show-time in seconds", to: sender)
                return
            }
            guard admitArmingCommand(from: sender) else { return }
            logCommand(message, from: sender)
            Task { await self.performSeek(to: showTime, sender: sender) }

        case "/duckswarm/pause":
            logCommand(message, from: sender)
            Task { await self.performPause(sender: sender) }

        case "/duckswarm/resume":
            logCommand(message, from: sender)
            Task { await self.performResume(sender: sender) }

        case "/duckswarm/relax":
            // Bare `/duckswarm/relax` relaxes; an explicit 0 re-torques. A
            // console with only a momentary button therefore gets the useful
            // half by default, and the toggle by sending the argument.
            let on = message.args.first?.numberValue.map { $0 != 0 } ?? true
            logCommand(message, from: sender)
            Task { await self.performRelax(on: on, sender: sender) }

        case "/duckswarm/stop":
            logCommand(message, from: sender)
            Task { await self.performStop(sender: sender) }

        case "/duckswarm/panic":
            logCommand(message, from: sender)
            Task { await self.performPanic(sender: sender) }

        default:
            log?("osc: ignoring unknown address \(message.address) from \(sender.endpoint)")
        }
    }

    /// Upper bound on a `/duckswarm/play` lead so the ns conversion can
    /// never overflow; a rig that wants a longer countdown fires later.
    private static let maxLeadSeconds: Double = 3600

    /// `load`/`play`/`go`/`seek` are refused once `quiesce()` was called.
    /// Checked both at dispatch and again when the command's Task runs, so
    /// a command already decoded when the owner quiesced is refused too.
    private func admitArmingCommand(from sender: NWConnection) -> Bool {
        guard isQuiescing else { return true }
        reply(error: "shutting down", to: sender)
        return false
    }

    private func logCommand(_ message: OSCMessage, from sender: NWConnection) {
        let args = message.args.map { Self.describe($0) }.joined(separator: " ")
        log?("osc: \(message.address)\(args.isEmpty ? "" : " " + args) from \(sender.endpoint)")
    }

    private static func describe(_ arg: OSCArg) -> String {
        switch arg {
        case .int32(let v): return "i:\(v)"
        case .float32(let v): return "f:\(v)"
        case .string(let v): return "s:\"\(v)\""
        case .blob(let v): return "b:<\(v.count) bytes>"
        case .true: return "T"
        case .false: return "F"
        }
    }

    // MARK: Command execution (each in its own Task)

    private func performLoad(id: String, sender: NWConnection) async {
        guard admitArmingCommand(from: sender) else { return }
        // Resolving the id is two `stat`s (metadata, local even on a
        // cloud-synced volume) and stays on the actor on purpose: a rig's
        // `/load` followed by `/go` must reach the master in that order so
        // the GO is refused with "load in progress" rather than arming the
        // previous show. The file *reads* are the master's, off its actor.
        guard let url = Self.resolveShow(id: id, in: configuration.showsDirectory) else {
            reply(error: "show not found: \(id)", to: sender)
            return
        }
        do {
            let outcomes = try await master.load(show: url, roster: configuration.rosterURL)
            await pushFeedback(command: "load", outcomes: outcomes.mapValues(\.status), sender: sender)
        } catch {
            reply(error: "load failed: \(error)", to: sender)
        }
    }

    private func isLoadFailed(_ error: SwarmMasterError) -> Bool {
        if case .loadFailed = error { return true }
        return false
    }

    private func performPlay(leadSeconds: Double, sender: NWConnection) async {
        guard admitArmingCommand(from: sender) else { return }
        do {
            let outcomes = try await master.play(at: Int64(leadSeconds * 1_000_000_000))
            await pushFeedback(command: "play", outcomes: outcomes, sender: sender)
        } catch SwarmMasterError.notLoaded {
            reply(error: "no show loaded", to: sender)
        } catch SwarmMasterError.loadInProgress {
            reply(error: "load in progress", to: sender)
        } catch let error as SwarmMasterError where isLoadFailed(error) {
            // Deliberately NOT overridable from the show-control surface:
            // bypassing this gate means a duck performs a different show, and
            // that should not be one fat-fingered cue away. The override lives
            // on swarmctl (--allow-failed-loads).
            reply(error: "\(error) — reload, or override with swarmctl --allow-failed-loads", to: sender)
        } catch {
            reply(error: "play failed: \(error)", to: sender)
        }
    }

    private func performSeek(to showTime: Double, sender: NWConnection) async {
        guard admitArmingCommand(from: sender) else { return }
        let outcomes = await master.seek(to: showTime)
        await pushFeedback(command: "seek", outcomes: outcomes, sender: sender)
    }

    private func performPause(sender: NWConnection) async {
        guard admitArmingCommand(from: sender) else { return }
        let outcomes = await master.pause()
        // An empty result means the master refused (not playing). Say so
        // rather than reporting a successful pause of nothing.
        guard !outcomes.isEmpty else {
            reply(error: "cannot pause: not playing", to: sender)
            return
        }
        await pushFeedback(command: "pause", outcomes: outcomes, sender: sender)
    }

    private func performResume(sender: NWConnection) async {
        guard admitArmingCommand(from: sender) else { return }
        let outcomes = await master.resume()
        guard !outcomes.isEmpty else {
            reply(error: "cannot resume: not paused", to: sender)
            return
        }
        await pushFeedback(command: "resume", outcomes: outcomes, sender: sender)
    }

    private func performRelax(on: Bool, sender: NWConnection) async {
        guard admitArmingCommand(from: sender) else { return }
        let outcomes = await master.relax(on: on)
        guard !outcomes.isEmpty else {
            reply(error: "cannot relax: stop the show first", to: sender)
            return
        }
        await pushFeedback(command: "relax", outcomes: outcomes, sender: sender)
    }

    private func performStop(sender: NWConnection) async {
        let outcomes = await master.stop()
        await pushFeedback(command: "stop", outcomes: outcomes, sender: sender)
    }

    private func performPanic(sender: NWConnection) async {
        let outcomes = await master.panic()
        await pushFeedback(command: "panic", outcomes: outcomes, sender: sender)
    }

    // MARK: Outbound feedback

    /// `/duckswarm/ack` per duck plus a full status push, to every live
    /// subscriber and to the command's sender (subscribed or not).
    private func pushFeedback(command: String, outcomes: [DuckID: CommandStatus], sender: NWConnection) async {
        if outcomes.isEmpty {
            reply(error: "\(command): no ducks connected", to: sender)
        }
        let targets = recipients(including: sender)
        let status = await statusMessages()
        send(Self.ackMessages(command: command, outcomes: outcomes) + status, to: targets)
    }

    private func pushStatus(to targets: [NWConnection]) async {
        let status = await statusMessages()
        send(status, to: targets)
    }

    private func pushStatusToSubscribers() async {
        let targets = recipients(including: nil)
        guard !targets.isEmpty else { return }
        await pushStatus(to: targets)
    }

    private func reply(error message: String, to sender: NWConnection) {
        log?("osc: error → \(sender.endpoint): \(message)")
        send([OSCMessage(address: "/duckswarm/error", args: [.string(message)])], to: [sender])
    }

    /// Every live subscriber's flow (one per endpoint) plus `sender`.
    private func recipients(including sender: NWConnection?) -> [NWConnection] {
        let live = subscribers.liveEndpoints()
        var byEndpoint: [NWEndpoint: NWConnection] = [:]
        for flow in flows.values where live.contains(flow.connection.endpoint) {
            byEndpoint[flow.connection.endpoint] = flow.connection
        }
        if let sender {
            byEndpoint[sender.endpoint] = sender
        }
        return Array(byEndpoint.values)
    }

    /// One datagram per message, on the flow that already exists for each
    /// recipient — replies always go back to the endpoint that sent.
    private func send(_ messages: [OSCMessage], to targets: [NWConnection]) {
        guard !messages.isEmpty, !targets.isEmpty else { return }
        let datagrams = messages.map { $0.encode() }
        for connection in targets {
            for datagram in datagrams {
                connection.send(content: datagram, completion: .idempotent)
            }
        }
    }

    static func ackMessages(command: String, outcomes: [DuckID: CommandStatus]) -> [OSCMessage] {
        outcomes.keys.sorted().map { duckID in
            let ok: Int32
            let error: String
            switch outcomes[duckID]! {
            case .ok: (ok, error) = (1, "")
            case .nacked(let reason): (ok, error) = (0, reason)
            case .timeout: (ok, error) = (0, "timeout")
            case .connectionFailed(let reason): (ok, error) = (0, reason)
            case .superseded: (ok, error) = (0, "superseded")
            }
            return OSCMessage(address: "/duckswarm/ack", args: [.string(command), .string(duckID.raw), .int32(ok), .string(error)])
        }
    }

    /// The full status feed, exactly the shapes in docs/osc-facade.md:
    /// transport, show, show_time, summary, then one `status/duck` per
    /// roster duck — all from one `statusSnapshot()` so the push describes
    /// one instant. A duck that has never reported, or whose telemetry has
    /// gone stale, is `lost` on the wire (roster = reporting + lost).
    private func statusMessages() async -> [OSCMessage] {
        let snapshot = await master.statusSnapshot()
        let showTime = snapshot.transport == .stopped ? 0.0 : (snapshot.showTime ?? 0.0)

        var duckMessages: [OSCMessage] = []
        var reporting = 0
        for duckID in snapshot.roster.keys.sorted() {
            let role = snapshot.roster[duckID]?.role ?? ""
            if let entry = snapshot.telemetry[duckID], !entry.lost, !snapshot.lostDucks.contains(duckID) {
                reporting += 1
                duckMessages.append(OSCMessage(address: "/duckswarm/status/duck", args: [
                    .string(duckID.raw), .string(role), .string(entry.state.rawValue),
                    .float32(Float(entry.showTime)), .float32(Float(entry.clockOffsetMs ?? -1.0)),
                    .int32(entry.policiesOk ? 1 : 0)
                ]))
            } else {
                duckMessages.append(OSCMessage(address: "/duckswarm/status/duck", args: [
                    .string(duckID.raw), .string(role), .string("lost"),
                    .float32(0), .float32(-1.0), .int32(0)
                ]))
            }
        }

        return [
            OSCMessage(address: "/duckswarm/status/transport", args: [.string(snapshot.transport.rawValue)]),
            OSCMessage(address: "/duckswarm/status/show", args: [.string(snapshot.showID ?? "")]),
            OSCMessage(address: "/duckswarm/status/show_time", args: [.float32(Float(showTime))]),
            OSCMessage(address: "/duckswarm/status/summary", args: [
                .int32(Int32(snapshot.roster.count)), .int32(Int32(reporting)), .int32(Int32(snapshot.roster.count - reporting))
            ])
        ] + duckMessages
    }

    // MARK: Cadence: 2 Hz armed/playing, 0.5 Hz otherwise, plus immediately
    // on transport change (ack/nack pushes come from `pushFeedback`)

    private func restartStatusLoop() {
        statusLoopTask?.cancel()
        statusLoopTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                let intervalNs = await self.statusIntervalNs()
                do {
                    try await Task.sleep(nanoseconds: intervalNs)
                } catch {
                    return // cancelled
                }
                await self.pushStatusToSubscribers()
            }
        }
    }

    private func statusIntervalNs() async -> UInt64 {
        let transport = await master.currentTransport
        let hz = transport == .stopped ? configuration.idleStatusHz : configuration.activeStatusHz
        return UInt64(1_000_000_000.0 / max(0.01, hz))
    }

    /// Parks on the master's transport stream. `self` is re-acquired per
    /// event, never held across the wait: the stream only ends when the
    /// master goes away, and an owner that drops the facade without
    /// `stop()` must still reach `deinit` (which cancels this task).
    private func startTransportWatch() {
        transportWatchTask?.cancel()
        let master = self.master
        transportWatchTask = Task { [weak self] in
            let changes = await master.transportEvents()
            for await transport in changes {
                if Task.isCancelled { return }
                guard let self else { return }
                await self.onTransportChanged(transport)
            }
        }
    }

    private func onTransportChanged(_ transport: Transport) async {
        guard listener != nil else { return } // stop() raced this event: nothing to restart
        log?("osc: transport → \(transport.rawValue)")
        restartStatusLoop() // the new cadence starts now, not after the old sleep
        await pushStatusToSubscribers()
    }
}
