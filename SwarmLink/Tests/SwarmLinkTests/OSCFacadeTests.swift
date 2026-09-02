import XCTest
@preconcurrency import Network
@testable import SwarmLink

// MARK: - In-test OSC client

/// A rig stand-in: one UDP flow to the facade, every reply decoded and
/// kept so tests can poll for what they expect (no fixed sleeps).
actor OSCTestClient {
    private let queue = DispatchQueue(label: "SwarmLinkTests.OSCTestClient")
    private let connection: NWConnection
    private(set) var received: [OSCMessage] = []
    private(set) var undecodable = 0

    init(port: UInt16) {
        connection = NWConnection(host: "127.0.0.1", port: NWEndpoint.Port(rawValue: port)!, using: .udp)
    }

    func start() async throws {
        let once = ResumeOnce()
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, any Error>) in
            connection.stateUpdateHandler = { state in
                switch state {
                case .ready: once.run { continuation.resume() }
                case .failed(let error): once.run { continuation.resume(throwing: error) }
                default: break
                }
            }
            connection.start(queue: queue)
        }
        receive()
    }

    func stop() {
        connection.cancel()
    }

    func send(_ message: OSCMessage) {
        sendRaw(message.encode())
    }

    func sendRaw(_ data: Data) {
        connection.send(content: data, completion: .contentProcessed { _ in })
    }

    func messages(at address: String) -> [OSCMessage] {
        received.filter { $0.address == address }
    }

    /// Polls until at least `count` messages at `address` satisfy
    /// `predicate`, or `timeoutMs` elapses; returns whatever matched.
    @discardableResult
    func waitFor(
        _ address: String, count: Int = 1, timeoutMs: Int = 3000,
        where predicate: @Sendable (OSCMessage) -> Bool = { _ in true }
    ) async -> [OSCMessage] {
        let deadline = Date().addingTimeInterval(Double(timeoutMs) / 1000)
        while Date() < deadline {
            let matching = messages(at: address).filter(predicate)
            if matching.count >= count { return matching }
            try? await Task.sleep(nanoseconds: 10_000_000)
        }
        return messages(at: address).filter(predicate)
    }

    private func receive() {
        connection.receiveMessage { [weak self] data, _, _, error in
            guard let self else { return }
            Task {
                if let data, !data.isEmpty { await self.record(data) }
                if error == nil { await self.receive() }
            }
        }
    }

    private func record(_ data: Data) {
        if let message = try? OSCCodec.decode(data) {
            received.append(message)
        } else {
            undecodable += 1
        }
    }
}

/// Guards a continuation against a second `.failed` after `.ready`.
private final class ResumeOnce: @unchecked Sendable {
    private let lock = NSLock()
    private var done = false

    func run(_ body: () -> Void) {
        lock.lock()
        defer { lock.unlock() }
        guard !done else { return }
        done = true
        body()
    }
}

/// Test clock for the pure subscriber registry.
private final class FakeClock: @unchecked Sendable {
    var seconds: Double = 0
}

// MARK: - Tests

/// The OSC facade over loopback: an in-test UDP client drives it, a
/// `TestDuck` stands in for the roster duck so `SwarmMaster` really loads,
/// plays, and panics, and every reply/push is checked against the shapes
/// in docs/osc-facade.md.
final class OSCFacadeTests: XCTestCase {
    private var tmpDir: URL?
    private let duck01 = DuckID("duck-01")

    override func tearDown() {
        if let tmpDir { try? FileManager.default.removeItem(at: tmpDir) }
        tmpDir = nil
        super.tearDown()
    }

    struct Rig {
        let master: SwarmMaster
        let facade: OSCFacade
        let duck: TestDuck
        let client: OSCTestClient
        let showsDir: URL
        let port: UInt16

        func close() async {
            await client.stop()
            await facade.stop()
            await duck.stop()
        }
    }

    /// Facade on an ephemeral port (no Bonjour), master on a fixed random
    /// port — the production shape, where `start()`'s connect followed by
    /// every `/duckswarm/load` re-uses one local port for the same peers —
    /// one TestDuck on the roster, `fixture.duckshow.json` in the shows
    /// dir. Idle pushes are sped up so tests see periodic feedback without
    /// waiting the production 2 s.
    private func rig(
        idleStatusHz: Double = 5, subscriberTTL: Double = 5, showDuration: Double = 5.0,
        lostThreshold: Double = SwarmLinkInfo.telemetryLostThresholdSeconds
    ) async throws -> Rig {
        let duck = TestDuck()
        let duckPort = try await duck.start()
        let (dir, _) = try Fixtures.writeShow(named: "fixture", roles: ["lead"], duration: showDuration)
        tmpDir = dir
        let roster = try Fixtures.writeRoster([RosterEntry(id: "duck-01", host: "127.0.0.1", port: duckPort, role: "lead")], in: dir)
        let masterPort = Fixtures.randomMasterPort()
        let master = SwarmMaster(masterPort: masterPort, telemetryLostThresholdSeconds: lostThreshold)
        let configuration = OSCFacadeConfiguration(
            rosterURL: roster, showsDirectory: dir, oscPort: 0, masterPort: masterPort, advertiseBonjour: false,
            defaultLeadSeconds: 0.05, subscriberTTLSeconds: subscriberTTL, activeStatusHz: 10, idleStatusHz: idleStatusHz
        )
        let facade = OSCFacade(master: master, configuration: configuration)
        let port = try await facade.start()
        XCTAssertNotEqual(port, 0)
        let client = OSCTestClient(port: port)
        try await client.start()
        return Rig(master: master, facade: facade, duck: duck, client: client, showsDir: dir, port: port)
    }

