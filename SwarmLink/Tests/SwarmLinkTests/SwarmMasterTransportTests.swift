import XCTest
@testable import SwarmLink

/// SwarmMaster's transport surface over real UDP: load outcomes beyond the
/// happy path, the exact fan-out payloads, and the ordering rules a show
/// night depends on (newest command wins, stop resets the cue, panic needs
/// no show, load while playing stops first).
final class SwarmMasterTransportTests: XCTestCase {
    private var tmpDir: URL?
    private let duck01 = DuckID("duck-01")

    override func tearDown() {
        if let tmpDir { try? FileManager.default.removeItem(at: tmpDir) }
        tmpDir = nil
        super.tearDown()
    }

    /// One master (ephemeral local port unless `masterPort` given), one duck
    /// on an ephemeral port, roster + show files on disk.
    private func rig(
        showName: String = "fixture", role: String = "lead", roles: [String] = ["lead"], masterPort: UInt16 = 0
    ) async throws -> (master: SwarmMaster, duck: TestDuck, show: URL, roster: URL) {
        let duck = TestDuck()
        let port = try await duck.start()
        let (dir, show) = try Fixtures.writeShow(named: showName, roles: roles)
        tmpDir = dir
        let roster = try Fixtures.writeRoster([RosterEntry(id: "duck-01", host: "127.0.0.1", port: port, role: role)], in: dir)
        let master = SwarmMaster(masterPort: masterPort)
        return (master, duck, show, roster)
    }

    // MARK: load outcomes

    func testLoadReportsNackWithAgentError() async throws {
        let (master, duck, show, roster) = try await rig()
        await duck.setNack("load", error: "sha256 mismatch")
        let outcomes = try await master.load(show: show, roster: roster)
        XCTAssertEqual(outcomes[duck01]?.status, .nacked("sha256 mismatch"))
        XCTAssertEqual(outcomes[duck01]?.isOK, false)
        await duck.stop()
    }

    func testLoadRejectsRosterRoleNotInCast() async throws {
        let (master, duck, show, roster) = try await rig(role: "ghost")
        let outcomes = try await master.load(show: show, roster: roster)
        guard case .connectionFailed(let reason)? = outcomes[duck01]?.status else {
            return XCTFail("expected connectionFailed, got \(String(describing: outcomes[duck01]))")
        }
        XCTAssertTrue(reason.contains("ghost"))
        let sent = await duck.commands(named: "load")
        XCTAssertTrue(sent.isEmpty, "no load must be sent to a duck whose role is not in the cast")
        await duck.stop()
    }

    func testLoadSendsShowIDWithoutDoubleExtension() async throws {
        let (master, duck, show, roster) = try await rig(showName: "x")
        let outcomes = try await master.load(show: show, roster: roster)
        XCTAssertEqual(outcomes[duck01]?.status, .ok)
        let loads = await duck.commands(named: "load")
        let load = try XCTUnwrap(loads.first)
        guard case .load(let id, let sha, let role) = load.payload else { return XCTFail("\(load.payload)") }
        XCTAssertEqual(id, "x")
        XCTAssertEqual(role, "lead")
        XCTAssertEqual(sha, try Show.sha256(of: show))
        await duck.stop()
    }

    // MARK: fan-out payloads

    func testPlaySeekStopPanicPayloadsAndOutcomes() async throws {
        let (master, duck, show, roster) = try await rig()
        _ = try await master.load(show: show, roster: roster)

        let before = MasterClock.nowNanoseconds()
        let playOutcomes = try await master.play(at: 200_000_000)
        XCTAssertEqual(playOutcomes, [duck01: .ok])
        let plays = await duck.commands(named: "play")
        let play = try XCTUnwrap(plays.first)
        guard case .play(let id, let atMasterTime, let fromShowTime) = play.payload else { return XCTFail("\(play.payload)") }
        XCTAssertEqual(id, "fixture")
        XCTAssertEqual(fromShowTime, 0.0)
        XCTAssertGreaterThanOrEqual(atMasterTime - before, 200_000_000)
        XCTAssertLessThan(atMasterTime - before, 1_200_000_000)

        let seekOutcomes = await master.seek(to: 4.5)
        XCTAssertEqual(seekOutcomes, [duck01: .ok])
        let seeks = await duck.commands(named: "seek")
        let seek = try XCTUnwrap(seeks.first)
        guard case .seek(let showTime, _) = seek.payload else { return XCTFail("\(seek.payload)") }
        XCTAssertEqual(showTime, 4.5)

        let stopOutcomes = await master.stop()
        XCTAssertEqual(stopOutcomes, [duck01: .ok])
        let stops = await duck.commands(named: "stop")
        XCTAssertEqual(stops.count, 1)
        let panicOutcomes = await master.panic()
        XCTAssertEqual(panicOutcomes, [duck01: .ok])
        let panics = await duck.commands(named: "panic")
        XCTAssertEqual(panics.count, 1)
        let transport = await master.currentTransport
        XCTAssertEqual(transport, .stopped)
        await duck.stop()
    }

