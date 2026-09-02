import XCTest
@testable import SwarmLink

/// The recorder's pure parts (docs/authoring.md §2): the default controller
/// map, track capture / decimation, scripted input, and the output merge.
final class RecorderTests: XCTestCase {
    // MARK: ControllerMap

    func testDeadZoneZeroesSmallDeflectionsAndRescalesTheRest() {
        XCTAssertEqual(ControllerMap.applyDeadZone(0.05, deadZone: 0.08), 0)
        XCTAssertEqual(ControllerMap.applyDeadZone(-0.05, deadZone: 0.08), 0)
        XCTAssertEqual(ControllerMap.applyDeadZone(0.08, deadZone: 0.08), 0, "the edge itself is inside the dead zone")
        XCTAssertEqual(ControllerMap.applyDeadZone(1.0, deadZone: 0.08), 1.0, accuracy: 1e-12)
        XCTAssertEqual(ControllerMap.applyDeadZone(-1.0, deadZone: 0.08), -1.0, accuracy: 1e-12)
        XCTAssertEqual(ControllerMap.applyDeadZone(0.54, deadZone: 0.08), 0.5, accuracy: 1e-12, "rescaled: (0.54 - 0.08) / 0.92")
        XCTAssertEqual(ControllerMap.applyDeadZone(1.7, deadZone: 0.08), 1.0, "clamped to the stick range")
        XCTAssertEqual(ControllerMap.applyDeadZone(.nan, deadZone: 0.08), 0)
    }

    func testSticksAndTriggersScaleToTheValidationLimits() {
        var map = ControllerMap.default
        let full = map.apply(InputFrame(t: 0, lx: 1, ly: 1, rx: 1, ry: -1, lt: 1, rt: 0.5))
        XCTAssertEqual(full.frame.move, PuppetMove(vx: 0.25, vy: 0.20, vyaw: 1.5))
        XCTAssertEqual(full.frame.head?.headPitch ?? 0, -1.2, accuracy: 1e-12)
        XCTAssertEqual(full.frame.head?.headYaw, 0)
        XCTAssertEqual(full.frame.pose, PuppetPose(z: -0.05, roll: 0, pitch: 0, active: true))
        XCTAssertEqual(full.frame.mouth, PuppetMouth(open: 0.5))
        XCTAssertTrue(full.skills.isEmpty)
        XCTAssertTrue(full.sounds.isEmpty)
        XCTAssertFalse(full.stop)

        let half = map.apply(InputFrame(t: 0.02, ly: 0.54))
        XCTAssertEqual(half.frame.move?.vx ?? 0, 0.125, accuracy: 1e-12, "dead-zoned then scaled")
        XCTAssertEqual(half.frame.pose?.active, false, "no crouch without the trigger")
        XCTAssertEqual(half.frame.pose?.z, 0)

        let rest = map.apply(InputFrame(t: 0.04, lx: 0.03, ly: -0.07, rx: 0.08, ry: 0.01))
        XCTAssertEqual(rest.frame.move, PuppetMove(vx: 0, vy: 0, vyaw: 0), "everything inside the dead zone is 0")
        XCTAssertEqual(rest.frame.head?.headPitch, 0)

        let report = Show(
            format: "duckshow/1", meta: Meta(duration: 1), cast: [CastMember(role: "r")],
            tracks: ["r": RoleTracks(
                locomotion: [LocomotionKeyframe(t: 0, vx: full.frame.move!.vx, vy: full.frame.move!.vy, vyaw: full.frame.move!.vyaw)],
                head: [HeadKeyframe(t: 0, neckPitch: 0, headPitch: full.frame.head!.headPitch, headYaw: 0, headRoll: 0)],
                pose: [PoseKeyframe(t: 0, z: full.frame.pose!.z, roll: 0, pitch: 0, active: true)],
                mouth: [MouthKeyframe(t: 0, open: full.frame.mouth!.open)]
            )]
        ).validate()
        XCTAssertTrue(report.isValid, "full deflection must sit exactly on the limits, never past them: \(report.errors)")
    }