    /// A second rig-side peer on its own UDP socket (a second desk).
    private func extraClient(for rig: Rig) async throws -> OSCTestClient {
        let client = OSCTestClient(port: rig.port)
        try await client.start()
        return client
    }

    private func duckLine(state: String) -> @Sendable (OSCMessage) -> Bool {
        { $0.args.count == 6 && $0.args[0] == .string("duck-01") && $0.args[2] == .string(state) }
    }

    private func ack(_ command: String) -> @Sendable (OSCMessage) -> Bool {
        { $0.args.first == .string(command) }
    }

    private func transport(_ state: String) -> @Sendable (OSCMessage) -> Bool {
        { $0.args == [.string(state)] }
    }

    // MARK: ping / status

    func testPingSubscribesAndReceivesFullStatusThenPeriodicPushes() async throws {
        let rig = try await rig(idleStatusHz: 5)
        await rig.client.send(OSCMessage(address: "/duckswarm/ping"))

        let transports = await rig.client.waitFor("/duckswarm/status/transport")
        XCTAssertEqual(transports.first?.args, [.string("stopped")])
        let shows = await rig.client.waitFor("/duckswarm/status/show")
        XCTAssertEqual(shows.first?.args, [.string("")])
        let showTimes = await rig.client.waitFor("/duckswarm/status/show_time")
        XCTAssertEqual(showTimes.first?.args, [.float32(0)])
        let summaries = await rig.client.waitFor("/duckswarm/status/summary")
        XCTAssertEqual(summaries.first?.args, [.int32(1), .int32(0), .int32(1)], "roster 1, nobody reporting yet, so 1 lost")
        let ducks = await rig.client.waitFor("/duckswarm/status/duck")
        XCTAssertEqual(ducks.first?.args, [
            .string("duck-01"), .string("lead"), .string("lost"), .float32(0), .float32(-1.0), .int32(0)
        ])

        // Subscribed: periodic pushes keep coming at the idle rate.
        let periodic = await rig.client.waitFor("/duckswarm/status/transport", count: 3, timeoutMs: 3000)
        XCTAssertGreaterThanOrEqual(periodic.count, 3)
        let subscribers = await rig.facade.subscriberCount
        XCTAssertEqual(subscribers, 1)
        let errors = await rig.client.messages(at: "/duckswarm/error")
        XCTAssertTrue(errors.isEmpty)
        await rig.close()
    }

    func testStatusPushesOnceWithoutSubscribing() async throws {
        let rig = try await rig(idleStatusHz: 10)
        await rig.client.send(OSCMessage(address: "/duckswarm/status"))
        let first = await rig.client.waitFor("/duckswarm/status/transport")
        XCTAssertEqual(first.count, 1)
        let ducks = await rig.client.waitFor("/duckswarm/status/duck")
        XCTAssertEqual(ducks.count, 1)

        // A subscriber would see ~6 more pushes in this window; a plain
        // /status sender sees none.
        await Task.sleepMs(600)
        let later = await rig.client.messages(at: "/duckswarm/status/transport")
        XCTAssertEqual(later.count, 1, "/duckswarm/status must not subscribe the sender")
        let subscribers = await rig.facade.subscriberCount
        XCTAssertEqual(subscribers, 0)
        await rig.close()
    }

    func testSubscriptionExpiresWithoutRePing() async throws {
        let rig = try await rig(idleStatusHz: 10, subscriberTTL: 0.3)
        await rig.client.send(OSCMessage(address: "/duckswarm/ping"))
        let pushes = await rig.client.waitFor("/duckswarm/status/transport", count: 2)
        XCTAssertGreaterThanOrEqual(pushes.count, 2, "subscribed: immediate push + periodic")

        await Task.sleepMs(500) // well past the 0.3 s TTL
        let atExpiry = await rig.client.messages(at: "/duckswarm/status/transport").count
        await Task.sleepMs(400)
        let afterExpiry = await rig.client.messages(at: "/duckswarm/status/transport").count
        XCTAssertEqual(afterExpiry, atExpiry, "no pushes after the subscription expired")
        let subscribers = await rig.facade.subscriberCount
        XCTAssertEqual(subscribers, 0)

        // Re-ping renews (and, having expired, counts as new → immediate push).
        await rig.client.send(OSCMessage(address: "/duckswarm/ping"))
        let renewed = await rig.client.waitFor("/duckswarm/status/transport", count: afterExpiry + 1)
        XCTAssertGreaterThanOrEqual(renewed.count, afterExpiry + 1)
        await rig.close()
    }

    // MARK: load

    func testLoadOfUnknownShowRepliesError() async throws {
        let rig = try await rig()
        await rig.client.send(OSCMessage(address: "/duckswarm/load", args: [.string("nope")]))
        let errors = await rig.client.waitFor("/duckswarm/error")
        guard case .string(let message)? = errors.first?.args.first else { return XCTFail("\(errors)") }
        XCTAssertTrue(message.contains("nope"), message)
        XCTAssertTrue(message.contains("not found"), message)
        let loads = await rig.duck.commands(named: "load")
        XCTAssertTrue(loads.isEmpty, "nothing may be sent to the ducks for an unknown show")
        await rig.close()
    }

    func testLoadRejectsPathTraversalAndWrongArgType() async throws {
        let rig = try await rig()
        await rig.client.send(OSCMessage(address: "/duckswarm/load", args: [.string("../fixture")]))
        let traversal = await rig.client.waitFor("/duckswarm/error")
        XCTAssertEqual(traversal.count, 1)
        await rig.client.send(OSCMessage(address: "/duckswarm/load", args: [.int32(1)]))
        let wrongType = await rig.client.waitFor("/duckswarm/error", count: 2)
        guard case .string(let message)? = wrongType.last?.args.first else { return XCTFail("\(wrongType)") }
        XCTAssertTrue(message.contains("string"), message)
        await rig.client.send(OSCMessage(address: "/duckswarm/load"))
        let missing = await rig.client.waitFor("/duckswarm/error", count: 3)
        XCTAssertEqual(missing.count, 3)
        let loads = await rig.duck.commands(named: "load")
        XCTAssertTrue(loads.isEmpty)
        await rig.close()
    }

