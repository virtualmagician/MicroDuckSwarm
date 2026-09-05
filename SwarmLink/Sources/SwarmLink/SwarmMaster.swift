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
// `play`/`stop`/`panic` are cheap calls a transport button can call
// directly. Each transport command issues its datagrams *before* its first
// suspension point and then reports the per-duck ACK/NACK/timeout outcome
// (≤ commandMaxAttempts × commandRetryIntervalMs later) — callers that do
// not care can discard the result (`Task { try await master.play() }`).
//
// Ordering guarantees (show-night invariants):
//  - The newest transport command wins: issuing play/seek/stop/panic/load
//    stops the retries of every earlier in-flight command (a stale `play`
//    retry can never re-arm a duck after the operator hit stop). This holds
//    across suspension points too: a `load` that is still reading files or
//    stopping the previous show when a newer command lands gives up as
//    `.superseded` instead of resuming and overriding it — so a `panic`
//    keeps every one of its retries no matter what it interrupted.
//  - `play` is refused (`loadInProgress`) while a `load` is under way: it
//    would otherwise arm whichever show happened to be current mid-load.
//  - `stop`/`panic` reset the cue point; `seek` while stopped sets it.
//  - `panic` needs nothing loaded — only a connected roster.
//  - The transport returns to `.stopped` by itself once the master's show
//    clock passes `meta.duration` (within one 5 Hz state tick): the agents
//    end playback there on their own, and a master that kept reporting
//    `playing` would mislead whatever watches it (the OSC facade, a GUI).
//
// Connection hygiene: `load()`/`connect()` keep the live UDP flow of every
// duck whose host:port is unchanged. Cancelling a fixed-local-port
// `NWConnection` and immediately re-dialing the same peer parks the new
// connection in `.waiting(EADDRINUSE)` for good (Network.framework never
// retries it while the old socket is still closing), which made the master
// deaf to the whole flock on every `load` after `connect`. Only newcomers
// and ducks that moved address are dialed; a connection that does report
// `.waiting`/`.failed` is hung up on first and re-dialed after a backoff.

import Foundation
@preconcurrency import Network

// MARK: - Public result / telemetry types

public struct LoadOutcome: Sendable, Equatable {
    public enum Status: Sendable, Equatable {
        case ok
        case nacked(String)
        case timeout
        case connectionFailed(String)
        /// A newer command (play/seek/stop/panic/load) was issued before
        /// this one was ACKed; its remaining retries were abandoned so the
        /// newer command is the one the duck ends up acting on.
        case superseded
    }

    public var status: Status

    public init(status: Status) {
        self.status = status
    }

    public var isOK: Bool { status == .ok }
}

/// Per-duck outcome of one transport command (`play`/`seek`/`stop`/`panic`).
public typealias CommandStatus = LoadOutcome.Status

public struct DuckTelemetry: Sendable, Equatable {
    public var duck: DuckID
    public var state: AgentState
    public var show: String?
    public var showTime: Double
    /// Milliseconds; `nil` until the duck's first successful time-sync
    /// exchange (docs/swarmlink-protocol.md §4) — never treat `nil` here as 0.
    public var clockOffsetMs: Double?
    /// Milliseconds; `nil` until the duck's first successful time-sync
    /// exchange — see `clockOffsetMs`.
    public var clockRttMs: Double?
    public var policiesOk: Bool
    public var batteryPct: Double?
    public var rssiDbm: Double?
    public var lastError: String?
    /// True while a puppet stream to this duck is fresh (docs/swarmlink-
    /// protocol.md §6) — a duck under a live (or forgotten) puppet sender
    /// is indistinguishable from one on the timeline without it.
    public var puppet: Bool
    /// True while this duck's torque is released (docs/swarmlink-protocol.md
    /// "Relax") — which ducks are safe to pick up, per duck, so an operator
    /// repositioning a cast between chapters reads it rather than remembering
    /// it. `false` from an agent that predates the relax command.
    public var relaxed: Bool
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
    /// The duck has sent no telemetry for `telemetryLostThresholdSeconds`
    /// — including a roster duck that never reported at all since it was
    /// dialed by `load()`/`connect()` (see `lostDucks`).
    case lost(DuckID)
}

/// Errors surfaced synchronously from `load`/`play` (i.e. before any
/// per-duck fan out — malformed inputs, not per-duck network failures,
/// which show up as `LoadOutcome.Status` instead).
public enum SwarmMasterError: Error, Sendable, Equatable, CustomStringConvertible {
    case notLoaded
    /// `play()` while one or more ducks did not successfully `load` the
    /// current show. A duck that NACKed a load still holds whatever show it
    /// had before and will accept a `play` naming it, so playing anyway is a
    /// cast split the master created. Pass `allowingFailedLoads: true` to
    /// override deliberately. See docs/swarmlink-protocol.md "The master must
    /// not play over a failed load".
    case loadFailed([DuckID])
    /// `play()` while a `load()` is still under way (reading the show,
    /// stopping the previous one, or fanning out): arming now would arm
    /// whichever show happened to be current mid-load.
    case loadInProgress
    /// `puppet(duck:frame:)` for a duck that is not on the dialed roster.
    case notConnected(DuckID)