    func testButtonsFireOnThePressEdgeOnly() {
        var map = ControllerMap.default
        let press = map.apply(InputFrame(t: 0, buttons: ["a", "left_shoulder"]))
        XCTAssertEqual(press.sounds, ["chirp"])
        XCTAssertEqual(press.skills, ["kick_left"])
        let hold = map.apply(InputFrame(t: 0.02, buttons: ["a", "left_shoulder"]))
        XCTAssertEqual(hold.sounds, [], "holding a button must not repeat the event")
        XCTAssertEqual(hold.skills, [])
        let release = map.apply(InputFrame(t: 0.04))
        XCTAssertEqual(release.sounds, [])
        let again = map.apply(InputFrame(t: 0.06, buttons: ["a"]))
        XCTAssertEqual(again.sounds, ["chirp"], "a fresh press fires again")

        let others = map.apply(InputFrame(t: 0.08, buttons: ["b", "x", "y", "right_shoulder", "menu"]))
        XCTAssertEqual(others.sounds, ["greet", "coo", "wheee"], "fixed order regardless of set iteration")
        XCTAssertEqual(others.skills, ["kick_right", "sit_toggle"])
        XCTAssertFalse(others.stop)

        let stop = map.apply(InputFrame(t: 0.10, buttons: ["options"]))
        XCTAssertTrue(stop.stop, "options = stop recording")
        let scriptedStop = map.apply(InputFrame(t: 0.12, stopRequested: true))
        XCTAssertTrue(scriptedStop.stop)
    }

    func testDpadStepsHeadYawAndClamps() {
        var map = ControllerMap.default
        XCTAssertEqual(map.apply(InputFrame(t: 0, buttons: ["dpad_left"])).frame.head?.headYaw ?? 0, 0.2, accuracy: 1e-12)
        XCTAssertEqual(map.apply(InputFrame(t: 0.02, buttons: ["dpad_left"])).frame.head?.headYaw ?? 0, 0.2, accuracy: 1e-12, "held: no repeat")
        _ = map.apply(InputFrame(t: 0.04))
        XCTAssertEqual(map.apply(InputFrame(t: 0.06, buttons: ["dpad_left"])).frame.head?.headYaw ?? 0, 0.4, accuracy: 1e-12)
        _ = map.apply(InputFrame(t: 0.08))
        XCTAssertEqual(map.apply(InputFrame(t: 0.10, buttons: ["dpad_right"])).frame.head?.headYaw ?? 0, 0.2, accuracy: 1e-12)
        for k in 0..<20 {
            _ = map.apply(InputFrame(t: 0.2 + Double(k) * 0.04))
            _ = map.apply(InputFrame(t: 0.22 + Double(k) * 0.04, buttons: ["dpad_right"]))
        }
        XCTAssertEqual(map.apply(InputFrame(t: 2)).frame.head?.headYaw ?? 0, -1.2, accuracy: 1e-12, "clamped to the head limit")
        map.reset()
        XCTAssertEqual(map.apply(InputFrame(t: 3)).frame.head?.headYaw, 0)
    }

    // MARK: TrackCapture

    private func moveFrame(vx: Double) -> PuppetFrame {
        PuppetFrame(move: PuppetMove(vx: vx), head: PuppetHead(), pose: PuppetPose(), mouth: PuppetMouth())
    }

