import XCTest
@testable import SwarmLink

final class MasterClockTests: XCTestCase {
    func testNowNanosecondsIsMonotonicNonDecreasing() {
        let a = MasterClock.nowNanoseconds()
        let b = MasterClock.nowNanoseconds()
        XCTAssertGreaterThanOrEqual(b, a)
    }

    func testShowTimeIsNilBeforeAnyEpoch() {
        let clock = MasterClock()
        XCTAssertNil(clock.showTime(now: 1_000))
    }

    func testShowTimeAdvancesLinearlyFromPlayEpoch() {
        var clock = MasterClock()
        clock.play(at: 1_000_000_000, fromShowTime: 0.0)

        XCTAssertEqual(clock.showTime(now: 1_000_000_000) ?? .nan, 0.0, accuracy: 1e-9)
        XCTAssertEqual(clock.showTime(now: 1_500_000_000) ?? .nan, 0.5, accuracy: 1e-9)
        XCTAssertEqual(clock.showTime(now: 3_000_000_000) ?? .nan, 2.0, accuracy: 1e-9)
    }

    func testShowTimeBeforeEpochGoesNegative() {
        // A "play" scheduled in the future (at_master_time > now) should
        // read back as negative show-time until that instant arrives —
        // callers use this to know playback hasn't started yet.
        var clock = MasterClock()
        clock.play(at: 2_000_000_000, fromShowTime: 0.0)
        XCTAssertEqual(clock.showTime(now: 1_000_000_000) ?? .nan, -1.0, accuracy: 1e-9)
    }

    func testSeekReanchorsEpoch() {
        var clock = MasterClock()
        clock.play(at: 0, fromShowTime: 0.0)
        XCTAssertEqual(clock.showTime(now: 5_000_000_000) ?? .nan, 5.0, accuracy: 1e-9)

        clock.seek(to: 45.0, atMasterTimeNs: 5_000_000_000)
        XCTAssertEqual(clock.showTime(now: 5_000_000_000) ?? .nan, 45.0, accuracy: 1e-9)
        XCTAssertEqual(clock.showTime(now: 6_000_000_000) ?? .nan, 46.0, accuracy: 1e-9)
    }

    func testStopClearsEpoch() {
        var clock = MasterClock()
        clock.play(at: 0, fromShowTime: 0.0)
        XCTAssertNotNil(clock.showTime(now: 100))
        clock.stop()
        XCTAssertNil(clock.showTime(now: 100))
    }

    func testPlayEpochShowTimeMath() {
        let epoch = PlayEpoch(masterTimeNs: 10_000_000_000, showTimeAtEpoch: 3.0)
        XCTAssertEqual(epoch.showTime(atMasterTimeNs: 12_000_000_000), 5.0, accuracy: 1e-9)
    }
}