    public var description: String {
        switch self {
        case .notLoaded: return "no show is loaded"
        case .loadFailed(let ducks):
            let names = ducks.map(\.description).sorted().joined(separator: ", ")
            return "these ducks did not load the current show: \(names)"
        case .loadInProgress: return "a load is in progress"
        case .notConnected(let duck): return "duck \(duck) is not connected (not on the dialed roster)"
        }
    }
}

/// Everything a status display needs, read in one actor hop so the values
/// belong to one instant (the OSC facade's full status push; a GUI).
public struct SwarmStatusSnapshot: Sendable, Equatable {
    public var transport: Transport
    /// `nil` when nothing is loaded.
    public var showID: String?
    /// The master's show clock: `nil` while stopped, the cued start
    /// position while armed (not advancing yet), the running clock while
    /// playing.
    public var showTime: Double?
    public var roster: [DuckID: RosterEntry]
    public var telemetry: [DuckID: DuckTelemetry]
    public var lostDucks: Set<DuckID>
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
    private let telemetryLostThresholdSeconds: Double
    private let networkQueue = DispatchQueue(label: "SwarmLink.SwarmMaster.network")

    /// Backoff before re-dialing a per-duck connection that reported
    /// `.failed` or a receive error.
    private let redialBackoffNs: UInt64 = 500_000_000

    /// How many telemetry events a `telemetryEvents()` subscriber may fall
    /// behind before the oldest are dropped. Telemetry is a lossy
    /// latest-state feed and `telemetry` always holds the authoritative
    /// snapshot, so an idle subscriber must never make the master grow
    /// without bound (10 ducks × 5 Hz × ~5 s).
    private let telemetryStreamBuffer = 256

    private var connections: [DuckID: NWConnection] = [:]
    private var roster: [DuckID: RosterEntry] = [:]

    private var show: Show?
    private var showID: String = ""
    private var showSHA256: String = ""

    private var clock = MasterClock()
    /// Master-side transport intent. Every *change* is published to
    /// `transportEvents()` subscribers (the OSC facade pushes status
    /// "immediately on any transport change" — docs/osc-facade.md).
    private var transport: Transport = .stopped {
        didSet {
            guard oldValue != transport else { return }
            for continuation in transportContinuations.values { continuation.yield(transport) }
        }
    }
    private var transportContinuations: [UUID: AsyncStream<Transport>.Continuation] = [:]
    private var stateSeq: Int = 0
    /// Show-time to start from on the next `play()`; set by `seek()` while
    /// stopped, consumed (and reset to 0) by the next `play()`, and reset
    /// to 0 by `stop()`/`panic()`/`load()`.
    private var cueShowTime: Double = 0.0
    /// The show-time the pending scheduled start will begin from: set by
    /// `play()` and cleared once `beginPlaying` establishes the epoch (or
    /// anything else ends the armed state). Reported as the show clock
    /// while armed, like the Python reference master's `from_show_time`.
    private var armedFromShowTime: Double?

    private var telemetryStore: [DuckID: DuckTelemetry] = [:]
    private var lastTelemetryNs: [DuckID: Int64] = [:]
    /// Roster ducks the watchdog has flagged lost before they ever sent a
    /// telemetry datagram (so there is no `telemetryStore` entry to mark).
    private var neverReportedLost: Set<DuckID> = []
    private var telemetryContinuations: [UUID: AsyncStream<TelemetryEvent>.Continuation] = [:]

    private var pendingAcks: [String: (AckMessage?) -> Void] = [:]
    /// Bumped by every fan-out; a retry loop whose generation is no longer
    /// current abandons its remaining attempts (`.superseded`).
    private var commandGeneration: Int = 0
    /// Bumped by play/seek/stop/panic so a scheduled `beginPlaying` from an
    /// earlier `play()` cannot clobber the clock epoch of a newer command.
    private var playGeneration: Int = 0
    /// Count of transport commands issued (load/play/seek/stop/panic). A
    /// multi-phase command — `load`, with its off-actor file reads and its
    /// stop-first fan-out — takes a ticket when it is issued and, after
    /// each suspension, abandons itself as `.superseded` if a newer command
    /// has been issued meanwhile. Newest command wins: an older `load` can
    /// never resume and override a `panic` that arrived while it was
    /// stopping the previous show.
    private var issuedCommands: Int = 0
    /// `load()`s currently between issue and return; `play()` is refused
    /// with `loadInProgress` meanwhile.
    private var loadsInProgress: Int = 0
    /// Per-duck result of the most recent `load()`, which `play()` gates on.
    /// Replaced wholesale by the next load, so the gate never reflects a stale
    /// verdict about a show that is no longer the one about to play.
    private var lastLoadOutcomes: [DuckID: LoadOutcome] = [:]

    private var stateLoopTask: Task<Void, Never>?
    private var stateLoopGeneration: Int = 0
    private var housekeepingTask: Task<Void, Never>?