    func testStickRampDecimatesToEpsilonSpacedLinearKeyframes() {
        var capture = TrackCapture()
        // 50 samples at 50 Hz, vx climbing 0.005 per tick: below the 0.01
        // epsilon per step, so a keyframe lands every third sample.
        for k in 0..<50 {
            capture.sample(moveFrame(vx: Double(k) * 0.005), at: Double(k) * 0.02)
        }
        let tracks = capture.finish()
        let loco = tracks.locomotion
        XCTAssertEqual(loco.map(\.t), (0..<17).map { TrackCapture.round3(Double($0) * 0.06) } + [0.98], "every third sample, then the closing sample")
        XCTAssertEqual(loco.first?.vx, 0)
        XCTAssertEqual(loco.last?.vx, 0.245, "finish() closes the track with the last sampled value")
        XCTAssertEqual(loco.last?.t, 0.98)
        for (a, b) in zip(loco, loco.dropFirst()) {
            XCTAssertLessThan(a.t, b.t)
            XCTAssertLessThanOrEqual(b.t - a.t, 0.1 + 1e-9)
            XCTAssertLessThan(a.vx, b.vx)
        }
        for (a, b) in zip(loco, loco.dropFirst().dropLast()) {
            XCTAssertGreaterThan(b.vx - a.vx, 0.01, "a keyframe only when the value moved more than epsilon")
        }
        XCTAssertTrue(loco.allSatisfy { $0.interp == .linear })
        XCTAssertEqual(tracks.head.map(\.t), (0..<10).map { TrackCapture.round3(Double($0) * 0.1) } + [0.98], "a still head: the 100 ms rule, then the closing sample")
        XCTAssertEqual(tracks.pose.count, 11)
        XCTAssertEqual(tracks.mouth.count, 11)
        XCTAssertTrue(tracks.events.isEmpty)
        XCTAssertEqual(capture.sampleCount, 50)
    }

    func testHeldValueStillYieldsAKeyframeEvery100ms() {
        var capture = TrackCapture()
        for k in 0...50 {
            capture.sample(moveFrame(vx: 0.1), at: Double(k) * 0.02)
        }
        let loco = capture.finish().locomotion
        XCTAssertEqual(loco.map(\.t), (0...10).map { TrackCapture.round3(Double($0) * 0.1) })
        XCTAssertTrue(loco.allSatisfy { $0.vx == 0.1 })
        for (a, b) in zip(loco, loco.dropFirst()) {
            XCTAssertLessThanOrEqual(b.t - a.t, 0.1 + 1e-9, "≥ one keyframe per 100 ms")
        }
    }

    /// A held value followed by a jump: the sample before the jump is
    /// written too, so playback's linear ramp spans one 20 ms tick instead
    /// of the whole gap back to the last keyframe (up to 100 ms early or
    /// late — several times the sync budget of docs/architecture.md).
    func testAStepChangeIsBracketedSoTheHoldLastsUntilTheTickBeforeIt() {
        var capture = TrackCapture()
        for k in 0..<10 {
            capture.sample(moveFrame(vx: 0.1141), at: Double(k) * 0.02)
        }
        capture.sample(moveFrame(vx: 0), at: 0.20)
        let loco = capture.finish().locomotion
        XCTAssertEqual(loco.map(\.t), [0.0, 0.1, 0.18, 0.2], "the 100 ms rule, then the hold's last tick, then the step")
        XCTAssertEqual(loco.map(\.vx), [0.1141, 0.1141, 0.1141, 0.0])

        // Onset from rest, the same way: 0…0.46 s at rest, full stick at 0.48.
        var onset = TrackCapture()
        for k in 0..<24 {
            onset.sample(moveFrame(vx: 0), at: Double(k) * 0.02)
        }
        onset.sample(moveFrame(vx: 0.25), at: 0.48)
        let ramp = onset.finish().locomotion
        XCTAssertEqual(ramp.suffix(2).map(\.t), [0.46, 0.48], "the duck is still at rest at 0.46, not already at vx 0.125 at 0.44")
        XCTAssertEqual(ramp.suffix(2).map(\.vx), [0, 0.25])
        XCTAssertEqual(ramp.map(\.t).prefix(5), [0.0, 0.1, 0.2, 0.3, 0.4], "the hold itself keeps the 100 ms rule")

        // A mouth step gets the same bracket; a still head none.
        var mouth = TrackCapture()
        for k in 0..<5 {
            mouth.sample(PuppetFrame(head: PuppetHead(headYaw: 0.3), mouth: PuppetMouth(open: 0)), at: Double(k) * 0.02)
        }
        mouth.sample(PuppetFrame(head: PuppetHead(headYaw: 0.3), mouth: PuppetMouth(open: 1)), at: 0.10)
        let tracks = mouth.finish()
        XCTAssertEqual(tracks.mouth.map(\.t), [0.0, 0.08, 0.10])
        XCTAssertEqual(tracks.head.map(\.t), [0.0, 0.10], "no extra keyframe for a value that did not move")
    }