    // MARK: load → play (int lead, extra args) → stop

    func testLoadPlayStopFlowWithLenientArgs() async throws {
        let rig = try await rig(idleStatusHz: 5)
        await rig.client.send(OSCMessage(address: "/duckswarm/load", args: [.string("fixture")]))
        let loadAcks = await rig.client.waitFor("/duckswarm/ack", where: ack("load"))
        XCTAssertEqual(loadAcks.first?.args, [.string("load"), .string("duck-01"), .int32(1), .string("")])
        let shows = await rig.client.waitFor("/duckswarm/status/show", where: { $0.args == [.string("fixture")] })
        XCTAssertEqual(shows.count, 1, "the sender of a command receives the status push even without a ping")
        let loadedID = await rig.master.currentShowID
        XCTAssertEqual(loadedID, "fixture")

        // The duck has heard from the master now, so it can report in;
        // the per-duck status line must carry its telemetry verbatim.
        await rig.client.send(OSCMessage(address: "/duckswarm/ping"))
        try await rig.duck.sendTelemetry(state: .loaded, show: "fixture", showTime: 0)
        let reporting = await rig.client.waitFor("/duckswarm/status/duck", where: { $0.args.count == 6 && $0.args[2] == .string("loaded") })
        XCTAssertEqual(reporting.first?.args, [
            .string("duck-01"), .string("lead"), .string("loaded"), .float32(0), .float32(0.5), .int32(1)
        ])
        let summaries = await rig.client.waitFor("/duckswarm/status/summary", where: { $0.args == [.int32(1), .int32(1), .int32(0)] })
        XCTAssertEqual(summaries.count, 1)

        // `i` where `f` is expected, plus an extra argument to ignore.
        await rig.client.send(OSCMessage(address: "/duckswarm/play", args: [.int32(0), .string("ignored")]))
        let playAcks = await rig.client.waitFor("/duckswarm/ack", where: ack("play"))
        XCTAssertEqual(playAcks.first?.args, [.string("play"), .string("duck-01"), .int32(1), .string("")])
        let playing = await rig.client.waitFor("/duckswarm/status/transport", where: transport("playing"))
        XCTAssertGreaterThanOrEqual(playing.count, 1)
        let plays = await rig.duck.commands(named: "play")
        XCTAssertEqual(plays.count, 1)
        let masterTransport = await rig.master.currentTransport
        XCTAssertEqual(masterTransport, .playing)
        let showTimes = await rig.client.waitFor("/duckswarm/status/show_time", where: {
            if case .float32(let t)? = $0.args.first { return t > 0 }
            return false
        })
        XCTAssertGreaterThanOrEqual(showTimes.count, 1, "show_time advances while playing")

        // The load ack, the ping and the idle pushes already delivered
        // several `stopped` lines: only a push *after* the stop counts.
        let stoppedBefore = await rig.client.messages(at: "/duckswarm/status/transport").filter(transport("stopped")).count
        await rig.client.send(OSCMessage(address: "/duckswarm/stop", args: [.true, .int32(5)]))
        let stopAcks = await rig.client.waitFor("/duckswarm/ack", where: ack("stop"))
        XCTAssertEqual(stopAcks.first?.args, [.string("stop"), .string("duck-01"), .int32(1), .string("")])
        let stoppedAgain = await rig.client.waitFor("/duckswarm/status/transport", count: stoppedBefore + 1, where: transport("stopped"))
        XCTAssertGreaterThanOrEqual(stoppedAgain.count, stoppedBefore + 1, "the stop must push transport stopped")
        await Task.sleepMs(150)
        let lastShowTime = await rig.client.messages(at: "/duckswarm/status/show_time").last?.args
        XCTAssertEqual(lastShowTime, [.float32(0)], "show_time is 0.0 once stopped")
        let errors = await rig.client.messages(at: "/duckswarm/error")
        XCTAssertTrue(errors.isEmpty, "\(errors)")
        await rig.close()
    }

    /// A rig's `/load` immediately followed by `/go` (a cue group with no
    /// pre-wait) while the previous show is still being stopped: the GO
    /// must be refused, not arm the *previous* show behind the load.
    func testGoDuringALoadIsRefusedInsteadOfArmingThePreviousShow() async throws {
        let rig = try await rig()
        await rig.client.send(OSCMessage(address: "/duckswarm/load", args: [.string("fixture")]))
        await rig.client.waitFor("/duckswarm/ack", where: ack("load"))
        await rig.client.send(OSCMessage(address: "/duckswarm/play", args: [.int32(0)]))
        let playing = await rig.client.waitFor("/duckswarm/status/transport", where: transport("playing"))
        XCTAssertGreaterThanOrEqual(playing.count, 1)

        // The reload's stop-first phase now takes ≥ 300 ms (a slow duck).
        await rig.duck.setDropFirst(3, of: "stop")
        await rig.client.send(OSCMessage(address: "/duckswarm/load", args: [.string("fixture")]))
        await rig.client.send(OSCMessage(address: "/duckswarm/go"))
        let errors = await rig.client.waitFor("/duckswarm/error")
        XCTAssertEqual(errors.first?.args, [.string("load in progress")])
        let loadAcks = await rig.client.waitFor("/duckswarm/ack", count: 2, where: ack("load"))
        XCTAssertEqual(loadAcks.last?.args, [.string("load"), .string("duck-01"), .int32(1), .string("")])
        let plays = await rig.duck.commands(named: "play")
        XCTAssertEqual(plays.count, 1, "the GO must not arm the previous show mid-load")
        let masterTransport = await rig.master.currentTransport
        XCTAssertEqual(masterTransport, .stopped)
        await rig.close()
    }