    /// Last puppet `seq` stamped per duck (docs/swarmlink-protocol.md §6:
    /// agents drop packets with a `seq` ≤ the last one seen, and only
    /// forget it after 2 s of silence). Seeded from the *wall-clock*
    /// millisecond on first use — the same seed `python/tools/puppet.py`
    /// uses — so a stream started by either sender within 2 s of the
    /// other's last packet is not locked out as stale: every sender's
    /// first `seq` is comparable across processes and machines. (An uptime
    /// clock, ~1e8 ms after days of uptime, sits far below the Python
    /// tool's ~1.8e12 and every frame would be dropped.) This is a
    /// sequence seed, not a protocol time: rule 4's "wall clocks are never
    /// trusted" is about timestamps, which stay on the monotonic clock.
    private var puppetSeq: [DuckID: Int] = [:]

    /// The puppet `seq` seed for a duck this master has not streamed to
    /// yet: milliseconds since the Unix epoch, never below 1.
    static func puppetSeqSeed(now: Date = Date()) -> Int {
        max(1, Int((now.timeIntervalSince1970 * 1000).rounded(.down)))
    }

    /// - Parameters:
    ///   - masterPort: local UDP port every per-duck connection is pinned
    ///     to (0 = ephemeral, for tests).
    ///   - telemetryLostThresholdSeconds: seconds without telemetry after
    ///     which a duck is marked lost (protocol default 5 s; injectable so
    ///     tests need not wait that long).
    public init(
        masterPort: UInt16 = SwarmLinkInfo.defaultMasterPort,
        telemetryLostThresholdSeconds: Double = SwarmLinkInfo.telemetryLostThresholdSeconds
    ) {
        self.masterPort = masterPort
        self.telemetryLostThresholdSeconds = max(0.05, telemetryLostThresholdSeconds)
    }

    deinit {
        stateLoopTask?.cancel()
        housekeepingTask?.cancel()
        for continuation in telemetryContinuations.values { continuation.finish() }
        for continuation in transportContinuations.values { continuation.finish() }
        for connection in connections.values { connection.cancel() }
    }

    // MARK: Public API — roster / connections

    /// Dials one UDP connection per roster duck *without* loading a show,
    /// so time sync, telemetry and the transport commands that need no
    /// show (`stop`, `panic`, `seek`) work — e.g. a standalone CLI that
    /// only wants to kill the flock. Ducks already dialed at the same
    /// host:port keep their live connection (and watchdog state); ducks
    /// that left the roster are hung up on, newcomers and movers dialed.
    /// A playing show is stopped first (see `load`). `load()` does this
    /// itself; `play()` still requires a loaded show.
    public func connect(roster rosterURL: URL) async throws {
        let entries = try [RosterEntry].load(contentsOf: rosterURL)
        try await connect(roster: entries)
    }

    /// Same as `connect(roster:)` for an in-memory roster.
    public func connect(roster entries: [RosterEntry]) async throws {
        var seen = Set<DuckID>()
        for entry in entries where !seen.insert(entry.id).inserted {
            throw RosterError.duplicateDuckID(entry.id)
        }
        await stopIfTransportActive()
        rewire(entries: entries)
    }

    /// Ducks currently dialed (the roster of the last `load()`/`connect()`).
    public var connectedDucks: Set<DuckID> {
        Set(connections.keys)
    }

    // MARK: Public API — show / transport