    // MARK: newest command wins (F50)

    func testStopSupersedesInFlightPlayRetries() async throws {
        let (master, duck, show, roster) = try await rig()
        _ = try await master.load(show: show, roster: roster)
        await duck.setDropFirst(1, of: "play") // first play datagram "lost in a WiFi burst"

        async let playOutcome = try master.play(at: 300_000_000)
        await duck.waitForCommands(named: "play", count: 1)
        await Task.sleepMs(30)
        let stopOutcome = await master.stop()
        XCTAssertEqual(stopOutcome, [duck01: .ok])
        let resolvedPlay = try await playOutcome
        XCTAssertEqual(resolvedPlay, [duck01: .superseded])

        // No stale play retry may follow the stop.
        await Task.sleepMs(450)
        let plays = await duck.commands(named: "play")
        XCTAssertEqual(plays.count, 1, "the dropped first copy only; retries were superseded by stop: \(plays.map(\.cmdID))")
        let names = await duck.received.map(\.payload.cmdName)
        XCTAssertEqual(names.last, "stop")
        let transport = await master.currentTransport
        XCTAssertEqual(transport, .stopped)
        await duck.stop()
    }

    // MARK: cue handling (F52)

    func testStopAfterLiveSeekResetsCueSoNextPlayStartsAtZero() async throws {
        let (master, duck, show, roster) = try await rig()
        _ = try await master.load(show: show, roster: roster)
        _ = try await master.play(at: 20_000_000)
        await Task.sleepMs(80)
        let playing = await master.currentTransport
        XCTAssertEqual(playing, .playing)

        _ = await master.seek(to: 45.0)
        let afterSeek = await master.currentShowTime() ?? -1
        XCTAssertEqual(afterSeek, 45.0, accuracy: 0.2)
        _ = await master.stop()
        let afterStop = await master.currentShowTime()
        XCTAssertNil(afterStop)

        _ = try await master.play(at: 20_000_000)
        let plays = await duck.commands(named: "play")
        XCTAssertEqual(plays.count, 2)
        guard plays.count == 2, case .play(_, _, let fromShowTime) = plays[1].payload else { return XCTFail("\(plays)") }
        XCTAssertEqual(fromShowTime, 0.0, "a stop must reset the rehearsal seek point")
        await Task.sleepMs(60)
        let secondShowTime = await master.currentShowTime() ?? -1
        XCTAssertEqual(secondShowTime, 0.05, accuracy: 0.2)
        _ = await master.stop()
        await duck.stop()
    }

    func testSeekWhileStoppedCuesNextPlay() async throws {
        let (master, duck, show, roster) = try await rig()
        _ = try await master.load(show: show, roster: roster)
        _ = await master.seek(to: 12.0)
        let transport = await master.currentTransport
        XCTAssertEqual(transport, .stopped)
        _ = try await master.play(at: 20_000_000)
        let plays = await duck.commands(named: "play")
        let play = try XCTUnwrap(plays.first)
        guard case .play(_, _, let fromShowTime) = play.payload else { return XCTFail("\(play.payload)") }
        XCTAssertEqual(fromShowTime, 12.0)
        await Task.sleepMs(60)
        let showTime = await master.currentShowTime() ?? -1
        XCTAssertEqual(showTime, 12.05, accuracy: 0.2)
        _ = await master.stop()
        await duck.stop()
    }

    // MARK: scheduled start vs newer commands (F53)

    func testStopBeforeLeadTimeDoesNotBeginPlaying() async throws {
        let (master, duck, show, roster) = try await rig()
        _ = try await master.load(show: show, roster: roster)
        _ = try await master.play(at: 300_000_000)
        let armed = await master.currentTransport
        XCTAssertEqual(armed, .armed)
        _ = await master.stop()
        await Task.sleepMs(450)
        let transport = await master.currentTransport
        XCTAssertEqual(transport, .stopped)
        let showTime = await master.currentShowTime()
        XCTAssertNil(showTime)
        await duck.stop()
    }

    func testSeekWhileArmedWinsOverScheduledStart() async throws {
        let (master, duck, show, roster) = try await rig()
        _ = try await master.load(show: show, roster: roster)
        // The responder ACKs instantly, so play() returns long before its
        // 300 ms lead elapses: the seek lands while still armed.
        _ = try await master.play(at: 300_000_000)
        let armed = await master.currentTransport
        XCTAssertEqual(armed, .armed)
        _ = await master.seek(to: 45.0)
        let transport = await master.currentTransport
        XCTAssertEqual(transport, .playing)
        await Task.sleepMs(400) // past the original at_master_time
        let showTime = await master.currentShowTime() ?? -1
        XCTAssertEqual(showTime, 45.4, accuracy: 0.25, "the stale scheduled start must not reset the epoch to 0")
        _ = await master.stop()
        await duck.stop()
    }