    func testGoIsPlayWithDefaultLead() async throws {
        let rig = try await rig()
        // Subscribe: the armed → playing transition after the lead time is
        // a transport change pushed to subscribers, not a command reply.
        await rig.client.send(OSCMessage(address: "/duckswarm/ping"))
        await rig.client.waitFor("/duckswarm/status/transport")
        await rig.client.send(OSCMessage(address: "/duckswarm/load", args: [.string("fixture")]))
        await rig.client.waitFor("/duckswarm/ack", where: ack("load"))
        let before = MasterClock.nowNanoseconds()
        await rig.client.send(OSCMessage(address: "/duckswarm/go"))
        let playAcks = await rig.client.waitFor("/duckswarm/ack", where: ack("play"))
        XCTAssertEqual(playAcks.first?.args, [.string("play"), .string("duck-01"), .int32(1), .string("")])
        let armed = await rig.client.waitFor("/duckswarm/status/transport", where: transport("armed"))
        XCTAssertGreaterThanOrEqual(armed.count, 1, "the ack-time status shows the lead time still running")
        let playing = await rig.client.waitFor("/duckswarm/status/transport", where: transport("playing"))
        XCTAssertGreaterThanOrEqual(playing.count, 1)
        let plays = await rig.duck.commands(named: "play")
        guard case .play(let id, let atMasterTime, let fromShowTime)? = plays.first?.payload else { return XCTFail("\(plays)") }
        XCTAssertEqual(id, "fixture")
        XCTAssertEqual(fromShowTime, 0.0)
        XCTAssertGreaterThanOrEqual(atMasterTime - before, 50_000_000, "go uses the configured default lead (50 ms here)")
        _ = await rig.master.stop()
        await rig.close()
    }

    func testPlayWithoutLoadRepliesNoShowLoaded() async throws {
        let rig = try await rig()
        await rig.client.send(OSCMessage(address: "/duckswarm/play", args: [.float32(0.5)]))
        let errors = await rig.client.waitFor("/duckswarm/error")
        XCTAssertEqual(errors.first?.args, [.string("no show loaded")])
        await rig.client.send(OSCMessage(address: "/duckswarm/go"))
        let again = await rig.client.waitFor("/duckswarm/error", count: 2)
        XCTAssertEqual(again.last?.args, [.string("no show loaded")])
        let plays = await rig.duck.commands(named: "play")
        XCTAssertTrue(plays.isEmpty)
        await rig.close()
    }

    func testPlayRejectsNonNumericLead() async throws {
        let rig = try await rig()
        await rig.client.send(OSCMessage(address: "/duckswarm/load", args: [.string("fixture")]))
        await rig.client.waitFor("/duckswarm/ack", where: ack("load"))
        await rig.client.send(OSCMessage(address: "/duckswarm/play", args: [.string("1.5")]))
        let errors = await rig.client.waitFor("/duckswarm/error")
        XCTAssertEqual(errors.count, 1)
        let plays = await rig.duck.commands(named: "play")
        XCTAssertTrue(plays.isEmpty, "a wrong-typed required arg yields an error and no action")
        let masterTransport = await rig.master.currentTransport
        XCTAssertEqual(masterTransport, .stopped)
        await rig.close()
    }

    // MARK: seek

    func testSeekWrongTypeIsRejectedWithoutSendingAndFloatSeekIsAcked() async throws {
        let rig = try await rig()
        await rig.client.send(OSCMessage(address: "/duckswarm/seek", args: [.string("abc")]))
        let errors = await rig.client.waitFor("/duckswarm/error")
        XCTAssertEqual(errors.count, 1)
        let seeksAfterError = await rig.duck.commands(named: "seek")
        XCTAssertTrue(seeksAfterError.isEmpty)

        await rig.client.send(OSCMessage(address: "/duckswarm/seek", args: [.float32(3.0)]))
        let seekAcks = await rig.client.waitFor("/duckswarm/ack", where: ack("seek"))
        XCTAssertEqual(seekAcks.first?.args, [.string("seek"), .string("duck-01"), .int32(1), .string("")])
        let seeks = await rig.duck.commands(named: "seek")
        guard case .seek(let showTime, _)? = seeks.first?.payload else { return XCTFail("\(seeks)") }
        XCTAssertEqual(showTime, 3.0)

        // Int leniency for seek too.
        await rig.client.send(OSCMessage(address: "/duckswarm/seek", args: [.int32(7)]))
        let secondSeek = await rig.duck.waitForCommands(named: "seek", count: 2)
        guard case .seek(let secondTime, _)? = secondSeek.last?.payload else { return XCTFail("\(secondSeek)") }
        XCTAssertEqual(secondTime, 7.0)
        await rig.close()
    }

    // MARK: panic