    /// Parses `showURL`, loads the roster from `rosterURL`, brings the
    /// dialed set in line with it (one UDP connection per duck, pinned to
    /// `masterPort`; see `connect(roster:)` — unchanged ducks keep their
    /// connection), and fans out `load`
    /// commands — retried up to `SwarmLinkInfo.commandMaxAttempts` times at
    /// `SwarmLinkInfo.commandRetryIntervalMs` — waiting for every duck's
    /// ACK/NACK/timeout. This is the "heavy" call: by the time it returns,
    /// every reachable duck is pre-armed and `play()` only has to send one
    /// more small unicast per duck.
    ///
    /// If a show is armed/playing, `stop` is fanned out (and awaited) first
    /// so no duck is left running its last commanded velocity while the
    /// master silently switches shows.
    ///
    /// A `play()` issued while this is under way throws `loadInProgress`;
    /// any other command issued meanwhile (panic, stop, seek, another load)
    /// wins — this load then returns `.superseded` for every roster duck
    /// without touching the show or the transport.
    public func load(show showURL: URL, roster rosterURL: URL) async throws -> [DuckID: LoadOutcome] {
        let ticket = issueCommand()
        loadsInProgress += 1
        defer { loadsInProgress -= 1 }

        // The file reads run off the actor: a stalled volume (an online-only
        // cloud placeholder, a network share) must not hold the actor — and
        // a `panic` queued behind this load — for the duration of the read.
        let (decodedShow, sha, entries) = try await Task.detached {
            (try Show.load(contentsOf: showURL), try Show.sha256(of: showURL), try [RosterEntry].load(contentsOf: rosterURL))
        }.value
        guard ticket == issuedCommands else { return Self.supersededOutcomes(for: entries) }

        await stopIfTransportActive()
        guard ticket == issuedCommands else { return Self.supersededOutcomes(for: entries) }
        rewire(entries: entries)

        self.show = decodedShow
        self.showID = showURL.deletingPathExtension().deletingPathExtension().lastPathComponent
        self.showSHA256 = sha
        self.transport = .stopped
        self.stateSeq = 0
        self.cueShowTime = 0
        self.armedFromShowTime = nil
        self.playGeneration += 1
        clock.stop()

        let castRoles = Set(decodedShow.cast.map(\.role))
        let generation = supersedeInFlightCommands()
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
                    let status = await self.sendWithRetry(message, to: entry.id, generation: generation)
                    return (entry.id, LoadOutcome(status: status))
                }
            }
            for await (id, outcome) in group {
                results[id] = outcome
            }
        }
        lastLoadOutcomes = results
        return results
    }

    /// Ducks whose most recent `load` did not succeed. Every non-OK status
    /// counts, not just an explicit NACK: a timeout, a connection failure and
    /// a superseded command all mean the master does not know what that duck
    /// is holding, which is the thing that makes playing unsafe.
    public var ducksWithFailedLoads: [DuckID] {
        Array(lastLoadOutcomes.filter { !$0.value.isOK }.keys).sorted()
    }

    /// Schedules playback to start `leadTimeNs` in the future (giving the
    /// `play` command time to be delivered/retried before it's due), sends
    /// `play` to every duck, and returns each duck's ACK outcome once every
    /// retry loop has finished. Starts from the show-time set by a prior
    /// `seek()` while stopped, if any, else from 0. Throws
    /// `SwarmMasterError.notLoaded` when no show has been loaded in this
    /// instance (the agents need the show id) and
    /// `SwarmMasterError.loadInProgress` while a `load()` is under way.
    @discardableResult
    public func play(
        at leadTimeNs: Int64 = 300_000_000,
        allowingFailedLoads: Bool = false
    ) async throws -> [DuckID: CommandStatus] {
        try await play(
            atMasterTime: MasterClock.nowNanoseconds() + max(0, leadTimeNs),
            allowingFailedLoads: allowingFailedLoads
        )
    }

    /// `play(at:)` with the start instant chosen by the caller: the show
    /// begins at master-monotonic `atMasterTime` (the `at_master_time` every
    /// duck receives), so a caller that must line other work up with the
    /// play epoch — the recorder streaming puppet frames "from the play
    /// epoch", a cue player syncing media — shares the exact instant
    /// instead of re-deriving it. An instant already in the past starts at
    /// once (the agents join in progress within their 2 s grace).
    @discardableResult
    public func play(
        atMasterTime: Int64,
        allowingFailedLoads: Bool = false
    ) async throws -> [DuckID: CommandStatus] {
        guard loadsInProgress == 0 else { throw SwarmMasterError.loadInProgress }
        guard show != nil else { throw SwarmMasterError.notLoaded }
        // load NACKs are per-duck and are the entire point of the hash check.
        // Playing over one means a duck performs whatever show it was already
        // holding, which is a cast split the master created rather than one
        // the network caused. Overridable, but only out loud.
        if !allowingFailedLoads {
            let failed = ducksWithFailedLoads
            guard failed.isEmpty else { throw SwarmMasterError.loadFailed(failed) }
        }
        _ = issueCommand()
        let now = MasterClock.nowNanoseconds()
        let fromShowTime = cueShowTime
        cueShowTime = 0

        transport = .armed
        clock.stop()
        armedFromShowTime = fromShowTime
        playGeneration += 1
        let generation = playGeneration
        startStateLoopIfNeeded()

        let delayNs = atMasterTime - now
        Task { [weak self] in
            if delayNs > 0 {
                try? await Task.sleep(nanoseconds: UInt64(delayNs))
            }
            await self?.beginPlaying(generation: generation, atMasterTime: atMasterTime, fromShowTime: fromShowTime)
        }

        return await fanOut(.play(show: showID, atMasterTime: atMasterTime, fromShowTime: fromShowTime))
    }

    /// Re-anchors every duck's show clock to `showTime` right now. While
    /// armed or playing the master clock follows immediately (agents start
    /// playing from `showTime` on arrival, so a pending scheduled start is
    /// discarded). While stopped, the position is remembered as the cue
    /// for the next `play()`; the `seek` is still sent so a standalone
    /// master can steer ducks it did not start (they NACK if not playing).
    @discardableResult
    public func seek(to showTime: Double) async -> [DuckID: CommandStatus] {
        _ = issueCommand()
        let now = MasterClock.nowNanoseconds()
        switch transport {
        case .stopped:
            cueShowTime = showTime
        case .armed, .playing:
            playGeneration += 1
            clock.seek(to: showTime, atMasterTimeNs: now)
            armedFromShowTime = nil
            transport = .playing
            startStateLoopIfNeeded()
        case .paused:
            // Scrub the frozen point and stay frozen, exactly as the agent
            // does. Falling through to .playing here would silently un-pause
            // the cast from the master's side only.
            playGeneration += 1
            clock.pause(atShowTime: showTime)
            startStateLoopIfNeeded()
        }
        return await fanOut(.seek(showTime: showTime, atMasterTime: now))
    }

    /// Freezes the show where it is: every duck holds its pose with
    /// locomotion commanded to zero, and the master clock stops advancing.
    /// Refused unless playing. Idempotent — a second `pause()` while already
    /// paused is a no-op rather than a re-freeze at a new position.
    @discardableResult
    public func pause() async -> [DuckID: CommandStatus] {
        guard transport == .playing, let showTime = clock.showTime() else { return [:] }
        _ = issueCommand()
        let now = MasterClock.nowNanoseconds()
        playGeneration += 1
        clock.pause(atShowTime: showTime)
        transport = .paused
        startStateLoopIfNeeded()
        return await fanOut(.pause(atMasterTime: now))
    }

    /// Continues from exactly where `pause()` stopped. Refused unless paused,
    /// which is what makes a second GO a no-op instead of a re-anchor that
    /// would move this master's epoch away from the cast.
    @discardableResult
    public func resume() async -> [DuckID: CommandStatus] {
        guard transport == .paused else { return [:] }
        _ = issueCommand()
        let now = MasterClock.nowNanoseconds()
        playGeneration += 1
        clock.resume(atMasterTimeNs: now)
        transport = .playing
        startStateLoopIfNeeded()
        return await fanOut(.resume(atMasterTime: now))
    }

    /// Graceful stop: fans out `stop` and reports each duck's ACK outcome.
    /// Resets the cue point, so the next `play()` starts from 0.
    @discardableResult
    public func stop() async -> [DuckID: CommandStatus] {
        _ = issueCommand()
        haltTransport()
        return await fanOut(.stop)
    }

    /// Releases torque on every duck so the cast can be picked up and
    /// repositioned by hand (`on: false` re-torques). Refused while the
    /// transport is armed, playing or paused: the agents refuse it too, but a
    /// master that fanned it out mid-show would spend a full retry ladder
    /// collecting eight NACKs to learn what it already knew. `.stopped` is
    /// fine — it is the state a show sits in between chapters, which is
    /// exactly when the cast gets moved by hand.
    @discardableResult
    public func relax(on: Bool = true) async -> [DuckID: CommandStatus] {
        guard transport == .stopped else { return [:] }
        _ = issueCommand()
        return await fanOut(.relax(on: on))
    }

    /// Highest-priority stop, valid from any state, never NACKed by agents.
    /// Needs no loaded show — only connections (`connect(roster:)`).
    @discardableResult
    public func panic() async -> [DuckID: CommandStatus] {
        _ = issueCommand()
        haltTransport()
        return await fanOut(.panic)
    }

    // MARK: Public API — puppet stream (docs/swarmlink-protocol.md §6)

    /// Sends one puppet datagram to `duck` on its existing connection —
    /// unicast, unacknowledged, never retried (the next frame comes in
    /// 20 ms; the agent's 250 ms deadman covers a gap). `seq` is stamped
    /// monotonic per duck and `master_time` with the master's clock; the
    /// stamped frame is returned. Independent of the transport: works in
    /// every state (puppet mode while idle/loaded, the nudge layer while
    /// playing) and never interferes with commands — panic, stop and load
    /// keep their priority on the agent. Throws `notConnected` for a duck
    /// that is not on the dialed roster (`connect(roster:)` / `load`).
    @discardableResult
    public func puppet(duck: DuckID, frame: PuppetFrame) throws -> PuppetFrame {
        guard let connection = connections[duck] else { throw SwarmMasterError.notConnected(duck) }
        let now = MasterClock.nowNanoseconds()
        let seq = (puppetSeq[duck] ?? Self.puppetSeqSeed()) + 1
        puppetSeq[duck] = seq
        var stamped = frame
        stamped.v = SwarmLinkInfo.protocolVersion
        stamped.seq = seq
        stamped.masterTime = now
        let data = try JSONEncoder().encode(stamped)
        send(data, on: connection)
        return stamped
    }

    /// Snapshot of the last known telemetry per duck.
    public var telemetry: [DuckID: DuckTelemetry] {
        telemetryStore
    }

    /// Every duck the watchdog currently considers lost: those whose last
    /// telemetry is older than the threshold *and* roster ducks that have
    /// not reported at all since the last `load()`/`connect()`.
    public var lostDucks: Set<DuckID> {
        Set(telemetryStore.filter { $0.value.lost }.keys).union(neverReportedLost)
    }

    /// A live feed of telemetry updates and lost-duck transitions. Each
    /// call returns an independent stream; finishing/cancelling it
    /// unregisters cleanly. Buffered with `.bufferingNewest` — a subscriber
    /// that stops iterating only ever holds the most recent events.
    public func telemetryEvents() -> AsyncStream<TelemetryEvent> {
        let id = UUID()
        return AsyncStream(TelemetryEvent.self, bufferingPolicy: .bufferingNewest(telemetryStreamBuffer)) { continuation in
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

    /// A live feed of `currentTransport` *changes* (stopped → armed →
    /// playing → stopped …), including the armed → playing transition the
    /// scheduled start performs on its own after a `play()` lead time.
    /// Same lifecycle as `telemetryEvents()`: independent per call,
    /// unregisters on finish/cancel, keeps only the newest few events.
    public func transportEvents() -> AsyncStream<Transport> {
        let id = UUID()
        return AsyncStream(Transport.self, bufferingPolicy: .bufferingNewest(16)) { continuation in
            transportContinuations[id] = continuation
            continuation.onTermination = { [weak self] _ in
                Task { await self?.removeTransportContinuation(id) }
            }
        }
    }

    /// Id of the show loaded by the last successful `load()`, or `nil`
    /// when nothing is loaded (what the agents were told in `load`/`play`:
    /// the file name minus `.duckshow.json`).
    public var currentShowID: String? {
        show == nil ? nil : showID
    }

    /// The roster of the last `load()`/`connect()`, keyed by duck id —
    /// which cast role each dialed duck is standing in for.
    public var currentRoster: [DuckID: RosterEntry] {
        roster
    }

    /// Number of live `telemetryEvents()` subscribers (test hook).
    var telemetrySubscriberCount: Int {
        telemetryContinuations.count
    }

    /// Master's own show-time estimate right now: `nil` while stopped, the
    /// cued start position while armed (the lead time is still running),
    /// the running clock while playing.
    public func currentShowTime() -> Double? {
        clock.showTime() ?? (transport == .armed ? armedFromShowTime : nil)
    }

    /// Transport, show, show clock, roster, telemetry and lost set read at
    /// one instant — the OSC facade's full status push is built from this
    /// so a `load` landing between separate reads can never produce a push
    /// describing a state the master was never in.
    public func statusSnapshot() -> SwarmStatusSnapshot {
        SwarmStatusSnapshot(
            transport: transport, showID: currentShowID, showTime: currentShowTime(),
            roster: roster, telemetry: telemetryStore, lostDucks: lostDucks
        )
    }

    // MARK: Playback epoch

    private func beginPlaying(generation: Int, atMasterTime: Int64, fromShowTime: Double) {
        // Only the most recent play() may establish the epoch; a seek(),
        // second play(), stop() or panic() issued during the lead time
        // bumped playGeneration (and/or left .armed) and wins.
        guard generation == playGeneration, transport == .armed else { return }
        clock.play(at: atMasterTime, fromShowTime: fromShowTime)
        armedFromShowTime = nil
        transport = .playing
    }

    private func haltTransport() {
        transport = .stopped
        clock.stop()
        cueShowTime = 0
        armedFromShowTime = nil
        playGeneration += 1
        stopStateLoop()
    }

    /// Takes the next command ticket (see `issuedCommands`).
    private func issueCommand() -> Int {
        issuedCommands += 1
        return issuedCommands
    }

    /// The outcome of a `load` that gave up because a newer command was
    /// issued while it was suspended.
    private static func supersededOutcomes(for entries: [RosterEntry]) -> [DuckID: LoadOutcome] {
        var outcomes: [DuckID: LoadOutcome] = [:]
        for entry in entries { outcomes[entry.id] = LoadOutcome(status: .superseded) }
        return outcomes
    }

    /// `load()`/`connect()` while a show is armed/playing: send `stop` and
    /// wait for the ACKs/timeouts before the connections are touched. Part
    /// of the caller's command, so it takes no ticket of its own.
    private func stopIfTransportActive() async {
        guard transport != .stopped else { return }
        haltTransport()
        _ = await fanOut(.stop)
    }

    // MARK: Connection lifecycle

    /// Brings the dialed set in line with `entries` *without* hanging up on
    /// ducks whose host:port is unchanged.
    ///
    /// Never cancel-and-immediately-redial the same fixed local port → same
    /// peer: the fresh UDP `NWConnection` lands in `.waiting(EADDRINUSE)`
    /// and Network.framework never retries it (the cancelled socket is
    /// still closing), so the master would be deaf and mute to that duck
    /// for good. That was exactly what every `load()` after `connect()`
    /// did — the OSC facade's every show. Keeping the live flow also keeps
    /// the duck's watchdog state honest: a duck that was lost stays lost
    /// until it actually reports again.
    ///
    /// Deliberately supersedes nothing: a command still retrying to an
    /// unchanged duck (a `panic` that landed during the caller's stop-first
    /// phase, say) keeps its flow and its remaining attempts; a retry to a
    /// duck that left the roster ends as `connectionFailed` on its next
    /// attempt.
    private func rewire(entries: [RosterEntry]) {
        stopStateLoop()
        let now = MasterClock.nowNanoseconds()
        let incoming = entries.indexedByID()
        let ids = Set(incoming.keys)
        // Hang up on, and forget, ducks that are no longer on the roster...
        let known = Set(connections.keys).union(lastTelemetryNs.keys).union(telemetryStore.keys).union(neverReportedLost)
        for stale in known.subtracting(ids) {
            connections.removeValue(forKey: stale)?.cancel()
            lastTelemetryNs[stale] = nil
            telemetryStore[stale] = nil
            neverReportedLost.remove(stale)
        }
        for entry in entries {
            if let previous = roster[entry.id], previous.host == entry.host, previous.port == entry.port,
               connections[entry.id] != nil {
                continue // same duck at the same address: its flow carries on
            }
            // ...dial newcomers and ducks that moved (hanging up on the old
            // address first — a different 4-tuple, so the fresh dial cannot
            // collide with it), and (re)start their watchdog: a duck that
            // never reports after this point is `lost` after the threshold
            // even though it has no telemetry entry yet.
            connections[entry.id]?.cancel()
            let connection = makeConnection(for: entry)
            connections[entry.id] = connection
            lastTelemetryNs[entry.id] = now
            neverReportedLost.remove(entry.id)
            startReceiving(duckID: entry.id, connection: connection)
        }
        roster = incoming
        startHousekeepingIfNeeded()
    }

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
        let duckID = entry.id
        connection.stateUpdateHandler = { [weak self, weak connection] state in
            guard let self, let connection else { return }
            switch state {
            case .failed, .waiting:
                // `.waiting` is terminal in practice for a UDP dial that
                // could not bind (EADDRINUSE while a cancelled socket on
                // the same 4-tuple closes) — Network.framework only
                // re-evaluates it on a path change. Treat it like `.failed`.
                Task { await self.scheduleRedial(duckID: duckID, replacing: connection) }
            default:
                break
            }
        }
        connection.start(queue: networkQueue)
        return connection
    }

    /// A per-duck connection reported `.failed`, `.waiting` or a receive
    /// error: hang up on it *now* (so its socket, if it has one, is released
    /// during the backoff) and dial a fresh one after `redialBackoffNs`,
    /// provided it is still the one on file (a `load()`/`connect()` in
    /// between already replaced or cancelled it). Sends in the meantime
    /// fail quietly; every command is retried anyway.
    private func scheduleRedial(duckID: DuckID, replacing failed: NWConnection) {
        guard connections[duckID] === failed else { return }
        failed.cancel()
        Task { [weak self] in
            try? await Task.sleep(nanoseconds: self?.redialBackoffNs ?? 0)
            await self?.redial(duckID: duckID, replacing: failed)
        }
    }

    private func redial(duckID: DuckID, replacing failed: NWConnection) {
        guard connections[duckID] === failed, let entry = roster[duckID] else { return }
        let fresh = makeConnection(for: entry)
        connections[duckID] = fresh
        startReceiving(duckID: duckID, connection: fresh)
    }

    private func startReceiving(duckID: DuckID, connection: NWConnection) {
        connection.receiveMessage { [weak self] data, _, _, error in
            // Stamp arrival here, on the network queue, so actor scheduling
            // latency is not counted as network delay in time_resp.t1.
            let rxNs = MasterClock.nowNanoseconds()
            guard let self else { return }
            Task { await self.onReceive(duckID: duckID, connection: connection, data: data, error: error, rxNs: rxNs) }
        }
    }

    private func onReceive(duckID: DuckID, connection: NWConnection, data: Data?, error: NWError?, rxNs: Int64) {
        // Only keep pumping this connection's receive loop while it's still
        // the one on file for this duck (load() may have torn it down).
        guard connections[duckID] === connection else { return }
        if let data, !data.isEmpty {
            handleDatagram(data, from: duckID, rxNs: rxNs)
        }
        if error == nil {
            startReceiving(duckID: duckID, connection: connection)
        } else {
            // Receive errors accompany terminal conditions on a UDP
            // NWConnection; re-dial rather than go deaf to this duck for
            // the rest of the show.
            scheduleRedial(duckID: duckID, replacing: connection)
        }
    }

    private func send(_ data: Data, on connection: NWConnection) {
        connection.send(content: data, completion: .contentProcessed { _ in })
    }

    // MARK: Inbound message handling

    private func handleDatagram(_ data: Data, from duckID: DuckID, rxNs: Int64) {
        guard let envelope = SwarmMessage.decode(data) else { return }
        switch envelope {
        case .timeRequest(let request):
            respond(to: request, duckID: duckID, rxNs: rxNs)
        case .ack(let ack):
            resolveAck(ack, key: ackKey(cmdID: ack.cmdID, duck: duckID))
        case .telemetry(let telemetry):
            ingest(telemetry, duckID: duckID)
        case .timeResponse, .state, .cmd, .puppet:
            break // master never receives these; ignore per "unknown/unexpected is dropped"
        }
    }

    private func respond(to request: TimeRequest, duckID: DuckID, rxNs: Int64) {
        guard let connection = connections[duckID] else { return }
        let response = TimeResponse(t0: request.t0, t1: rxNs, t2: MasterClock.nowNanoseconds())
        guard let data = try? JSONEncoder().encode(response) else { return }
        send(data, on: connection)
    }

    private func ingest(_ message: TelemetryMessage, duckID: DuckID) {
        let now = MasterClock.nowNanoseconds()
        lastTelemetryNs[duckID] = now
        neverReportedLost.remove(duckID)
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
            puppet: message.puppet,
            relaxed: message.relaxed,
            lastSeenMasterNs: now,
            lost: false
        )
        telemetryStore[duckID] = entry
        publish(.updated(entry))
    }

    // MARK: Command fan-out

    private func ackKey(cmdID: String, duck: DuckID) -> String { "\(cmdID)|\(duck.raw)" }

    /// Starts a new command generation: every retry loop still waiting on
    /// an ACK is woken and abandons its remaining attempts, so the command
    /// being issued now is the one every duck ends up acting on.
    private func supersedeInFlightCommands() -> Int {
        commandGeneration += 1
        let waiting = pendingAcks
        pendingAcks.removeAll()
        for handler in waiting.values { handler(nil) }
        return commandGeneration
    }

    /// Sends `payload` to every connected duck (one fresh `cmd_id` shared by
    /// all of them) and returns once each duck's retry loop has ended.
    private func fanOut(_ payload: CommandMessage.Payload) async -> [DuckID: CommandStatus] {
        let generation = supersedeInFlightCommands()
        let message = CommandMessage(cmdID: UUID().uuidString, payload: payload)
        let targets = Array(connections.keys)
        var results: [DuckID: CommandStatus] = [:]
        await withTaskGroup(of: (DuckID, CommandStatus).self) { group in
            for duckID in targets {
                group.addTask {
                    (duckID, await self.sendWithRetry(message, to: duckID, generation: generation))
                }
            }
            for await (id, status) in group {
                results[id] = status
            }
        }
        return results
    }

    /// Sends `message` to `duckID`, retrying up to
    /// `SwarmLinkInfo.commandMaxAttempts` times at
    /// `SwarmLinkInfo.commandRetryIntervalMs` until an ACK/NACK arrives or
    /// a newer command supersedes this one. The same `cmd_id` is reused
    /// across every attempt, matching the protocol's "agents deduplicate
    /// by cmd_id" contract.
    private func sendWithRetry(_ message: CommandMessage, to duckID: DuckID, generation: Int) async -> CommandStatus {
        guard let data = try? JSONEncoder().encode(message) else {
            return .connectionFailed("failed to encode command")
        }
        let key = ackKey(cmdID: message.cmdID, duck: duckID)
        for _ in 1...SwarmLinkInfo.commandMaxAttempts {
            guard generation == commandGeneration else { return .superseded }
            // Look the connection up per attempt: a redial may have
            // replaced it since the last one.
            guard let connection = connections[duckID] else {
                return .connectionFailed("no connection for duck \(duckID)")
            }
            send(data, on: connection)
            if let ack = await waitForAck(key: key, timeoutMs: SwarmLinkInfo.commandRetryIntervalMs) {
                return ack.ok ? .ok : .nacked(ack.error ?? "nack")
            }
        }
        return generation == commandGeneration ? .timeout : .superseded
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
        stateLoopGeneration += 1
        let generation = stateLoopGeneration
        let intervalNs = UInt64(1_000_000_000.0 / SwarmLinkInfo.stateBroadcastHz)
        stateLoopTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                let shouldContinue = await self.publishStateTick()
                if !shouldContinue { break }
                do {
                    try await Task.sleep(nanoseconds: intervalNs)
                } catch {
                    break // cancelled: never spin on a throwing sleep
                }
            }
            await self?.finishStateLoop(generation: generation)
        }
    }

    private func stopStateLoop() {
        stateLoopTask?.cancel()
        stateLoopTask = nil
    }

    /// Only the loop that owns `stateLoopTask` may clear it — a loop that
    /// exits after a `play()` already started a newer one must not orphan
    /// that newer loop's handle.
    private func finishStateLoop(generation: Int) {
        if stateLoopGeneration == generation {
            stateLoopTask = nil
        }
    }

    /// Sends one `state` tick to every duck; returns whether the loop
    /// should keep running (only while armed/playing, per the API contract).
    /// Also where the show ends: once the master's show clock passes
    /// `meta.duration` the transport drops to `.stopped` (no `stop` is
    /// fanned out — the agents end playback themselves at that instant,
    /// docs/swarmlink-protocol.md §5, and a `stop` after a `seek` past the
    /// end would only race them).
    private func publishStateTick() -> Bool {
        // .paused belongs here: freezing the show must not also stop the 5 Hz
        // state stream, which is the only thing telling the operator the cast
        // is holding and where. This guard returning false is what tears the
        // loop down, and it is an == comparison the compiler will not flag
        // when a transport case is added.
        guard !Task.isCancelled,
              transport == .armed || transport == .playing || transport == .paused
        else { return false }
        if transport == .playing, let show, let showTime = clock.showTime(), showTime >= show.meta.duration {
            haltTransport()
            return false
        }
        stateSeq += 1
        let message = StateMessage(
            seq: stateSeq,
            show: showID,
            transport: transport,
            showTime: currentShowTime() ?? 0,
            masterTime: MasterClock.nowNanoseconds()
        )
        guard let data = try? JSONEncoder().encode(message) else { return true }
        for connection in connections.values { send(data, on: connection) }
        return true
    }

    // MARK: Housekeeping (lost watchdog)

    private func startHousekeepingIfNeeded() {
        guard housekeepingTask == nil else { return }
        let sweepIntervalNs = UInt64(min(1.0, telemetryLostThresholdSeconds / 2) * 1_000_000_000)
        housekeepingTask = Task { [weak self] in
            while !Task.isCancelled {
                do {
                    try await Task.sleep(nanoseconds: sweepIntervalNs)
                } catch {
                    return
                }
                guard let self else { return }
                await self.sweepLostDucks()
            }
        }
    }

    private func sweepLostDucks() {
        let now = MasterClock.nowNanoseconds()
        let thresholdNs = Int64(telemetryLostThresholdSeconds * 1_000_000_000)
        for (duckID, lastSeen) in lastTelemetryNs where now - lastSeen > thresholdNs {
            if var entry = telemetryStore[duckID] {
                guard !entry.lost else { continue }
                entry.lost = true
                telemetryStore[duckID] = entry
                publish(.lost(duckID))
            } else if !neverReportedLost.contains(duckID) {
                neverReportedLost.insert(duckID)
                publish(.lost(duckID))
            }
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

    private func removeTransportContinuation(_ id: UUID) {
        transportContinuations.removeValue(forKey: id)
    }
}
