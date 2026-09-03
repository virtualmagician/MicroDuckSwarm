import XCTest
@testable import SwarmLink

/// Cross-implementation fixtures: every document here loads and validates
/// identically in `python/duckshow` (the canonical loader/validator). If a
/// case is added here, mirror it in python/tests.
final class DuckShowParityTests: XCTestCase {
    private func decode(_ json: String) throws -> Show {
        try Show.decode(Data(json.utf8))
    }

    private func show(tracks: String, requires: String = "{\"policies\":[]}", duration: Double = 10.0) throws -> Show {
        try decode("""
        {"format":"duckshow/1","meta":{"duration":\(duration)},"requires":\(requires),
         "cast":[{"role":"lead"}],"tracks":{"lead":\(tracks)}}
        """)
    }

    // MARK: Optional fields (python/tests/test_loader.py MINIMAL and friends)

    func testMinimalDocumentLoads() throws {
        let show = try decode("""
        {"format":"duckshow/1","meta":{"duration":5.0},"cast":[{"role":"lead"}],"tracks":{"lead":{}}}
        """)
        XCTAssertNil(show.meta.name)
        XCTAssertNil(show.meta.author)
        XCTAssertNil(show.meta.created)
        XCTAssertNil(show.meta.music)
        XCTAssertEqual(show.meta.duration, 5.0)
        XCTAssertTrue(show.requires.policies.isEmpty)
        XCTAssertTrue(show.validate().isValid)
    }

    func testSparseKeyframesDefaultLikePython() throws {
        let show = try show(tracks: """
        {"head":[{"t":0,"head_yaw":0.3}],
         "locomotion":[{"t":0}],
         "pose":[{"t":0,"z":-0.01}],
         "mouth":[{"t":0}]}
        """)
        let lead = try XCTUnwrap(show.tracks["lead"])
        XCTAssertEqual(lead.head[0].headYaw, 0.3)
        XCTAssertEqual(lead.head[0].neckPitch, 0.0)
        XCTAssertEqual(lead.head[0].headPitch, 0.0)
        XCTAssertEqual(lead.head[0].headRoll, 0.0)
        XCTAssertEqual(lead.head[0].interp, .linear)
        XCTAssertEqual(lead.locomotion[0].vx, 0.0)
        XCTAssertEqual(lead.locomotion[0].vyaw, 0.0)
        XCTAssertEqual(lead.pose[0].z, -0.01)
        XCTAssertFalse(lead.pose[0].active)
        XCTAssertEqual(lead.mouth[0].open, 0.0)
        XCTAssertTrue(show.validate().isValid)
    }

    func testPolicyWithoutSlotAndMusicWithoutBeatOffset() throws {
        let show = try decode("""
        {"format":"duckshow/1",
         "meta":{"duration":5.0,"music":{"file":"x.wav","bpm":100}},
         "requires":{"policies":[{"name":"moonwalk","mode":"moonwalk","file":"policies/m.onnx","sha256":"abc"}]},
         "cast":[{"role":"lead"}],"tracks":{"lead":{}}}
        """)
        XCTAssertNil(show.requires.policies[0].slot)
        XCTAssertEqual(show.meta.music?.beatOffset, 0.0)
        XCTAssertEqual(show.meta.music?.bpm, 100)
    }

    func testServoWithoutModeDefaultsToHold() throws {
        let show = try show(tracks: """
        {"servo":[{"t":2.0,"duration":1.0}]}
        """)
        XCTAssertEqual(show.tracks["lead"]?.servo[0].mode, "hold")
        XCTAssertEqual(show.tracks["lead"]?.servo[0].duration, 1.0)
    }

    func testPolicyStillRequiresNameFileSha() {
        XCTAssertThrowsError(try decode("""
        {"format":"duckshow/1","meta":{"duration":5.0},
         "requires":{"policies":[{"name":"moonwalk","mode":"moonwalk"}]},
         "cast":[{"role":"lead"}],"tracks":{"lead":{}}}
        """))
    }

    func testKeyframeStillRequiresT() {
        XCTAssertThrowsError(try show(tracks: "{\"head\":[{\"head_yaw\":0.3}]}"))
    }

    // MARK: Format gate (F46)