    func testPanicFromPlayingReachesDucksAndStopsMaster() async throws {
        let rig = try await rig()
        await rig.client.send(OSCMessage(address: "/duckswarm/load", args: [.string("fixture")]))
        await rig.client.waitFor("/duckswarm/ack", where: ack("load"))
        await rig.client.send(OSCMessage(address: "/duckswarm/play", args: [.int32(0)]))
        let playing = await rig.client.waitFor("/duckswarm/status/transport", where: transport("playing"))
        XCTAssertGreaterThanOrEqual(playing.count, 1)

        await rig.client.send(OSCMessage(address: "/duckswarm/panic"))
        let panics = await rig.duck.waitForCommands(named: "panic")
        XCTAssertEqual(panics.count, 1)
        let panicAcks = await rig.client.waitFor("/duckswarm/ack", where: ack("panic"))
        XCTAssertEqual(panicAcks.first?.args, [.string("panic"), .string("duck-01"), .int32(1), .string("")])
        let masterTransport = await rig.master.currentTransport
        XCTAssertEqual(masterTransport, .stopped)
        let showTime = await rig.master.currentShowTime()
        XCTAssertNil(showTime)
        let stopped = await rig.client.waitFor("/duckswarm/status/transport", where: transport("stopped"))
        XCTAssertGreaterThanOrEqual(stopped.count, 1)
        await rig.close()
    }

    /// docs/osc-facade.md: transport is `stopped | armed | playing` and
    /// show_time is 0.0 when stopped. The agents end the show themselves at
    /// `meta.duration`; the facade must push the master's matching
    /// `stopped` rather than report `playing` until someone sends /stop.
    func testTransportStoppedIsPushedWhenTheShowEnds() async throws {
        let rig = try await rig(idleStatusHz: 5, showDuration: 0.3)
        await rig.client.send(OSCMessage(address: "/duckswarm/ping"))
        await rig.client.waitFor("/duckswarm/status/transport")
        await rig.client.send(OSCMessage(address: "/duckswarm/load", args: [.string("fixture")]))
        let loadAcks = await rig.client.waitFor("/duckswarm/ack", where: ack("load"))
        XCTAssertEqual(loadAcks.first?.args, [.string("load"), .string("duck-01"), .int32(1), .string("")])
        await rig.client.send(OSCMessage(address: "/duckswarm/play", args: [.float32(0.05)]))
        let playing = await rig.client.waitFor("/duckswarm/status/transport", where: transport("playing"))
        XCTAssertFalse(playing.isEmpty)

        let stoppedBefore = await rig.client.messages(at: "/duckswarm/status/transport").filter(transport("stopped")).count
        let stoppedAfter = await rig.client.waitFor(
            "/duckswarm/status/transport", count: stoppedBefore + 1, timeoutMs: 2000, where: transport("stopped"))
        XCTAssertGreaterThanOrEqual(stoppedAfter.count, stoppedBefore + 1, "the end of the show must be pushed as transport stopped")
        await Task.sleepMs(150)
        let lastShowTime = await rig.client.messages(at: "/duckswarm/status/show_time").last?.args
        XCTAssertEqual(lastShowTime, [.float32(0)], "show_time is 0.0 once stopped")
        let stops = await rig.duck.commands(named: "stop")
        XCTAssertTrue(stops.isEmpty, "no stop is fanned out at the end of the show")
        let masterTransport = await rig.master.currentTransport
        XCTAssertEqual(masterTransport, .stopped)
        let errors = await rig.client.messages(at: "/duckswarm/error")
        XCTAssertTrue(errors.isEmpty)
        await rig.close()
    }

    func testPanicWorksBeforeAnyLoad() async throws {
        let rig = try await rig()
        await rig.client.send(OSCMessage(address: "/duckswarm/panic"))
        let panics = await rig.duck.waitForCommands(named: "panic")
        XCTAssertEqual(panics.count, 1, "panic needs only the roster the facade dialed at start")
        let panicAcks = await rig.client.waitFor("/duckswarm/ack", where: ack("panic"))
        XCTAssertEqual(panicAcks.first?.args, [.string("panic"), .string("duck-01"), .int32(1), .string("")])
        await rig.close()
    }

    // MARK: acks carry NACK reasons

    func testAckReportsNackReason() async throws {
        let rig = try await rig()
        await rig.duck.setNack("stop", error: "not playing")
        await rig.client.send(OSCMessage(address: "/duckswarm/stop"))
        let acks = await rig.client.waitFor("/duckswarm/ack", where: ack("stop"))
        XCTAssertEqual(acks.first?.args, [.string("stop"), .string("duck-01"), .int32(0), .string("not playing")])
        await rig.close()
    }

    /// A rig that bundles by default (TouchDesigner's OSC Out, python-osc
    /// bundle builders, cue systems that bundle a fire with a fader) must
    /// still be able to panic the flock: panic always works.
    func testBundledPanicReachesTheDucks() async throws {
        let rig = try await rig()
        let panic = OSCMessage(address: "/duckswarm/panic").encode()
        let packet = Data("#bundle\0".utf8) + Data([0, 0, 0, 0, 0, 0, 0, 1]) + Data([0, 0, 0, UInt8(panic.count)]) + panic
        await rig.client.sendRaw(packet)
        let panics = await rig.duck.waitForCommands(named: "panic")
        XCTAssertEqual(panics.count, 1)
        let acks = await rig.client.waitFor("/duckswarm/ack", where: ack("panic"))
        XCTAssertEqual(acks.first?.args, [.string("panic"), .string("duck-01"), .int32(1), .string("")])
        await rig.close()
    }

    // MARK: flows: a sender is never refused