    func testPoseActiveFlipsAlwaysWriteAKeyframe() {
        var capture = TrackCapture()
        for k in 0..<5 {
            capture.sample(PuppetFrame(pose: PuppetPose(z: 0, active: false)), at: Double(k) * 0.02)
        }
        capture.sample(PuppetFrame(pose: PuppetPose(z: -0.001, active: true)), at: 0.10)
        capture.sample(PuppetFrame(pose: PuppetPose(z: -0.001, active: true)), at: 0.12)
        capture.sample(PuppetFrame(pose: PuppetPose(z: 0, active: false)), at: 0.14)
        let pose = capture.finish().pose
        XCTAssertEqual(pose.map(\.t), [0.0, 0.10, 0.14])
        XCTAssertEqual(pose.map(\.active), [false, true, false], "crouch held → active, released → inactive, at the tick it happened")
    }

    func testEventsCloserThanTheSpacingLimitAreDroppedWithAWarning() {
        var capture = TrackCapture()
        capture.sample(PuppetFrame(move: PuppetMove(), sound: "chirp"), at: 0.0)
        capture.sample(PuppetFrame(move: PuppetMove(), sound: "greet"), at: 0.10)
        capture.sample(PuppetFrame(move: PuppetMove(), skill: "kick_left"), at: 0.24)
        capture.sample(PuppetFrame(move: PuppetMove(), skill: "kick_right"), at: 0.26)
        capture.sample(PuppetFrame(move: PuppetMove(), skill: "sit_toggle", sound: "coo"), at: 0.60)
        let tracks = capture.finish()
        XCTAssertEqual(tracks.events.map(\.t), [0.0, 0.26, 0.60])
        XCTAssertEqual(tracks.events.map(\.action), [.sound("chirp", hold: nil), .skill("kick_right"), .skill("sit_toggle")])
        XCTAssertEqual(capture.droppedEvents, 3, "greet, kick_left, and the coo that shared a tick with sit_toggle")
        XCTAssertEqual(capture.warnings.count, 3)
        XCTAssertTrue(capture.warnings[0].contains("greet"), capture.warnings[0])
        XCTAssertTrue(capture.warnings[0].contains("0.25"), capture.warnings[0])
        let report = Show(
            format: "duckshow/1", meta: Meta(duration: 1), cast: [CastMember(role: "r")], tracks: ["r": tracks]
        ).validate()
        XCTAssertTrue(report.isValid, "what the capture keeps must pass the density rule: \(report.errors)")
    }

    // MARK: ScriptedInput

    func testScriptDecodesTheDocumentedFormatWithDefaultsAndSorting() throws {
        let json = """
        [{"t": 0.5, "buttons": ["a"], "future": 1},
         {"t": 0.0, "lx": 0.0, "ly": 0.5, "rx": 0, "ry": 0, "lt": 0, "rt": 0, "buttons": ["a"]},
         {"t": 1.0, "stop": true}]
        """
        let input = ScriptedInput(frames: try ScriptedInput.decode(Data(json.utf8)), name: "t")
        XCTAssertEqual(input.frames.map(\.t), [0.0, 0.5, 1.0], "sorted by t")
        XCTAssertEqual(input.frames[0].ly, 0.5)
        XCTAssertEqual(input.frames[1], InputFrame(t: 0.5, buttons: ["a"]))
        XCTAssertTrue(input.frames[2].stopRequested)
        XCTAssertEqual(input.displayName, "script: t")

        XCTAssertThrowsError(try ScriptedInput.decode(Data("{\"t\": 0}".utf8))) { error in
            XCTAssertEqual(error as? ScriptedInputError, .notAnArray)
        }
        XCTAssertThrowsError(try ScriptedInput.decode(Data("[{\"t\": -1}]".utf8))) { error in
            XCTAssertEqual(error as? ScriptedInputError, .badTime(index: 0, t: -1))
        }
        XCTAssertThrowsError(try ScriptedInput.decode(Data("[{\"lx\": 1}]".utf8)), "t is required")

        let roundTrip = try JSONDecoder().decode([InputFrame].self, from: try JSONEncoder().encode(input.frames))
        XCTAssertEqual(roundTrip, input.frames)
    }

