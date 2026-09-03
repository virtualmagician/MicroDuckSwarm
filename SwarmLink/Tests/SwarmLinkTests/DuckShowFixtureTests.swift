import XCTest
@testable import SwarmLink

/// Loads the *same* shared fixture files python/tests/test_validator.py
/// reads from shows/fixtures/ -- proving the format really is portable
/// (docs/duckshow-format.md), not just decoded the same way by
/// coincidence in two hand-written test suites (F67).
///
/// python/duckshow is the canonical validator (docs/architecture.md);
/// shows/fixtures/expected.json documents its per-fixture result, and
/// every fixture here asserts those same expected values -- there is
/// currently no known divergence between the two validators. If a future
/// change to either validator introduces one, give the fixture a
/// `divergent-` filename prefix and pin Swift's own current behavior in
/// its own clearly-labeled test here (see expected.json's `_comment`),
/// rather than silently matching or silently skipping it.
final class DuckShowFixtureTests: XCTestCase {
    private static var repoRoot: URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent() // SwarmLinkTests/
            .deletingLastPathComponent() // Tests/
            .deletingLastPathComponent() // SwarmLink/
            .deletingLastPathComponent() // repo root
    }

    private static var fixturesDir: URL {
        repoRoot.appendingPathComponent("shows/fixtures")
    }

    private struct ExpectedEntry {
        let errors: Int
        let warnings: Int
    }

    private static func loadExpected() throws -> [String: ExpectedEntry] {
        let data = try Data(contentsOf: fixturesDir.appendingPathComponent("expected.json"))
        let raw = try JSONSerialization.jsonObject(with: data) as? [String: Any] ?? [:]
        var out: [String: ExpectedEntry] = [:]
        for (key, value) in raw {
            guard key != "_comment", let dict = value as? [String: Any] else { continue }
            let errors = dict["errors"] as? Int ?? 0
            let warnings = dict["warnings"] as? Int ?? 0
            out[key] = ExpectedEntry(errors: errors, warnings: warnings)
        }
        return out
    }

    private func loadFixture(_ name: String) throws -> Show {
        try Show.load(contentsOf: Self.fixturesDir.appendingPathComponent("\(name).duckshow.json"))
    }

    func testFixtureDirectoryIsReachable() throws {
        XCTAssertTrue(FileManager.default.fileExists(atPath: Self.fixturesDir.path))
    }

    // MARK: Fixtures where Swift already agrees with the canonical validator

    func testValidBaselineHasNoErrorsOrWarnings() throws {
        let expected = try Self.loadExpected()["valid-baseline"]
        let report = try loadFixture("valid-baseline").validate()
        XCTAssertEqual(report.errors.count, expected?.errors ?? -1)
        XCTAssertEqual(report.warnings.count, expected?.warnings ?? -1)
    }

    func testTwoActionKeysMatchesCanonicalErrorCount() throws {
        let expected = try Self.loadExpected()["invalid-two-action-keys"]
        let report = try loadFixture("invalid-two-action-keys").validate()
        XCTAssertEqual(report.errors.count, expected?.errors ?? -1)
        XCTAssertTrue(report.errors.contains { $0.message.contains("more than one action key") })
    }

    func testVxOverLimitMatchesCanonicalErrorCount() throws {
        let expected = try Self.loadExpected()["invalid-vx-over-limit"]
        let report = try loadFixture("invalid-vx-over-limit").validate()
        XCTAssertEqual(report.errors.count, expected?.errors ?? -1)
        XCTAssertTrue(report.errors.contains { $0.message.contains("vx=0.5") })
    }

    func testEventsTooDenseMatchesCanonicalErrorCount() throws {
        let expected = try Self.loadExpected()["invalid-events-too-dense"]
        let report = try loadFixture("invalid-events-too-dense").validate()
        XCTAssertEqual(report.errors.count, expected?.errors ?? -1)
    }

    func testUnknownModeMatchesCanonicalErrorCount() throws {
        // Real robotd accepts exactly "walk"/"roller" as a mode event's
        // value (docs/robotd-api.md "Custom .onnx policies & modes"); any
        // other value is an error, not a warning -- see
        // shows/fixtures/expected.json.
        let expected = try Self.loadExpected()["invalid-unknown-mode"]
        let report = try loadFixture("invalid-unknown-mode").validate()
        XCTAssertEqual(report.errors.count, expected?.errors ?? -1)
        XCTAssertEqual(report.warnings.count, expected?.warnings ?? -1)
        XCTAssertTrue(report.errors.contains { $0.message.contains("not a valid drive mode") }, "\(report.errors)")
    }

    func testMissingTracksEntryMatchesCanonicalErrorCount() throws {
        // python/duckshow (matching docs/duckshow-format.md's "every role
        // in cast must have a track entry"): 1 error, 0 warnings -- see
        // shows/fixtures/expected.json. DuckShow.swift used to report this
        // as a warning only (see git history / F67); it is now an error,
        // matching the canonical validator.
        let expected = try Self.loadExpected()["invalid-missing-tracks-entry"]
        let report = try loadFixture("invalid-missing-tracks-entry").validate()
        XCTAssertEqual(report.errors.count, expected?.errors ?? -1)
        XCTAssertEqual(report.warnings.count, expected?.warnings ?? -1)
        XCTAssertTrue(report.errors.contains { $0.message.contains("no tracks entry") }, "\(report.errors)")
    }

    func testUnsortedEventsMatchesCanonicalNoErrorsOrWarnings() throws {
        // docs/duckshow-format.md only requires sorting for curve tracks
        // (locomotion/head/pose/mouth), not the point-event track, so
        // python/duckshow correctly raises nothing here -- and so does
        // DuckShow.swift's validateEvents(), which never applied the
        // curve-track sort rule to events: 0 errors, 0 warnings on both
        // sides -- see shows/fixtures/expected.json.
        let expected = try Self.loadExpected()["valid-unsorted-events"]
        let report = try loadFixture("valid-unsorted-events").validate()
        XCTAssertEqual(report.errors.count, expected?.errors ?? -1)
        XCTAssertEqual(report.warnings.count, expected?.warnings ?? -1)
    }
}