    /// Every one-shot sender (an `osc_send.py` invocation, a readiness
    /// poll, a restarted rig) is a new UDP flow, and UDP has no close. Past
    /// the tracked-flow cap the oldest idle flow makes room: the newcomer
    /// is always heard — including a panic from a socket never seen before.
    func testNewSendersAreNeverRefusedPastTheFlowCap() async throws {
        let rig = try await rig(idleStatusHz: 0.2) // TTL 5 s: no pruning helps within this test
        var clients: [OSCTestClient] = []
        for index in 0..<(OSCFacade.maxTrackedFlows + 6) {
            let client = try await extraClient(for: rig)
            clients.append(client)
            await client.send(OSCMessage(address: "/duckswarm/status"))
            let replies = await client.waitFor("/duckswarm/status/transport")
            XCTAssertEqual(replies.count, 1, "sender #\(index) must get its status reply")
        }
        let tracked = await rig.facade.trackedFlowCount
        XCTAssertLessThanOrEqual(tracked, OSCFacade.maxTrackedFlows)

        let late = try await extraClient(for: rig)
        await late.send(OSCMessage(address: "/duckswarm/panic"))
        let panics = await rig.duck.waitForCommands(named: "panic")
        XCTAssertEqual(panics.count, 1, "a panic from a never-seen socket must be heard after \(clients.count) other senders")
        let acks = await late.waitFor("/duckswarm/ack", where: ack("panic"))
        XCTAssertEqual(acks.first?.args, [.string("panic"), .string("duck-01"), .int32(1), .string("")])
        await late.stop()
        for client in clients { await client.stop() }
        await rig.close()
    }

    func testIdleFlowsArePrunedAndAReturningSenderIsHeard() async throws {
        let rig = try await rig(idleStatusHz: 10, subscriberTTL: 0.3)
        await rig.client.send(OSCMessage(address: "/duckswarm/status"))
        let first = await rig.client.waitFor("/duckswarm/status/transport")
        XCTAssertEqual(first.count, 1)
        var tracked = await rig.facade.trackedFlowCount
        XCTAssertEqual(tracked, 1)

        for _ in 0..<150 where tracked > 0 {
            await Task.sleepMs(10)
            tracked = await rig.facade.trackedFlowCount
        }
        XCTAssertEqual(tracked, 0, "a one-shot sender's flow is hung up on once it has been silent for the TTL")

        await rig.client.send(OSCMessage(address: "/duckswarm/status"))
        let second = await rig.client.waitFor("/duckswarm/status/transport", count: 2)
        XCTAssertEqual(second.count, 2, "the same socket talking again gets a fresh flow and its reply")
        let again = await rig.facade.trackedFlowCount
        XCTAssertEqual(again, 1)
        await rig.close()
    }

    // MARK: quiesce (swarmctl serve's shutdown)

    func testQuiesceRefusesArmingCommandsButPanicAndStatusStillWork() async throws {
        let rig = try await rig()
        await rig.client.send(OSCMessage(address: "/duckswarm/load", args: [.string("fixture")]))
        await rig.client.waitFor("/duckswarm/ack", where: ack("load"))
        await rig.facade.quiesce()

        await rig.client.send(OSCMessage(address: "/duckswarm/go"))
        let errors = await rig.client.waitFor("/duckswarm/error")
        XCTAssertEqual(errors.first?.args, [.string("shutting down")])
        let plays = await rig.duck.commands(named: "play")
        XCTAssertTrue(plays.isEmpty, "nothing may arm the flock once the owner is shutting down")
        let masterTransport = await rig.master.currentTransport
        XCTAssertEqual(masterTransport, .stopped)

        await rig.client.send(OSCMessage(address: "/duckswarm/panic"))
        let panics = await rig.duck.waitForCommands(named: "panic")
        XCTAssertEqual(panics.count, 1, "panic always works")
        await rig.client.send(OSCMessage(address: "/duckswarm/status"))
        let status = await rig.client.waitFor("/duckswarm/status/transport", count: 2)
        XCTAssertGreaterThanOrEqual(status.count, 2, "feedback keeps flowing while quiescing")
        await rig.close()
    }

    // MARK: routing across peers

    /// docs/osc-facade.md: `/duckswarm/error` goes only to the offending
    /// sender; acks and status go to every live subscriber plus the sender.
    func testErrorsGoOnlyToTheSenderWhileAcksReachSubscribers() async throws {
        let rig = try await rig()
        let subscriber = rig.client
        await subscriber.send(OSCMessage(address: "/duckswarm/ping"))
        await subscriber.waitFor("/duckswarm/status/transport")
        let desk = try await extraClient(for: rig)

        await desk.send(OSCMessage(address: "/duckswarm/load", args: [.string("nope")]))
        let deskErrors = await desk.waitFor("/duckswarm/error")
        XCTAssertEqual(deskErrors.count, 1)

        await desk.send(OSCMessage(address: "/duckswarm/load", args: [.string("fixture")]))
        let deskAcks = await desk.waitFor("/duckswarm/ack", where: ack("load"))
        XCTAssertEqual(deskAcks.first?.args, [.string("load"), .string("duck-01"), .int32(1), .string("")])
        let subscriberAcks = await subscriber.waitFor("/duckswarm/ack", where: ack("load"))
        XCTAssertEqual(subscriberAcks.first?.args, [.string("load"), .string("duck-01"), .int32(1), .string("")], "a subscriber sees another sender's acks")
        let subscriberShows = await subscriber.waitFor("/duckswarm/status/show", where: { $0.args == [.string("fixture")] })
        XCTAssertGreaterThanOrEqual(subscriberShows.count, 1)
        await Task.sleepMs(100)
        let subscriberErrors = await subscriber.messages(at: "/duckswarm/error")
        XCTAssertTrue(subscriberErrors.isEmpty, "a bystander subscriber must never see another sender's /duckswarm/error")
        let subscribers = await rig.facade.subscriberCount
        XCTAssertEqual(subscribers, 1, "a command sender is not subscribed by sending commands")
        await desk.stop()
        await rig.close()
    }

    // MARK: per-duck status mapping

