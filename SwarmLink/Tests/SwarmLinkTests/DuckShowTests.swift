import XCTest
@testable import SwarmLink

final class DuckShowTests: XCTestCase {
    /// Walks up from this source file to the repo root
    /// (.../MicroDuckSwarm/SwarmLink/Tests/SwarmLinkTests/DuckShowTests.swift
    /// -> .../MicroDuckSwarm), so this test finds shows/demo/demo.duckshow.json
    /// no matter where the repo is checked out.
    private static var repoRoot: URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent() // SwarmLinkTests/
            .deletingLastPathComponent() // Tests/
            .deletingLastPathComponent() // SwarmLink/
            .deletingLastPathComponent() // repo root
    }

    private static var demoShowURL: URL {
        repoRoot.appendingPathComponent("shows/demo/demo.duckshow.json")
    }

    private func loadDemoShow() throws -> Show {
        try Show.load(contentsOf: Self.demoShowURL)
    }

    func testDemoShowFileExists() {
        XCTAssertTrue(
            FileManager.default.fileExists(atPath: Self.demoShowURL.path),
            "expected demo show at \(Self.demoShowURL.path)"
        )
    }

    func testDecodesFormatAndMeta() throws {
        let show = try loadDemoShow()
        XCTAssertEqual(show.format, "duckshow/1")
        XCTAssertEqual(show.meta.name, "Demo Waddle")
        XCTAssertEqual(show.meta.author, "Marco Tempest")
        XCTAssertEqual(show.meta.created, "2026-09-01")
        XCTAssertEqual(show.meta.duration, 20.0)
        XCTAssertEqual(show.meta.music?.file, "demo.wav")
        XCTAssertEqual(show.meta.music?.bpm, 120.0)
        XCTAssertEqual(show.meta.music?.beatOffset, 0.0)
        XCTAssertTrue(show.requires.policies.isEmpty)
    }

    func testDecodesCastRoles() throws {
        let show = try loadDemoShow()
        XCTAssertEqual(show.cast.map(\.role), ["lead", "wing"])
        XCTAssertEqual(show.cast.first?.notes, "front center mark")
        XCTAssertEqual(show.cast.last?.notes, "stage left mark")
    }

    func testDecodesLeadTrackCounts() throws {
        let show = try loadDemoShow()
        let lead = try XCTUnwrap(show.tracks["lead"])
        XCTAssertEqual(lead.head.count, 8)
        XCTAssertEqual(lead.locomotion.count, 4)
        XCTAssertEqual(lead.mouth.count, 3)
        XCTAssertEqual(lead.events.count, 4)
        XCTAssertTrue(lead.pose.isEmpty)
        XCTAssertTrue(lead.servo.isEmpty)
    }

    func testDecodesWingTrackCounts() throws {
        let show = try loadDemoShow()
        let wing = try XCTUnwrap(show.tracks["wing"])
        XCTAssertEqual(wing.head.count, 7)
        XCTAssertEqual(wing.pose.count, 3)
        XCTAssertEqual(wing.events.count, 3)
        XCTAssertTrue(wing.locomotion.isEmpty)
        XCTAssertTrue(wing.mouth.isEmpty)
    }

    func testDecodesKeyframeValuesAndDefaultInterp() throws {
        let show = try loadDemoShow()
        let lead = try XCTUnwrap(show.tracks["lead"])

        // First head keyframe: fully specified, "smooth" interp.
        let firstHead = try XCTUnwrap(lead.head.first)
        XCTAssertEqual(firstHead.t, 0.0)
        XCTAssertEqual(firstHead.headPitch, 0.0)
        XCTAssertEqual(firstHead.interp, .smooth)

        // Last head keyframe in the file omits "interp" -> defaults to .linear.
        let lastHead = try XCTUnwrap(lead.head.last)
        XCTAssertEqual(lastHead.t, 8.0)
        XCTAssertEqual(lastHead.headYaw, 0.0)
        XCTAssertEqual(lastHead.interp, .linear)

        // Locomotion keyframe with an explicit "step" interp.
        let steppedLocomotion = try XCTUnwrap(lead.locomotion.first { $0.t == 3.5 })
        XCTAssertEqual(steppedLocomotion.vx, 0.1)
        XCTAssertEqual(steppedLocomotion.interp, .step)
    }

    func testDecodesEventActions() throws {
        let show = try loadDemoShow()
        let lead = try XCTUnwrap(show.tracks["lead"])

        XCTAssertEqual(lead.events[0].t, 4.0)
        guard case .sound(let tag, let hold) = lead.events[0].action else {
            return XCTFail("expected a sound event")
        }
        XCTAssertEqual(tag, "chirp")
        XCTAssertNil(hold)

        guard case .skill(let skill) = lead.events[2].action else {
            return XCTFail("expected a do/skill event")
        }
        XCTAssertEqual(lead.events[2].t, 12.0)
        XCTAssertEqual(skill, "kick_left")
    }

    func testDecodesPoseActiveFlag() throws {
        let show = try loadDemoShow()
        let wing = try XCTUnwrap(show.tracks["wing"])
        XCTAssertEqual(wing.pose.map(\.active), [true, true, false])
        XCTAssertEqual(wing.pose[1].z, -0.03, accuracy: 1e-9)
        XCTAssertEqual(wing.pose[1].pitch, 0.1, accuracy: 1e-9)
    }

    func testValidateReportsNoErrorsForDemoShow() throws {
        let show = try loadDemoShow()
        let report = show.validate()
        XCTAssertTrue(report.isValid, "unexpected errors: \(report.errors)")
    }

    func testShaHelperIsStableAndHexEncoded() throws {
        let sha1 = try Show.sha256(of: Self.demoShowURL)
        let sha2 = try Show.sha256(of: Self.demoShowURL)
        XCTAssertEqual(sha1, sha2)
        XCTAssertEqual(sha1.count, 64)
        XCTAssertTrue(sha1.allSatisfy(\.isHexDigit))
    }

    func testShaHelperMatchesPinnedPythonLiteral() throws {
        // Same literal is asserted from the Python side in
        // test_showmaster.ShowFileHelpersTest.test_sha256_of_demo_show_matches_pinned_literal
        // -- a `load` command's sha256 field must mean the same 64 hex
        // chars whether it was computed on the Mac (this helper) or the
        // duck (python's hashlib), or the hash check in
        // docs/swarmlink-protocol.md #3 doesn't actually guarantee
        // anything (F67).
        let sha = try Show.sha256(of: Self.demoShowURL)
        XCTAssertEqual(sha, "617b07e6dd6596f4bce5cc772072040c9365c1f579decd44cda3244ef7ac496f")
    }

    func testRejectsUnknownMajorFormatVersion() throws {
        let bogus = """
        {"format":"duckshow/99","meta":{"name":"x","author":"y","created":"2026-01-01","duration":1.0},
         "requires":{"policies":[]},"cast":[],"tracks":{}}
        """
        let data = Data(bogus.utf8)
        let tmp = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString + ".duckshow.json")
        try data.write(to: tmp)
        defer { try? FileManager.default.removeItem(at: tmp) }

        XCTAssertThrowsError(try Show.load(contentsOf: tmp)) { error in
            guard case DuckShowError.unsupportedFormat(let format) = error else {
                return XCTFail("expected unsupportedFormat, got \(error)")
            }
            XCTAssertEqual(format, "duckshow/99")
        }
    }

    func testToleratesUnknownFields() throws {
        let json = """
        {"format":"duckshow/1","meta":{"name":"x","author":"y","created":"2026-01-01","duration":1.0,
         "totally_new_field": 42},
         "requires":{"policies":[]},"cast":[{"role":"lead","surprise":true}],
         "tracks":{"lead":{}}, "future_top_level_key": {"a": 1}}
        """
        let show = try JSONDecoder().decode(Show.self, from: Data(json.utf8))
        XCTAssertEqual(show.cast.first?.role, "lead")
    }
}