    func testScriptedInputReplaysFramesOnTheMonotonicClock() async throws {
        let input = ScriptedInput(frames: [
            InputFrame(t: 0.0, ly: 0.1), InputFrame(t: 0.1, ly: 0.2), InputFrame(t: 0.2, ly: 0.3), InputFrame(t: 0.3, ly: 0.4)
        ])
        let epoch = MasterClock.nowNanoseconds()
        var arrivals: [(InputFrame, Int64)] = []
        for await frame in input.frames(from: epoch) {
            arrivals.append((frame, MasterClock.nowNanoseconds()))
        }
        XCTAssertEqual(arrivals.map(\.0.ly), [0.1, 0.2, 0.3, 0.4], "in order, then the stream finishes")
        XCTAssertEqual(input.deliveryLead, 0.010)
        for (frame, at) in arrivals {
            let lateMs = Double(at - epoch) / 1e6 - frame.t * 1000
            XCTAssertGreaterThanOrEqual(lateMs, -11, "delivered at most the 10 ms lead early: \(frame.t) arrived \(lateMs) ms off")
            XCTAssertLessThan(lateMs, 70, "frame \(frame.t) arrived \(lateMs) ms late")
        }
    }

    func testScriptedInputWithAPastEpochYieldsDueFramesAtOnce() async {
        let input = ScriptedInput(frames: [InputFrame(t: 0.0), InputFrame(t: 0.05)])
        let epoch = MasterClock.nowNanoseconds() - 5_000_000_000
        let start = MasterClock.nowNanoseconds()
        var count = 0
        for await _ in input.frames(from: epoch) { count += 1 }
        XCTAssertEqual(count, 2)
        XCTAssertLessThan(MasterClock.nowNanoseconds() - start, 500_000_000)
    }

    // MARK: merge

    private func recorded() -> RoleTracks {
        RoleTracks(
            locomotion: [LocomotionKeyframe(t: 0, vx: 0, vy: 0, vyaw: 0), LocomotionKeyframe(t: 0.4, vx: 0.2, vy: 0, vyaw: 0)],
            events: [Event(t: 0.2, action: .sound("chirp", hold: nil))]
        )
    }

    func testMergeReplacesOnlyTheRoleAndPreservesUnknownFields() throws {
        let existing = """
        {"format":"duckshow/1",
         "meta":{"name":"Mine","duration":30.0,"music":{"file":"m.wav","bpm":100.0,"beat_offset":0.5}},
         "requires":{"policies":[]},
         "cast":[{"role":"lead","notes":"front"},{"role":"wing"}],
         "tracks":{"lead":{"locomotion":[{"t":0,"vx":0.1,"vy":0,"vyaw":0}],"servo":[{"t":1,"mode":"hold","duration":2}]},
                   "wing":{"head":[{"t":0,"head_yaw":0.3,"vendor_field":true}]}},
         "editor":{"marks":{"lead":{"x":1,"y":2,"heading":0.5}}}}
        """
        let show = try Show.decode(Data(existing.utf8))
        let merged = try Recorder.merge(
            existing: Data(existing.utf8), role: "lead", tracks: recorded(), recordedSeconds: 0.42,
            layeredOn: show, bpm: nil, beatOffset: 0, outputName: "mine"
        )
        let root = try XCTUnwrap(JSONSerialization.jsonObject(with: merged) as? [String: Any])
        let editor = try XCTUnwrap(root["editor"] as? [String: Any])
        XCTAssertNotNil((editor["marks"] as? [String: Any])?["lead"], "unknown top-level fields survive")
        let tracks = try XCTUnwrap(root["tracks"] as? [String: Any])
        let wing = try XCTUnwrap(tracks["wing"] as? [String: Any])
        let wingHead = try XCTUnwrap((wing["head"] as? [[String: Any]])?.first)
        XCTAssertEqual(wingHead["vendor_field"] as? Bool, true, "other roles are untouched byte-for-byte")
        let lead = try XCTUnwrap(tracks["lead"] as? [String: Any])
        XCTAssertEqual((lead["locomotion"] as? [Any])?.count, 2)
        XCTAssertNil(lead["servo"], "the role's previous tracks are replaced, not merged")
        XCTAssertNil(lead["head"], "empty tracks are omitted")
        let meta = try XCTUnwrap(root["meta"] as? [String: Any])
        XCTAssertEqual((meta["duration"] as? NSNumber)?.doubleValue, 30.0, "layered on a show: the file's duration stays")
        XCTAssertEqual(((meta["music"] as? [String: Any])?["bpm"] as? NSNumber)?.doubleValue, 100.0)
        XCTAssertEqual((root["cast"] as? [Any])?.count, 2)

        let decoded = try Show.decode(merged)
        XCTAssertEqual(decoded.tracks["lead"], recorded())
        XCTAssertTrue(decoded.validate().isValid)
    }