    /// docs/osc-facade.md line 45 and docs/swarmlink-protocol.md §4: a null
    /// clock offset is −1.0 on the wire (never 0), policies_ok maps 1/0,
    /// agent states pass through, and a duck that reported and then went
    /// silent flips to `lost` with the summary counting it.
    func testDuckStatusLineMapsUnsyncedClockFailedPoliciesAndStaleTelemetry() async throws {
        let rig = try await rig(idleStatusHz: 10, lostThreshold: 0.3)
        await rig.client.send(OSCMessage(address: "/duckswarm/load", args: [.string("fixture")]))
        await rig.client.waitFor("/duckswarm/ack", where: ack("load"))
        await rig.client.send(OSCMessage(address: "/duckswarm/ping"))

        try await rig.duck.sendTelemetry(state: .degraded, show: "fixture", showTime: 2.5, clockOffsetMs: nil, clockRttMs: nil, policiesOk: false)
        let degraded = await rig.client.waitFor("/duckswarm/status/duck", where: duckLine(state: "degraded"))
        XCTAssertEqual(degraded.first?.args, [
            .string("duck-01"), .string("lead"), .string("degraded"), .float32(2.5), .float32(-1.0), .int32(0)
        ], "null offset → −1.0, policies_ok false → 0, state verbatim")
        let reporting = await rig.client.waitFor("/duckswarm/status/summary", where: { $0.args == [.int32(1), .int32(1), .int32(0)] })
        XCTAssertGreaterThanOrEqual(reporting.count, 1)

        // Silence past the lost threshold: the line flips back to lost.
        let lostBefore = await rig.client.messages(at: "/duckswarm/status/duck").filter(duckLine(state: "lost")).count
        let lostAfter = await rig.client.waitFor("/duckswarm/status/duck", count: lostBefore + 1, timeoutMs: 3000, where: duckLine(state: "lost"))
        XCTAssertGreaterThanOrEqual(lostAfter.count, lostBefore + 1, "a duck whose telemetry went stale is lost on the wire")
        XCTAssertEqual(lostAfter.last?.args, [.string("duck-01"), .string("lead"), .string("lost"), .float32(0), .float32(-1.0), .int32(0)])
        let lostSummary = await rig.client.waitFor("/duckswarm/status/summary", count: 1, where: { $0.args == [.int32(1), .int32(0), .int32(1)] })
        XCTAssertGreaterThanOrEqual(lostSummary.count, 1)

        // Reporting again clears it; a fault passes through with its offset.
        try await rig.duck.sendTelemetry(state: .fault, show: "fixture", showTime: 0, clockOffsetMs: 1.25, policiesOk: true)
        let fault = await rig.client.waitFor("/duckswarm/status/duck", where: duckLine(state: "fault"))
        XCTAssertEqual(fault.first?.args, [.string("duck-01"), .string("lead"), .string("fault"), .float32(0), .float32(1.25), .int32(1)])
        await rig.close()
    }

    // MARK: cadence

    /// docs/osc-facade.md: pushes at the active rate while armed/playing
    /// and the idle rate otherwise (10 Hz / 1 Hz here, 2 Hz / 0.5 Hz in
    /// production).
    func testStatusCadenceFollowsTheTransport() async throws {
        let rig = try await rig(idleStatusHz: 1)
        await rig.client.send(OSCMessage(address: "/duckswarm/ping"))
        await rig.client.waitFor("/duckswarm/status/transport")
        let idleStart = await rig.client.messages(at: "/duckswarm/status/transport").count
        await Task.sleepMs(650)
        let idlePushes = await rig.client.messages(at: "/duckswarm/status/transport").count - idleStart
        XCTAssertLessThanOrEqual(idlePushes, 1, "stopped: ~1 Hz, got \(idlePushes) in 650 ms")

        await rig.client.send(OSCMessage(address: "/duckswarm/load", args: [.string("fixture")]))
        await rig.client.waitFor("/duckswarm/ack", where: ack("load"))
        await rig.client.send(OSCMessage(address: "/duckswarm/play", args: [.int32(0)]))
        let playing = await rig.client.waitFor("/duckswarm/status/transport", where: transport("playing"))
        XCTAssertGreaterThanOrEqual(playing.count, 1)
        let activeStart = await rig.client.messages(at: "/duckswarm/status/transport").count
        await Task.sleepMs(650)
        let activePushes = await rig.client.messages(at: "/duckswarm/status/transport").count - activeStart
        XCTAssertGreaterThanOrEqual(activePushes, 4, "playing: ~10 Hz, got \(activePushes) in 650 ms")
        _ = await rig.master.stop()
        await rig.close()
    }

    // MARK: unknown addresses / garbage

    func testUnknownAddressesAndGarbageAreIgnored() async throws {
        let rig = try await rig()
        await rig.client.send(OSCMessage(address: "/duckswarm/bogus", args: [.int32(1)]))
        await rig.client.send(OSCMessage(address: "/stagewizard/go"))
        await rig.client.sendRaw(Data([0x01, 0x02, 0x03]))
        await rig.client.sendRaw(Data("#bundle\0".utf8) + Data(repeating: 0, count: 8)) // an empty bundle carries nothing
        await rig.client.sendRaw(Data("#bundle\0".utf8) + Data(repeating: 0, count: 4)) // truncated bundle header
        await rig.client.sendRaw(Data("/duckswarm/panic\0\0\0\0,d\0\0".utf8)) // unsupported tag → rejected whole

        // The facade is still alive and nothing reached the ducks.
        await rig.client.send(OSCMessage(address: "/duckswarm/ping"))
        let status = await rig.client.waitFor("/duckswarm/status/transport")
        XCTAssertEqual(status.first?.args, [.string("stopped")])
        let errors = await rig.client.messages(at: "/duckswarm/error")
        XCTAssertTrue(errors.isEmpty, "unknown addresses are ignored silently: \(errors)")
        let commands = await rig.duck.received
        XCTAssertTrue(commands.isEmpty)
        await rig.close()
    }