    func testSecondPlayWhileArmedOwnsTheEpoch() async throws {
        let (master, duck, show, roster) = try await rig()
        _ = try await master.load(show: show, roster: roster)
        _ = try await master.play(at: 300_000_000)
        await Task.sleepMs(150)
        let secondIssued = MasterClock.nowNanoseconds()
        _ = try await master.play(at: 300_000_000)
        await Task.sleepMs(400)
        let elapsed = Double(MasterClock.nowNanoseconds() - secondIssued) / 1e9
        let transport = await master.currentTransport
        XCTAssertEqual(transport, .playing)
        let showTime = await master.currentShowTime() ?? -1
        XCTAssertEqual(showTime, elapsed - 0.3, accuracy: 0.08, "the master epoch must follow the second play, like the agents do")
        _ = await master.stop()
        await duck.stop()
    }

    // MARK: connect without load (F49 / F74)

    func testPanicWorksWithOnlyAConnectedRoster() async throws {
        let (master, duck, _, roster) = try await rig()
        try await master.connect(roster: roster)
        let connected = await master.connectedDucks
        XCTAssertEqual(connected, [duck01])

        let panicOutcomes = await master.panic()
        XCTAssertEqual(panicOutcomes, [duck01: .ok])
        let panics = await duck.commands(named: "panic")
        XCTAssertEqual(panics.count, 1)

        let stopOutcomes = await master.stop()
        XCTAssertEqual(stopOutcomes, [duck01: .ok])
        let seekOutcomes = await master.seek(to: 3.0)
        XCTAssertEqual(seekOutcomes, [duck01: .ok])
        await duck.stop()
    }

    func testPlayWithoutLoadThrowsNotLoaded() async throws {
        let (master, duck, _, roster) = try await rig()
        try await master.connect(roster: roster)
        do {
            _ = try await master.play()
            XCTFail("play without a loaded show must throw")
        } catch {
            XCTAssertEqual(error as? SwarmMasterError, .notLoaded)
        }
        let plays = await duck.commands(named: "play")
        XCTAssertTrue(plays.isEmpty)
        await duck.stop()
    }

    func testPanicWithNothingConnectedReportsNoOutcomes() async {
        let master = SwarmMaster(masterPort: 0)
        let outcomes = await master.panic()
        XCTAssertTrue(outcomes.isEmpty)
    }

    // MARK: load while playing (F57)

    func testLoadWhilePlayingSendsStopFirst() async throws {
        let (master, duck, show, roster) = try await rig()
        _ = try await master.load(show: show, roster: roster)
        _ = try await master.play(at: 10_000_000)
        await Task.sleepMs(50)
        let playing = await master.currentTransport
        XCTAssertEqual(playing, .playing)

        let outcomes = try await master.load(show: show, roster: roster)
        XCTAssertEqual(outcomes[duck01]?.status, .ok)
        let names = await duck.received.map(\.payload.cmdName)
        XCTAssertEqual(names, ["load", "play", "stop", "load"])
        let transport = await master.currentTransport
        XCTAssertEqual(transport, .stopped)
        await duck.stop()
    }

    // MARK: production port configuration (F60)

    func testTwoDucksShareOneFixedMasterPort() async throws {
        let lead = TestDuck(id: "duck-01")
        let wing = TestDuck(id: "duck-02")
        let leadPort = try await lead.start()
        let wingPort = try await wing.start()
        let (dir, show) = try Fixtures.writeShow(named: "duet", roles: ["lead", "wing"])
        tmpDir = dir
        let roster = try Fixtures.writeRoster([
            RosterEntry(id: "duck-01", host: "127.0.0.1", port: leadPort, role: "lead"),
            RosterEntry(id: "duck-02", host: "127.0.0.1", port: wingPort, role: "wing")
        ], in: dir)

        let master = SwarmMaster(masterPort: Fixtures.randomMasterPort())
        let outcomes = try await master.load(show: show, roster: roster)
        XCTAssertEqual(outcomes[duck01]?.status, .ok)
        XCTAssertEqual(outcomes[DuckID("duck-02")]?.status, .ok)
        let leadLoads = await lead.commands(named: "load")
        let wingLoads = await wing.commands(named: "load")
        guard case .load(_, _, let leadRole)? = leadLoads.first?.payload,
              case .load(_, _, let wingRole)? = wingLoads.first?.payload else {
            return XCTFail("both ducks must receive their load")
        }
        XCTAssertEqual(leadRole, "lead")
        XCTAssertEqual(wingRole, "wing")

        let panic = await master.panic()
        XCTAssertEqual(panic, [duck01: .ok, DuckID("duck-02"): .ok])
        await lead.stop()
        await wing.stop()
    }
}