    /// docs/duckshow-format.md's "diff-friendly" goal: the capture rounds
    /// to 1 ms / 1e-4, and the file must print those numbers as such —
    /// `JSONSerialization` would write 0.1 as 0.10000000000000001 and noise
    /// every number of the other roles on each re-save.
    func testMergeWritesShortestDoublesAndKeepsOtherRolesNumbersClean() throws {
        let existing = """
        {"format":"duckshow/1","meta":{"duration":30.0},
         "cast":[{"role":"lead"},{"role":"wing"}],
         "tracks":{"lead":{},"wing":{"head":[{"t":0.3,"head_yaw":0.3},{"t":0.7,"head_yaw":0.1,"interp":"step"}],
                                     "pose":[{"t":0.1,"z":-0.01,"active":true}],
                                     "events":[{"t":2.2,"sound":"chirp","hold":1.5}]}},
         "editor":{"marks":{"wing":{"x":0.1,"y":-0.3,"heading":0.7}}}}
        """
        let tracks = RoleTracks(
            locomotion: [LocomotionKeyframe(t: 0, vx: 0, vy: 0, vyaw: 0), LocomotionKeyframe(t: 0.1, vx: 0.3, vy: 0, vyaw: 0)],
            mouth: [MouthKeyframe(t: 0.2, open: 0.7)]
        )
        let merged = try Recorder.merge(
            existing: Data(existing.utf8), role: "lead", tracks: tracks, recordedSeconds: 0.3,
            layeredOn: nil, bpm: nil, beatOffset: 0, outputName: "x"
        )
        let text = try XCTUnwrap(String(data: merged, encoding: .utf8))
        for noisy in ["0.10000000000000001", "0.29999999999999999", "0.69999999999999996", "0.30000000000000004"] {
            XCTAssertFalse(text.contains(noisy), "17-digit doubles in the file: \(text)")
        }
        let numbers = text.split(whereSeparator: { !"0123456789.-".contains($0) }).map(String.init)
        for number in numbers {
            XCTAssertLessThanOrEqual(number.count, 8, "'\(number)' is not a shortest round-trip double: \(text)")
        }
        XCTAssertTrue(text.contains("\"active\" : true"), "booleans stay booleans (not 1): \(text)")
        XCTAssertTrue(text.contains("\"interp\" : \"step\""), text)

        let show = try Show.decode(merged)
        XCTAssertEqual(show.meta.duration, 30)
        XCTAssertEqual(show.tracks["lead"], tracks, "the values themselves are exact")
        XCTAssertEqual(show.tracks["wing"]?.head.map(\.headYaw), [0.3, 0.1])
        XCTAssertEqual(show.tracks["wing"]?.head.map(\.interp), [.linear, .step])
        XCTAssertEqual(show.tracks["wing"]?.pose.first?.active, true)
        XCTAssertEqual(show.tracks["wing"]?.events.first?.action, .sound("chirp", hold: 1.5))
        let root = try XCTUnwrap(JSONSerialization.jsonObject(with: merged) as? [String: Any])
        let marks = try XCTUnwrap(((root["editor"] as? [String: Any])?["marks"] as? [String: Any])?["wing"] as? [String: Any])
        XCTAssertEqual((marks["y"] as? NSNumber)?.doubleValue, -0.3, "unknown fields survive")

        // Re-merging the file it produced changes nothing but the role.
        let again = try Recorder.merge(
            existing: merged, role: "lead", tracks: tracks, recordedSeconds: 0.3,
            layeredOn: nil, bpm: nil, beatOffset: 0, outputName: "x"
        )
        XCTAssertEqual(again, merged, "a second pass is byte-for-byte stable")
    }