    // MARK: pure pieces

    func testSubscriberRegistryTouchRenewAndExpire() {
        let clock = FakeClock()
        var registry = OSCSubscriberRegistry<String>(ttl: 5) { clock.seconds }
        XCTAssertTrue(registry.touch("a"), "first contact is new")
        XCTAssertFalse(registry.touch("a"), "a re-ping renews, it is not new")
        clock.seconds = 3
        XCTAssertTrue(registry.touch("b"))
        XCTAssertEqual(registry.liveEndpoints(), ["a", "b"])
        clock.seconds = 5.5
        XCTAssertEqual(registry.liveEndpoints(), ["b"], "'a' last pinged at 0 → expired after 5 s")
        clock.seconds = 9
        XCTAssertEqual(registry.liveEndpoints(), [])
        XCTAssertTrue(registry.touch("a"), "a return after expiring counts as new again")
        XCTAssertEqual(registry.liveEndpoints(), ["a"])
    }

    func testResolveShowFlatThenNestedAndRefusesEscapes() throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: dir.appendingPathComponent("nested"), withIntermediateDirectories: true)
        tmpDir = dir
        try Data("{}".utf8).write(to: dir.appendingPathComponent("flat.duckshow.json"))
        try Data("{}".utf8).write(to: dir.appendingPathComponent("nested/nested.duckshow.json"))
        try Data("{}".utf8).write(to: dir.appendingPathComponent("both.duckshow.json"))
        try FileManager.default.createDirectory(at: dir.appendingPathComponent("both"), withIntermediateDirectories: true)
        try Data("{}".utf8).write(to: dir.appendingPathComponent("both/both.duckshow.json"))

        XCTAssertEqual(OSCFacade.resolveShow(id: "flat", in: dir)?.lastPathComponent, "flat.duckshow.json")
        XCTAssertEqual(OSCFacade.resolveShow(id: "nested", in: dir)?.path, dir.appendingPathComponent("nested/nested.duckshow.json").path)
        XCTAssertEqual(OSCFacade.resolveShow(id: "both", in: dir)?.path, dir.appendingPathComponent("both.duckshow.json").path, "flat wins, like the duck-agent")
        XCTAssertNil(OSCFacade.resolveShow(id: "missing", in: dir))
        XCTAssertNil(OSCFacade.resolveShow(id: "", in: dir))
        XCTAssertNil(OSCFacade.resolveShow(id: "../flat", in: dir))
        XCTAssertNil(OSCFacade.resolveShow(id: "nested/nested", in: dir))
        XCTAssertNil(OSCFacade.resolveShow(id: ".flat", in: dir))
        XCTAssertFalse(OSCFacade.isValidShowID("a\\b"))
        XCTAssertTrue(OSCFacade.isValidShowID("Demo Waddle 2"))
    }

    func testAckMessagesAreSortedAndMapEveryOutcome() {
        let messages = OSCFacade.ackMessages(command: "play", outcomes: [
            DuckID("duck-03"): .timeout,
            DuckID("duck-01"): .ok,
            DuckID("duck-02"): .nacked("missed_start"),
            DuckID("duck-04"): .connectionFailed("no connection"),
            DuckID("duck-05"): .superseded
        ])
        XCTAssertEqual(messages.map(\.address), Array(repeating: "/duckswarm/ack", count: 5))
        XCTAssertEqual(messages.map(\.args), [
            [.string("play"), .string("duck-01"), .int32(1), .string("")],
            [.string("play"), .string("duck-02"), .int32(0), .string("missed_start")],
            [.string("play"), .string("duck-03"), .int32(0), .string("timeout")],
            [.string("play"), .string("duck-04"), .int32(0), .string("no connection")],
            [.string("play"), .string("duck-05"), .int32(0), .string("superseded")]
        ])
    }

    /// `swarmctl serve`'s preflight (exit 3 on a taken port) lives in the
    /// library so it can be checked here: the facade's own listener must
    /// make its port read as taken, and a stopped facade must release it.
    func testUDPPortProbeSeesTheFacadePort() async throws {
        let rig = try await rig()
        XCTAssertFalse(UDPPortProbe.isFree(rig.port), "the facade's listener holds udp/\(rig.port)")
        XCTAssertTrue(UDPPortProbe.isFree(0), "port 0 asks the kernel for any port")
        await rig.close()
        var free = UDPPortProbe.isFree(rig.port)
        for _ in 0..<100 where !free {
            await Task.sleepMs(20)
            free = UDPPortProbe.isFree(rig.port)
        }
        XCTAssertTrue(free, "udp/\(rig.port) must be released once the facade stopped")
    }

    func testStartTwiceThrowsAndStopThenRestartWorks() async throws {
        let rig = try await rig()
        do {
            _ = try await rig.facade.start()
            XCTFail("second start must throw")
        } catch {
            XCTAssertEqual(error as? OSCFacadeError, .alreadyStarted)
        }
        await rig.facade.stop()
        let port = await rig.facade.boundPort
        XCTAssertNil(port)
        let again = try await rig.facade.start()
        XCTAssertNotEqual(again, 0)
        let client = OSCTestClient(port: again)
        try await client.start()
        await client.send(OSCMessage(address: "/duckswarm/status"))
        let status = await client.waitFor("/duckswarm/status/transport")
        XCTAssertEqual(status.count, 1)
        await client.stop()
        await rig.close()
    }
}