    func testFutureMajorIsReportedAsUnsupportedFormatBeforeSchemaErrors() {
        let json = """
        {"format":"duckshow/2","meta":{"title":"x","length_s":3},"cast":[],"tracks":{}}
        """
        XCTAssertThrowsError(try decode(json)) { error in
            guard case DuckShowError.unsupportedFormat(let format) = error else {
                return XCTFail("expected unsupportedFormat, got \(error)")
            }
            XCTAssertEqual(format, "duckshow/2")
        }
    }

    func testMissingFormatIsReportedAsSuch() {
        XCTAssertThrowsError(try decode("{\"meta\":{\"duration\":1},\"cast\":[],\"tracks\":{}}")) { error in
            XCTAssertEqual(error as? DuckShowError, .missingFormat)
        }
        XCTAssertThrowsError(try decode("{\"format\":7,\"meta\":{\"duration\":1},\"cast\":[],\"tracks\":{}}")) { error in
            XCTAssertEqual(error as? DuckShowError, .missingFormat)
        }
    }

    // MARK: Events (F41, F43)

    func testEventsInAuthoringOrderAreValid() throws {
        // The doc's own "Per-role tracks" example lists events out of time order.
        let show = try show(tracks: """
        {"events":[{"t":8.0,"do":"kick_left"},{"t":3.0,"sound":"chirp"},{"t":15.0,"mode":"roller"}]}
        """, requires: "{\"policies\":[{\"name\":\"p\",\"mode\":\"roller\",\"file\":\"x.onnx\",\"sha256\":\"abc\"}]}", duration: 20)
        let report = show.validate()
        XCTAssertTrue(report.isValid, "unexpected errors: \(report.errors)")
    }

    func testEventDensityIsCheckedAfterSorting() throws {
        let show = try show(tracks: """
        {"events":[{"t":3.0,"sound":"chirp"},{"t":8.0,"do":"kick_left"},{"t":3.1,"sound":"coo"}]}
        """)
        let report = show.validate()
        XCTAssertEqual(report.errors.count, 1, "\(report.errors)")
        XCTAssertEqual(report.errors.first?.t, 3.1)
        XCTAssertTrue(report.errors.first?.message.contains("is less than 0.25s after previous event at t=3.0") ?? false, "\(report.errors)")
    }

    func testMultipleActionKeysDecodeButFailValidation() throws {
        let show = try show(tracks: """
        {"events":[{"t":1.0,"do":"kick_left","sound":"coo","mode":"roller"}]}
        """)
        let event = try XCTUnwrap(show.tracks["lead"]?.events.first)
        guard case .skill("kick_left") = event.action else { return XCTFail("expected do to take precedence") }
        XCTAssertEqual(event.extraActionKeys, ["sound", "mode"])
        let report = show.validate()
        XCTAssertFalse(report.isValid)
        XCTAssertTrue(report.errors.contains { $0.message.contains("more than one action key") }, "\(report.errors)")
    }

    func testSingleActionKeyHasNoExtras() throws {
        let show = try show(tracks: "{\"events\":[{\"t\":1.0,\"sound\":\"coo\",\"hold\":0.5}]}")
        let event = try XCTUnwrap(show.tracks["lead"]?.events.first)
        XCTAssertEqual(event.extraActionKeys, [])
        guard case .sound("coo", hold: 0.5) = event.action else { return XCTFail("\(event.action)") }
    }

    // MARK: Skill / sound-tag enum parity (python/duckshow/limits.py SKILLS/SOUND_TAGS)

    func testUnrecognizedSkillIsAnError() throws {
        let show = try show(tracks: "{\"events\":[{\"t\":1.0,\"do\":\"moonwalk\"}]}")
        let report = show.validate()
        XCTAssertFalse(report.isValid)
        XCTAssertTrue(
            report.errors.contains { $0.role == "lead" && $0.message.contains("moonwalk") },
            "\(report.errors)"
        )
    }

    func testUnrecognizedSoundTagIsAnError() throws {
        let show = try show(tracks: "{\"events\":[{\"t\":1.0,\"sound\":\"quack\"}]}")
        let report = show.validate()
        XCTAssertFalse(report.isValid)
        XCTAssertTrue(
            report.errors.contains { $0.role == "lead" && $0.message.contains("quack") },
            "\(report.errors)"
        )
    }

