import XCTest
@testable import SwarmLink

/// Telemetry ingestion, the lost watchdog (including roster ducks that
/// never report), subscriber lifecycle, time-sync replies, and the 5 Hz
/// state loop's behaviour across load/play churn.
final class SwarmMasterTelemetryTests: XCTestCase {
    private var tmpDir: URL?
    private let duck01 = DuckID("duck-01")

    override func tearDown() {
        if let tmpDir { try? FileManager.default.removeItem(at: tmpDir) }
        tmpDir = nil
        super.tearDown()
    }

    private func rig(lostThreshold: Double = 0.3) async throws -> (master: SwarmMaster, duck: TestDuck, show: URL, roster: URL) {
        let duck = TestDuck()
        let port = try await duck.start()
        let (dir, show) = try Fixtures.writeShow(named: "tele")
        tmpDir = dir
        let roster = try Fixtures.writeRoster([RosterEntry(id: "duck-01", host: "127.0.0.1", port: port, role: "lead")], in: dir)
        let master = SwarmMaster(masterPort: 0, telemetryLostThresholdSeconds: lostThreshold)
        return (master, duck, show, roster)
    }

    /// Collects events from a fresh subscription until `count` arrive or
    /// `timeoutMs` elapses.
    private static func collect(_ master: SwarmMaster, count: Int, timeoutMs: Int) async -> [TelemetryEvent] {
        let stream = await master.telemetryEvents()
        return await withTaskGroup(of: [TelemetryEvent].self) { group in
            group.addTask {
                var events: [TelemetryEvent] = []
                for await event in stream {
                    events.append(event)
                    if events.count >= count { break }
                }
                return events
            }
            group.addTask {
                await Task.sleepMs(timeoutMs)
                return []
            }
            let first = await group.next() ?? []
            group.cancelAll()
            return first
        }
    }

    func testTelemetryIngestThenLostAfterThreshold() async throws {
        let (master, duck, show, roster) = try await rig(lostThreshold: 0.3)
        _ = try await master.load(show: show, roster: roster)

        async let events = Self.collect(master, count: 2, timeoutMs: 1500)
        await Task.sleepMs(20)
        try await duck.sendTelemetry(state: .loaded, show: "tele", showTime: 0)
        let got = await events

        guard got.count == 2, case .updated(let t) = got[0], case .lost(let lostID) = got[1] else {
            return XCTFail("expected [.updated, .lost], got \(got)")
        }
        XCTAssertEqual(t.duck, duck01)
        XCTAssertEqual(t.state, .loaded)
        XCTAssertEqual(t.show, "tele")
        XCTAssertFalse(t.lost)
        XCTAssertEqual(lostID, duck01)
        let lostEntry = await master.telemetry[duck01]?.lost
        XCTAssertEqual(lostEntry, true)
        let lostSet = await master.lostDucks
        XCTAssertEqual(lostSet, [duck01])

        // Telemetry returning clears the lost flag.
        try await duck.sendTelemetry(state: .loaded, show: "tele", showTime: 1)
        await Task.sleepMs(50)
        let clearedEntry = await master.telemetry[duck01]?.lost
        XCTAssertEqual(clearedEntry, false)
        let clearedSet = await master.lostDucks
        XCTAssertTrue(clearedSet.isEmpty)
        await duck.stop()
    }

    func testSilentRosterDuckIsReportedLostWithoutEverSendingTelemetry() async throws {
        let (master, duck, show, roster) = try await rig(lostThreshold: 0.3)
        _ = try await master.load(show: show, roster: roster) // ACKs load, never sends telemetry
        let events = await Self.collect(master, count: 1, timeoutMs: 1500)
        XCTAssertEqual(events, [.lost(duck01)])
        let lost = await master.lostDucks
        XCTAssertEqual(lost, [duck01])
        let snapshot = await master.telemetry
        XCTAssertTrue(snapshot.isEmpty, "no fabricated telemetry entry")
        // Reported once, not on every sweep.
        await Task.sleepMs(400)
        let more = await Self.collect(master, count: 1, timeoutMs: 200)
        XCTAssertTrue(more.isEmpty)
        await duck.stop()
    }

    func testTelemetryStreamUnregistersOnCancel() async throws {
        let master = SwarmMaster(masterPort: 0)
        let consumer = Task {
            for await _ in await master.telemetryEvents() {}
        }
        await Task.sleepMs(50)
        let live = await master.telemetrySubscriberCount
        XCTAssertEqual(live, 1)
        consumer.cancel()
        _ = await consumer.value
        await Task.sleepMs(50)
        let afterCancel = await master.telemetrySubscriberCount
        XCTAssertEqual(afterCancel, 0)
    }

    func testTimeRequestGetsResponseWithMonotonicStamps() async throws {
        let (master, duck, show, roster) = try await rig()
        _ = try await master.load(show: show, roster: roster)
        let t0: Int64 = 123_456
        try await duck.sendTimeRequest(t0: t0)
        var responses = await duck.receivedTimeResponses
        for _ in 0..<100 where responses.isEmpty {
            await Task.sleepMs(10)
            responses = await duck.receivedTimeResponses
        }
        let response = try XCTUnwrap(responses.first)
        XCTAssertEqual(response.t0, t0)
        XCTAssertLessThanOrEqual(response.t1, response.t2)
        XCTAssertGreaterThan(response.t1, 0)
        await duck.stop()
    }

    // MARK: 5 Hz state loop (F51)

    func testStateLoopRunsAtFiveHertzAndStopsOnStop() async throws {
        let (master, duck, show, roster) = try await rig()
        _ = try await master.load(show: show, roster: roster)
        _ = try await master.play(at: 10_000_000)
        await Task.sleepMs(1000)
        let states = await duck.receivedStates
        XCTAssertGreaterThanOrEqual(states.count, 3, "expected ~5 state ticks in 1 s, got \(states.count)")
        XCTAssertLessThanOrEqual(states.count, 8, "state loop must not exceed ~5 Hz, got \(states.count) in 1 s")
        let seqs = states.map(\.seq)
        XCTAssertEqual(seqs, seqs.sorted())
        XCTAssertEqual(Set(seqs).count, seqs.count, "seq must be unique/monotonic")

        _ = await master.stop()
        let atStop = await duck.receivedStates.count
        await Task.sleepMs(500)
        let afterStop = await duck.receivedStates.count
        XCTAssertLessThanOrEqual(afterStop, atStop + 1, "no state ticks after stop")
        await duck.stop()
    }

    func testStateLoopDoesNotSpinAcrossLoadPlayChurn() async throws {
        let (master, duck, show, roster) = try await rig()
        _ = try await master.load(show: show, roster: roster)
        _ = try await master.play(at: 10_000_000)
        await Task.sleepMs(50)

        // Reload while playing and press GO while the load is in flight.
        async let reload = try master.load(show: show, roster: roster)
        await Task.sleepMs(1)
        _ = try? await master.play(at: 10_000_000)
        _ = try await reload
        _ = try await master.play(at: 10_000_000)

        await Task.sleepMs(1000)
        let ticks = await duck.receivedStates.count
        XCTAssertLessThanOrEqual(ticks, 12, "a cancelled state loop must not keep ticking: \(ticks) ticks")
        XCTAssertGreaterThanOrEqual(ticks, 3)
        _ = await master.stop()
        await duck.stop()
    }
}