    func testMergeCreatesAOneRoleShowAndRoundsDurationUpToTheBeat() throws {
        let merged = try Recorder.merge(
            existing: nil, role: "solo", tracks: recorded(), recordedSeconds: 0.42,
            layeredOn: nil, bpm: 120, beatOffset: 0, outputName: "take1"
        )
        let show = try Show.decode(merged)
        XCTAssertEqual(show.format, "duckshow/1")
        XCTAssertEqual(show.cast.map(\.role), ["solo"])
        XCTAssertEqual(show.meta.name, "take1")
        XCTAssertEqual(show.meta.duration, 0.5, "0.42 s rounded up to the next beat at 120 bpm")
        XCTAssertEqual(show.meta.music?.bpm, 120)
        XCTAssertEqual(show.meta.music?.beatOffset, 0)
        XCTAssertNotNil(show.meta.created)
        XCTAssertEqual(show.tracks["solo"], recorded())
        XCTAssertTrue(show.validate().isValid, "\(show.validate().errors)")

        let noBeat = try Show.decode(try Recorder.merge(
            existing: nil, role: "solo", tracks: recorded(), recordedSeconds: 0.42,
            layeredOn: nil, bpm: nil, beatOffset: 0, outputName: "take2"
        ))
        XCTAssertEqual(noBeat.meta.duration, 0.42, "without a beat grid the duration is the recorded length")
        XCTAssertNil(noBeat.meta.music)
    }

    func testMergeAddsAMissingRoleToTheCastAndNeverShrinksAnExistingDuration() throws {
        let existing = """
        {"format":"duckshow/1","meta":{"duration":30.0},"cast":[{"role":"wing"}],"tracks":{"wing":{}}}
        """
        let merged = try Recorder.merge(
            existing: Data(existing.utf8), role: "lead", tracks: recorded(), recordedSeconds: 12.0,
            layeredOn: nil, bpm: nil, beatOffset: 0, outputName: "x"
        )
        let show = try Show.decode(merged)
        XCTAssertEqual(show.cast.map(\.role), ["wing", "lead"])
        XCTAssertEqual(show.meta.duration, 30.0, "the other role's 30 s are not cut to the 12 s take")
        XCTAssertEqual(Set(show.tracks.keys), ["wing", "lead"])
        XCTAssertTrue(show.validate().isValid)

        let longer = try Show.decode(try Recorder.merge(
            existing: Data(existing.utf8), role: "lead", tracks: recorded(), recordedSeconds: 31.3,
            layeredOn: nil, bpm: 60, beatOffset: 0.5, outputName: "x"
        ))
        XCTAssertEqual(longer.meta.duration, 31.5, "a longer take extends the show to the next beat (offset 0.5 + k × 1 s)")
    }