    func testEveryRecognizedSkillAndSoundTagIsValid() throws {
        let doEvents = Show.skills.enumerated().map { i, name in "{\"t\":\(Double(i) * 0.5),\"do\":\"\(name)\"}" }
        let soundEvents = Show.soundTags.enumerated().map { i, tag in
            "{\"t\":\(Double(doEvents.count) * 0.5 + Double(i) * 0.5),\"sound\":\"\(tag)\"}"
        }
        let show = try show(
            tracks: "{\"events\":[\(([doEvents, soundEvents].flatMap { $0 }).joined(separator: ","))]}",
            duration: 60
        )
        let report = show.validate()
        XCTAssertTrue(report.isValid, "\(report.errors)")
    }

    // MARK: Mode / locomotion overlap (F42) — fixtures from python/tests/test_validator.py

    private let rollerPolicy = "{\"policies\":[{\"name\":\"p\",\"mode\":\"roller\",\"file\":\"x.onnx\",\"sha256\":\"abc\"}]}"

    private func overlapWarnings(_ report: ValidationReport) -> [ValidationIssue] {
        report.warnings.filter { $0.message.contains("overlaps nonzero locomotion") }
    }

    func testModeOverlapDetectsLocomotionHeldFromEarlierKeyframe() throws {
        let show = try show(tracks: """
        {"locomotion":[{"t":0,"vx":0.1,"interp":"step"},{"t":10,"vx":0}],
         "events":[{"t":5.0,"mode":"roller"}]}
        """, requires: rollerPolicy, duration: 20)
        let report = show.validate()
        XCTAssertEqual(overlapWarnings(report).count, 1, "\(report.warnings)")
        XCTAssertTrue(report.isValid)
    }

    func testModeOverlapPythonFixtureWarnsExactlyOnce() throws {
        // test_mode_overlapping_nonzero_locomotion_warns: window [1.3, 2.3]
        // contains only the zero keyframe at t=2.0, but the ramp is nonzero.
        let show = try show(tracks: """
        {"locomotion":[{"t":0.0,"vx":0.1},{"t":2.0,"vx":0.0}],
         "events":[{"t":1.8,"mode":"roller"}]}
        """, requires: rollerPolicy)
        XCTAssertEqual(overlapWarnings(show.validate()).count, 1)
    }

    func testModeWithZeroLocomotionNearbyDoesNotWarn() throws {
        let show = try show(tracks: """
        {"locomotion":[{"t":0.0,"vx":0.0},{"t":2.0,"vx":0.0}],
         "events":[{"t":1.0,"mode":"roller"}]}
        """, requires: rollerPolicy)
        XCTAssertTrue(overlapWarnings(show.validate()).isEmpty)
    }

    func testModeFarFromLocomotionDoesNotWarn() throws {
        let show = try show(tracks: """
        {"locomotion":[{"t":0.0,"vx":0.1},{"t":1.0,"vx":0.0}],
         "events":[{"t":5.0,"mode":"roller"}]}
        """, requires: rollerPolicy)
        XCTAssertTrue(overlapWarnings(show.validate()).isEmpty)
    }

    func testUnknownModeValueIsErrorNotWarning() throws {
        // Real robotd accepts exactly "walk"/"roller" as a mode event's
        // value -- there is no such thing as a "declared" custom mode
        // name, so an unrecognized value is an ERROR (docs/robotd-api.md
        // "Custom .onnx policies & modes"), regardless of whether
        // `requires.policies` declares anything at all.
        let show = try show(tracks: "{\"events\":[{\"t\":1.0,\"mode\":\"not_a_real_mode\"}]}")
        let report = show.validate()
        XCTAssertFalse(report.isValid)
        XCTAssertTrue(report.errors.contains { $0.message.contains("not a valid drive mode") }, "\(report.errors)")
        XCTAssertTrue(report.warnings.isEmpty, "\(report.warnings)")

        // A recognized drive-mode value is always valid, whether or not a
        // policy declares that name -- `requires.policies` plays no part
        // in whether a `mode` event is valid.
        let declaredButIrrelevant = try self.show(tracks: "{\"events\":[{\"t\":1.0,\"mode\":\"roller\"}]}", requires: rollerPolicy)
        XCTAssertTrue(declaredButIrrelevant.validate().isValid)

        let undeclaredButValid = try self.show(tracks: "{\"events\":[{\"t\":1.0,\"mode\":\"roller\"}]}")
        XCTAssertTrue(undeclaredButValid.validate().isValid)
    }
}