    /// Layering on a longer show than the output file has: the take ran to
    /// the show's end, so the file's duration follows — otherwise the tail
    /// of the fresh take would sit past `meta.duration`, unreachable in
    /// playback and flagged by no validator.
    func testMergeLayeredOnALongerShowExtendsAShorterExistingDuration() throws {
        let existing = """
        {"format":"duckshow/1","meta":{"duration":5.0},"cast":[{"role":"lead"}],"tracks":{"lead":{}}}
        """
        let base = Show(
            format: "duckshow/1", meta: Meta(duration: 30),
            cast: [CastMember(role: "lead"), CastMember(role: "wing")], tracks: ["lead": RoleTracks(), "wing": RoleTracks()]
        )
        let late = RoleTracks(
            locomotion: [LocomotionKeyframe(t: 0, vx: 0, vy: 0, vyaw: 0), LocomotionKeyframe(t: 29.5, vx: 0.1, vy: 0, vyaw: 0)],
            events: [Event(t: 29.8, action: .sound("chirp", hold: nil))]
        )
        let show = try Show.decode(try Recorder.merge(
            existing: Data(existing.utf8), role: "lead", tracks: late, recordedSeconds: 30,
            layeredOn: base, bpm: nil, beatOffset: 0, outputName: "x"
        ))
        XCTAssertEqual(show.meta.duration, 30, "extended to the timeline the take was recorded against")
        XCTAssertEqual(show.tracks["lead"]?.events.first?.t, 29.8)

        let longerFile = """
        {"format":"duckshow/1","meta":{"duration":45.0},"cast":[{"role":"wing"}],"tracks":{"wing":{}}}
        """
        let kept = try Show.decode(try Recorder.merge(
            existing: Data(longerFile.utf8), role: "lead", tracks: late, recordedSeconds: 30,
            layeredOn: base, bpm: nil, beatOffset: 0, outputName: "x"
        ))
        XCTAssertEqual(kept.meta.duration, 45, "never shrinks under the other roles' tracks")
    }

    func testMergeCreatingFromAShowCopiesItsMeta() throws {
        let base = Show(
            format: "duckshow/1",
            meta: Meta(name: "Base", author: "t", created: "2026-01-01", duration: 20, music: Music(file: "b.wav", bpm: 90)),
            cast: [CastMember(role: "lead"), CastMember(role: "wing")], tracks: ["lead": RoleTracks(), "wing": RoleTracks()]
        )
        let show = try Show.decode(try Recorder.merge(
            existing: nil, role: "lead", tracks: recorded(), recordedSeconds: 5,
            layeredOn: base, bpm: nil, beatOffset: 0, outputName: "new"
        ))
        XCTAssertEqual(show.meta.duration, 20, "the show's duration, not the take's")
        XCTAssertEqual(show.meta.music?.bpm, 90)
        XCTAssertEqual(show.cast.map(\.role), ["lead"], "a fresh output gets a one-role cast")
        XCTAssertEqual(show.tracks["lead"], recorded())
    }

    func testRoundUpToBeat() {
        XCTAssertEqual(Recorder.roundUpToBeat(0.45, bpm: 120, beatOffset: 0), 0.5, accuracy: 1e-9)
        XCTAssertEqual(Recorder.roundUpToBeat(0.5, bpm: 120, beatOffset: 0), 0.5, accuracy: 1e-9, "already on the beat")
        XCTAssertEqual(Recorder.roundUpToBeat(1.01, bpm: 120, beatOffset: 0.25), 1.25, accuracy: 1e-9)
        XCTAssertEqual(Recorder.roundUpToBeat(0, bpm: 120, beatOffset: 0), 0.5, accuracy: 1e-9, "never a zero duration")
        XCTAssertEqual(Recorder.roundUpToBeat(3.3, bpm: 0, beatOffset: 0), 3.3, "no grid: unchanged")
        // A take shorter than the beat offset ends on the first downbeat —
        // a grid point — not on the beat length, which is on no beat here.
        XCTAssertEqual(Recorder.roundUpToBeat(0.2, bpm: 120, beatOffset: 0.3), 0.3, accuracy: 1e-9)
        XCTAssertEqual(Recorder.roundUpToBeat(0.3, bpm: 120, beatOffset: 0.3), 0.3, accuracy: 1e-9, "exactly on the downbeat")
        XCTAssertEqual(Recorder.roundUpToBeat(0.31, bpm: 120, beatOffset: 0.3), 0.8, accuracy: 1e-9)
        XCTAssertEqual(Recorder.roundUpToBeat(0, bpm: 120, beatOffset: 0.3), 0.3, accuracy: 1e-9, "never zero, still on the grid")
        XCTAssertEqual(Recorder.roundUpToBeat(0.2, bpm: 120, beatOffset: 5.0), 0.5, accuracy: 1e-9, "the grid extends before the downbeat")
        XCTAssertEqual(Recorder.roundUpToBeat(0.2, bpm: 120, beatOffset: -0.2), 0.3, accuracy: 1e-9)
    }
}
