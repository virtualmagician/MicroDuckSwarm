import XCTest
@testable import SwarmLink

/// The puppet channel and the recorder over real UDP against `TestDuck`s:
/// stamped, unacknowledged frames; a scripted take layered on a show
/// (temp show loaded then removed, 50 Hz stream with monotonic seq, the
/// role's tracks merged and valid); a take from t=0; an interrupted take.
final class RecorderEndToEndTests: XCTestCase {
    private var tmpDir: URL?
    private let duck01 = DuckID("duck-01")
    private let duck02 = DuckID("duck-02")

    override func setUpWithError() throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent("rec-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        tmpDir = dir
    }

    override func tearDown() {
        if let tmpDir { try? FileManager.default.removeItem(at: tmpDir) }
        tmpDir = nil
        super.tearDown()
    }

    /// A two-role show where `lead` already has tracks (so emptying them
    /// in the temp copy is observable) and `wing` has its own.
    private func writeBaseShow(duration: Double) throws -> URL {
        let json = """
        {"format":"duckshow/1",
         "meta":{"name":"base","author":"test","created":"2026-01-01","duration":\(duration),"music":{"bpm":120.0,"beat_offset":0.0}},
         "requires":{"policies":[]},
         "cast":[{"role":"lead"},{"role":"wing"}],
         "tracks":{"lead":{"locomotion":[{"t":0.0,"vx":0.1,"vy":0,"vyaw":0}],"events":[{"t":0.5,"sound":"greet"}]},
                   "wing":{"head":[{"t":0.0,"head_yaw":0.4,"vendor_note":"keep me"}]}},
         "editor":{"marks":{"wing":{"x":1,"y":0,"heading":0}}}}
        """
        let dir = try XCTUnwrap(tmpDir)
        let showsDir = dir.appendingPathComponent("shows", isDirectory: true)
        try FileManager.default.createDirectory(at: showsDir, withIntermediateDirectories: true)
        let url = showsDir.appendingPathComponent("base.duckshow.json")
        try Data(json.utf8).write(to: url)
        return url
    }

    private func tempShows(in showsDir: URL) throws -> [URL] {
        try FileManager.default.contentsOfDirectory(at: showsDir, includingPropertiesForKeys: nil)
            .filter { $0.lastPathComponent.hasPrefix("rec-") }
    }

    // MARK: SwarmMaster.puppet

    func testPuppetFramesAreStampedMonotonicAndNeverRetried() async throws {
        let duck = TestDuck()
        let port = try await duck.start()
        let dir = try XCTUnwrap(tmpDir)
        let roster = try Fixtures.writeRoster([RosterEntry(id: "duck-01", host: "127.0.0.1", port: port, role: "lead")], in: dir)
        let master = SwarmMaster(masterPort: 0)
        try await master.connect(roster: roster)

        let before = MasterClock.nowNanoseconds()
        let wallMsBefore = Int(Date().timeIntervalSince1970 * 1000)
        let first = try await master.puppet(duck: duck01, frame: PuppetFrame(move: PuppetMove(vx: 0.1)))
        let second = try await master.puppet(duck: duck01, frame: PuppetFrame(seq: 1, masterTime: 1, head: PuppetHead(headYaw: 0.2)))
        let third = try await master.puppet(duck: duck01, frame: PuppetFrame(sound: "chirp"))
        XCTAssertGreaterThan(first.seq, 0)
        // Seeded from the wall-clock millisecond, like python/tools/puppet.py,
        // so a stream from either sender started within the agent's 2 s
        // seq-reset window of the other's is never dropped as stale.
        XCTAssertGreaterThan(first.seq, wallMsBefore, "seq seed must be comparable with the Python tool's wall-clock seed")
        XCTAssertLessThan(first.seq, wallMsBefore + 60_000)
        XCTAssertEqual(second.seq, first.seq + 1, "the caller's seq/master_time are overwritten by the master's stamps")
        XCTAssertEqual(third.seq, first.seq + 2)
        XCTAssertGreaterThanOrEqual(try XCTUnwrap(first.masterTime), before)
        XCTAssertLessThanOrEqual(try XCTUnwrap(first.masterTime), try XCTUnwrap(second.masterTime))

        let received = await duck.waitForPuppets(count: 3)
        XCTAssertEqual(received.map(\.seq), [first.seq, second.seq, third.seq])
        XCTAssertEqual(received[0].move, PuppetMove(vx: 0.1))
        XCTAssertEqual(received[1].head, PuppetHead(headYaw: 0.2))
        XCTAssertNil(received[1].move, "a frame carries only what the sender asserted")
        XCTAssertEqual(received[2].sound, "chirp")
        XCTAssertTrue(received.allSatisfy { $0.v == 1 && $0.masterTime != nil })

        await Task.sleepMs(SwarmLinkInfo.commandMaxAttempts * Int(SwarmLinkInfo.commandRetryIntervalMs) + 100)
        let later = await duck.receivedPuppets.count
        XCTAssertEqual(later, 3, "no ACK is expected, so nothing is ever retried")
        let commands = await duck.received
        XCTAssertTrue(commands.isEmpty, "puppet traffic is not a command")

        do {
            _ = try await master.puppet(duck: "ghost", frame: PuppetFrame())
            XCTFail("a duck that is not dialed must be refused")
        } catch {
            XCTAssertEqual(error as? SwarmMasterError, .notConnected("ghost"))
        }
        await duck.stop()
    }

    // MARK: record, layered on a show

    func testScriptedTakeLayeredOnAShow() async throws {
        let lead = TestDuck(id: "duck-01")
        let wing = TestDuck(id: "duck-02")
        let leadPort = try await lead.start()
        let wingPort = try await wing.start()
        let dir = try XCTUnwrap(tmpDir)
        let showURL = try writeBaseShow(duration: 1.0)
        let showsDir = showURL.deletingLastPathComponent()
        let roster = try Fixtures.writeRoster([
            RosterEntry(id: "duck-01", host: "127.0.0.1", port: leadPort, role: "lead"),
            RosterEntry(id: "duck-02", host: "127.0.0.1", port: wingPort, role: "wing")
        ], in: dir)
        let outputURL = dir.appendingPathComponent("out").appendingPathComponent("mine.duckshow.json")

        // Walk forward faster and faster; chirp at 0.1 s and again at
        // 0.2 s (too close: dropped); crouch from 0.5 s; the script outlives
        // the 1 s show, so the show's end is what stops the take.
        let script = ScriptedInput(frames: [
            InputFrame(t: 0.0, ly: 0.0),
            InputFrame(t: 0.1, ly: 0.5, buttons: ["a"]),
            InputFrame(t: 0.15),
            InputFrame(t: 0.2, buttons: ["a"]),
            InputFrame(t: 0.3, ly: 1.0),
            InputFrame(t: 0.5, ly: 1.0, lt: 1.0),
            InputFrame(t: 2.0, ly: 1.0, lt: 1.0)
        ], name: "take")
        let master = SwarmMaster(masterPort: 0)
        let logLines = LogSink()
        let recorder = Recorder(
            master: master, input: script,
            configuration: RecorderConfiguration(
                rosterURL: roster, duck: duck01, role: "lead", outputURL: outputURL,
                showURL: showURL, showsDirectory: showsDir, leadSeconds: 0.25
            ),
            log: { line in logLines.append(line) }
        )

        let run = Task { try await recorder.run() }

        // While the take runs: the temp show (lead emptied, wing intact) is
        // on disk and was what the whole roster loaded.
        let loads = await lead.waitForCommands(named: "load", count: 1, timeoutMs: 3000)
        guard case .load(let tempID, _, let leadRole)? = loads.first?.payload else {
            return XCTFail("the roster must load the temp show first: \(loads)")
        }
        XCTAssertTrue(tempID.hasPrefix("rec-"), tempID)
        XCTAssertEqual(leadRole, "lead")
        let tempURL = showsDir.appendingPathComponent("\(tempID).duckshow.json")
        let tempShow = try Show.load(contentsOf: tempURL)
        XCTAssertEqual(tempShow.tracks["lead"], RoleTracks(), "the recorded role stands idle on the timeline")
        XCTAssertEqual(tempShow.tracks["wing"]?.head.first?.headYaw, 0.4, "the rest of the cast performs")
        XCTAssertEqual(tempShow.meta.duration, 1.0)
        let recorderTemp = await recorder.temporaryShowURL
        XCTAssertEqual(recorderTemp?.lastPathComponent, tempURL.lastPathComponent)
        let wingLoads = await wing.waitForCommands(named: "load", count: 1, timeoutMs: 3000)
        XCTAssertEqual(wingLoads.count, 1, "the whole roster loads the temp show")

        let result = try await run.value

        // Cleanup and outcome.
        XCTAssertFalse(FileManager.default.fileExists(atPath: tempURL.path), "temp show removed on exit")
        XCTAssertTrue(try tempShows(in: showsDir).isEmpty)
        let recorderTempAfter = await recorder.temporaryShowURL
        XCTAssertNil(recorderTempAfter)
        XCTAssertTrue(result.written)
        XCTAssertFalse(result.interrupted)
        XCTAssertEqual(result.recordedSeconds, 1.0, accuracy: 1e-9, "the take ends at the show's meta.duration, on a nominal tick")
        XCTAssertEqual(result.framesSent, 51, "50 ticks over 1 s plus the neutral closing frame")

        // The puppet stream: ~50 Hz to the recorded duck only, seq monotonic.
        let puppets = await lead.waitForPuppets(count: 51)
        XCTAssertEqual(puppets.count, 51)
        for (a, b) in zip(puppets, puppets.dropFirst()) {
            XCTAssertLessThan(a.seq, b.seq, "seq must be strictly monotonic")
            XCTAssertLessThanOrEqual(try XCTUnwrap(a.masterTime), try XCTUnwrap(b.masterTime))
        }
        let arrivals = await lead.puppetArrivalNs
        let spanMs = Double(arrivals[49] - arrivals[0]) / 1e6
        XCTAssertEqual(spanMs, 980, accuracy: 120, "49 intervals of 20 ms (±jitter)")
        let wingPuppets = await wing.receivedPuppets
        XCTAssertTrue(wingPuppets.isEmpty, "only the recorded duck is puppeteered")
        XCTAssertEqual(puppets.first?.move, PuppetMove(vx: 0, vy: 0, vyaw: 0))
        XCTAssertEqual(puppets.last?.move, PuppetMove(vx: 0, vy: 0, vyaw: 0), "the closing frame is neutral")
        XCTAssertEqual(puppets.last?.pose?.active, false)
        XCTAssertEqual(puppets.filter { $0.sound == "chirp" }.count, 2, "both presses are streamed live; only the capture drops the second")
        XCTAssertTrue(puppets[40].pose?.active ?? false, "crouch held from 0.5 s")
        XCTAssertEqual(puppets[40].move?.vx ?? 0, 0.25, accuracy: 1e-9)

        // The commands the roster saw: load, play, and the stop that ends the session.
        let leadNames = await lead.received.map(\.payload.cmdName)
        XCTAssertEqual(Array(leadNames.prefix(2)), ["load", "play"])
        XCTAssertFalse(leadNames.contains("panic"), "a take that ran to the end is not panicked")
        let plays = await lead.commands(named: "play")
        guard case .play(let playedID, _, let fromShowTime)? = plays.first?.payload else { return XCTFail("\(plays)") }
        XCTAssertEqual(playedID, tempID)
        XCTAssertEqual(fromShowTime, 0)
        let transport = await master.currentTransport
        XCTAssertEqual(transport, .stopped)

        // The output: the role's tracks merged into a fresh one-role show
        // based on the base show's meta.
        let output = try Show.load(contentsOf: outputURL)
        XCTAssertEqual(output.cast.map(\.role), ["lead"])
        XCTAssertEqual(output.meta.duration, 1.0, "layered: the show's duration")
        XCTAssertEqual(output.meta.music?.bpm, 120)
        let tracks = try XCTUnwrap(output.tracks["lead"])
        XCTAssertEqual(tracks, result.tracks)
        XCTAssertGreaterThanOrEqual(tracks.locomotion.count, 10)
        XCTAssertEqual(tracks.locomotion.first?.t, 0)
        XCTAssertEqual(tracks.locomotion.first?.vx, 0)
        XCTAssertEqual(tracks.locomotion.last?.t, 1.0)
        XCTAssertEqual(tracks.locomotion.last?.vx, 0, "the track ends at rest")
        let peak = tracks.locomotion.map(\.vx).max() ?? 0
        XCTAssertEqual(peak, 0.25, accuracy: 1e-9, "full stick = the vx limit")
        XCTAssertEqual(tracks.events.count, 1)
        XCTAssertEqual(tracks.events.first?.action, .sound("chirp", hold: nil))
        XCTAssertEqual(tracks.events.first?.t ?? 0, 0.1, accuracy: 0.05)
        XCTAssertEqual(result.warnings.count, 1, "the second chirp, 0.1 s after the first, is dropped with a warning")
        XCTAssertTrue(result.warnings[0].contains("chirp"), result.warnings[0])
        XCTAssertTrue(tracks.pose.contains { $0.active && $0.z == -0.05 }, "crouch captured")
        XCTAssertTrue(tracks.locomotion.allSatisfy { $0.interp == .linear })
        for (a, b) in zip(tracks.locomotion, tracks.locomotion.dropFirst()) {
            XCTAssertLessThanOrEqual(b.t - a.t, 0.1 + 1e-9, "≥ one keyframe per 100 ms")
        }
        XCTAssertTrue(result.validation.isValid, "\(result.validation.errors)")
        XCTAssertEqual(output.validate(), result.validation)

        // The base show itself was never touched.
        let base = try Show.load(contentsOf: showURL)
        XCTAssertEqual(base.tracks["lead"]?.locomotion.first?.vx, 0.1)

        let lines = logLines.lines
        XCTAssertTrue(lines.contains { $0.hasPrefix("GO") }, "\(lines)")
        XCTAssertTrue(lines.contains { $0.contains("warning:") && $0.contains("chirp") }, "\(lines)")
        XCTAssertTrue(lines.contains { $0.contains("wrote") && $0.contains("(valid)") }, "\(lines)")

        await lead.stop()
        await wing.stop()
    }

    // MARK: record from t=0 (no show)

    func testScriptedTakeWithoutAShowCreatesAOneRoleShowOnTheBeatGrid() async throws {
        let duck = TestDuck()
        let port = try await duck.start()
        let dir = try XCTUnwrap(tmpDir)
        let roster = try Fixtures.writeRoster([RosterEntry(id: "duck-01", host: "127.0.0.1", port: port, role: "lead")], in: dir)
        let outputURL = dir.appendingPathComponent("solo.duckshow.json")
        // Turn on the spot, then press the stop button at 0.45 s.
        let script = ScriptedInput(frames: [
            InputFrame(t: 0.0, rx: 1.0),
            InputFrame(t: 0.45, rx: 1.0, buttons: ["options"])
        ])
        let master = SwarmMaster(masterPort: 0)
        let recorder = Recorder(
            master: master, input: script,
            configuration: RecorderConfiguration(
                rosterURL: roster, duck: duck01, role: "solo", outputURL: outputURL,
                bpm: 120, beatOffset: 0, leadSeconds: 0.1
            )
        )
        let result = try await recorder.run()
        XCTAssertTrue(result.written)
        XCTAssertFalse(result.interrupted)
        XCTAssertEqual(result.recordedSeconds, 0.46, accuracy: 1e-9,
                       "the take ends at the first tick at or after the options press's t — on every run, not just slow ones")
        XCTAssertEqual(result.framesSent, 24, "23 ticks (t = 0 … 0.44) plus the neutral closing frame at 0.46")

        let commands = await duck.received.map(\.payload.cmdName)
        XCTAssertTrue(commands.isEmpty, "no show: nothing is loaded or played; the duck is driven in puppet mode only (\(commands))")
        let puppets = await duck.waitForPuppets(count: result.framesSent)
        XCTAssertEqual(puppets.count, result.framesSent)
        XCTAssertGreaterThanOrEqual(puppets.count, 20)
        XCTAssertEqual(puppets[5].move?.vyaw ?? 0, 1.5, accuracy: 1e-9)
        XCTAssertEqual(puppets.last?.move?.vyaw, 0, "closing frame at rest")
        for (a, b) in zip(puppets, puppets.dropFirst()) { XCTAssertLessThan(a.seq, b.seq) }

        let show = try Show.load(contentsOf: outputURL)
        XCTAssertEqual(show.cast.map(\.role), ["solo"])
        XCTAssertEqual(show.meta.duration, 0.5, "rounded up to the next beat at 120 bpm")
        XCTAssertEqual(show.meta.music?.bpm, 120)
        XCTAssertEqual(show.meta.name, "solo")
        let loco = try XCTUnwrap(show.tracks["solo"]?.locomotion)
        XCTAssertEqual(loco.first?.vyaw ?? 0, 1.5, accuracy: 1e-9)
        XCTAssertEqual(loco.last?.vyaw, 0)
        XCTAssertTrue(result.validation.isValid, "\(result.validation.errors)")
        await duck.stop()
    }

    /// Regression test for the capture race fixed in `Recorder.foldSchedule`
    /// (Sources/SwarmLink/Recorder.swift): the pre-fix recorder resolved
    /// each tick's puppet frame from `latestContinuous`, a var mutated only
    /// by asynchronously consuming `ScriptedInput.frames(from:)` — a stream
    /// that was *supposed* to deliver a frame scripted for show time `t`
    /// before the tick at `t` (a fixed 10 ms lead plus a 5 ms tick delay),
    /// but that is a guess about scheduler latency, not a guarantee: on a
    /// loaded CI runner it sometimes lost the race, silently capturing the
    /// previous (often rest) value instead of what was actually scripted —
    /// see CI run 33594893037 for three examples.
    ///
    /// `BrokenDeliveryInput` below provokes exactly that failure shape
    /// deterministically, without depending on real machine load: it wraps
    /// a normal script but its `frames(from:)` never delivers anything at
    /// all — an infinitely slow machine, not just a loaded one. On the
    /// pre-fix recorder this reproducibly captured all-neutral frames (or,
    /// worse, ended the take at t=0 the instant the empty stream finished,
    /// via `markInputEnded`). The fix resolves a `ScheduledPuppetInputSource`
    /// like `ScriptedInput` from its `scheduledFrames` directly, folded
    /// against each tick's own show time (`Recorder.foldSchedule`) — the
    /// recorder never subscribes to `frames(from:)` for a scheduled input
    /// at all, so breaking that stream must have zero effect on what gets
    /// captured. This test is the same shape as
    /// `testScriptedTakeWithoutAShowCreatesAOneRoleShowOnTheBeatGrid` so the
    /// two can be compared directly.
    func testScheduledInputIsResolvedFromItsScheduleNeverFromDeliveryTiming() async throws {
        let duck = TestDuck()
        let port = try await duck.start()
        let dir = try XCTUnwrap(tmpDir)
        let roster = try Fixtures.writeRoster([RosterEntry(id: "duck-01", host: "127.0.0.1", port: port, role: "lead")], in: dir)
        let outputURL = dir.appendingPathComponent("broken-delivery.duckshow.json")
        let script = ScriptedInput(frames: [
            InputFrame(t: 0.0, rx: 1.0),
            InputFrame(t: 0.45, rx: 1.0, buttons: ["options"])
        ])
        let master = SwarmMaster(masterPort: 0)
        let recorder = Recorder(
            master: master, input: BrokenDeliveryInput(script: script),
            configuration: RecorderConfiguration(
                rosterURL: roster, duck: duck01, role: "solo", outputURL: outputURL,
                bpm: 120, beatOffset: 0, leadSeconds: 0.1
            )
        )
        let result = try await recorder.run()
        XCTAssertTrue(result.written, "a broken delivery stream must not starve the take")
        XCTAssertFalse(result.interrupted)
        XCTAssertEqual(result.recordedSeconds, 0.46, accuracy: 1e-9)
        XCTAssertEqual(result.framesSent, 24)

        let puppets = await duck.waitForPuppets(count: result.framesSent)
        XCTAssertEqual(puppets.count, result.framesSent)
        XCTAssertEqual(puppets[5].move?.vyaw ?? 0, 1.5, accuracy: 1e-9, "the scripted value, not the neutral rest the old race would have captured")
        XCTAssertEqual(puppets.last?.move?.vyaw, 0, "closing frame at rest")

        let show = try Show.load(contentsOf: outputURL)
        let loco = try XCTUnwrap(show.tracks["solo"]?.locomotion)
        XCTAssertEqual(loco.first?.vyaw ?? 0, 1.5, accuracy: 1e-9)
        XCTAssertEqual(loco.last?.vyaw, 0)
        XCTAssertTrue(result.validation.isValid, "\(result.validation.errors)")
        await duck.stop()
    }

    func testScriptRunningOutEndsTheTakeAtItsLastFrameOnTheSameTickEveryRun() async throws {
        let duck = TestDuck()
        let port = try await duck.start()
        let dir = try XCTUnwrap(tmpDir)
        let roster = try Fixtures.writeRoster([RosterEntry(id: "duck-01", host: "127.0.0.1", port: port, role: "lead")], in: dir)
        let outputURL = dir.appendingPathComponent("short.duckshow.json")
        // No stop button: the script simply runs out at t = 0.30. A frame
        // holds until the next one, so the last frame marks the end and its
        // values never play; the take closes on the tick at 0.30 exactly.
        // (A script's frames arrive 10 ms early and ticks fire 5 ms late,
        // which used to race: the end was noticed either at the 0.30 tick
        // or one tick later, and meta.duration flipped between beats.)
        let script = ScriptedInput(frames: [
            InputFrame(t: 0.0, ly: 1.0),
            InputFrame(t: 0.30, ly: -1.0)
        ])
        let master = SwarmMaster(masterPort: 0)
        let recorder = Recorder(
            master: master, input: script,
            configuration: RecorderConfiguration(
                rosterURL: roster, duck: duck01, role: "solo", outputURL: outputURL,
                bpm: 120, beatOffset: 0, leadSeconds: 0.1
            )
        )
        let result = try await recorder.run()
        XCTAssertTrue(result.written)
        XCTAssertEqual(result.recordedSeconds, 0.30, accuracy: 1e-9, "the take ends at the last frame's t")
        XCTAssertEqual(result.framesSent, 16, "15 ticks (t = 0 … 0.28) plus the neutral closing frame at 0.30")

        let puppets = await duck.waitForPuppets(count: result.framesSent)
        XCTAssertEqual(puppets.count, 16)
        XCTAssertEqual(puppets[14].move?.vx ?? 0, 0.25, accuracy: 1e-9, "held at full stick to the last tick before the end")
        XCTAssertEqual(puppets.last?.move, PuppetMove(vx: 0, vy: 0, vyaw: 0), "closing frame at rest, never the last frame's -1.0")
        XCTAssertFalse(puppets.contains { ($0.move?.vx ?? 0) < 0 }, "the last frame's values never play")

        let show = try Show.load(contentsOf: outputURL)
        XCTAssertEqual(show.meta.duration, 0.5, "0.30 s rounded up to the beat at 120 bpm — the same on every run")
        let loco = try XCTUnwrap(show.tracks["solo"]?.locomotion)
        XCTAssertEqual(loco.last?.t, 0.30)
        XCTAssertEqual(loco.last?.vx, 0)
        await duck.stop()
    }

    // MARK: interrupt

    func testCancelDuringTheCountdownPanicsRemovesTheTempShowAndWritesNothing() async throws {
        let duck = TestDuck()
        let port = try await duck.start()
        let dir = try XCTUnwrap(tmpDir)
        let showURL = try writeBaseShow(duration: 5.0)
        let showsDir = showURL.deletingLastPathComponent()
        let roster = try Fixtures.writeRoster([RosterEntry(id: "duck-01", host: "127.0.0.1", port: port, role: "lead")], in: dir)
        let outputURL = dir.appendingPathComponent("never.duckshow.json")
        let master = SwarmMaster(masterPort: 0)
        let recorder = Recorder(
            master: master, input: ScriptedInput(frames: [InputFrame(t: 0, ly: 1)]),
            configuration: RecorderConfiguration(
                rosterURL: roster, duck: duck01, role: "lead", outputURL: outputURL,
                showURL: showURL, showsDirectory: showsDir, leadSeconds: 5.0
            )
        )
        let run = Task { try await recorder.run() }
        await duck.waitForCommands(named: "play", count: 1, timeoutMs: 3000)
        XCTAssertEqual(try tempShows(in: showsDir).count, 1, "the temp show exists during the countdown")
        await recorder.cancel()
        let result = try await run.value
        XCTAssertTrue(result.interrupted)
        XCTAssertFalse(result.written)
        XCTAssertFalse(FileManager.default.fileExists(atPath: outputURL.path), "nothing captured → output untouched")
        XCTAssertTrue(try tempShows(in: showsDir).isEmpty, "temp show removed on interrupt")
        let panics = await duck.waitForCommands(named: "panic", count: 1)
        XCTAssertEqual(panics.count, 1, "an interrupted take panics the flock")
        let puppets = await duck.receivedPuppets
        XCTAssertTrue(puppets.isEmpty, "cancelled before the epoch: nothing streamed")
        let transport = await master.currentTransport
        XCTAssertEqual(transport, .stopped)
        await duck.stop()
    }

    func testCancelMidTakeStillWritesWhatWasCaptured() async throws {
        let duck = TestDuck()
        let port = try await duck.start()
        let dir = try XCTUnwrap(tmpDir)
        let roster = try Fixtures.writeRoster([RosterEntry(id: "duck-01", host: "127.0.0.1", port: port, role: "lead")], in: dir)
        let outputURL = dir.appendingPathComponent("partial.duckshow.json")
        let master = SwarmMaster(masterPort: 0)
        let recorder = Recorder(
            master: master, input: ScriptedInput(frames: [InputFrame(t: 0, ly: 1), InputFrame(t: 10, ly: 1)]),
            configuration: RecorderConfiguration(rosterURL: roster, duck: duck01, role: "lead", outputURL: outputURL, leadSeconds: 0.05)
        )
        let run = Task { try await recorder.run() }
        await duck.waitForPuppets(count: 10, timeoutMs: 3000)
        await recorder.cancel()
        let result = try await run.value
        XCTAssertTrue(result.interrupted)
        XCTAssertTrue(result.written, "an interrupted take keeps what it captured")
        XCTAssertGreaterThanOrEqual(result.recordedSeconds, 0.18)
        let panics = await duck.waitForCommands(named: "panic", count: 1)
        XCTAssertEqual(panics.count, 1)
        let show = try Show.load(contentsOf: outputURL)
        XCTAssertEqual(show.tracks["lead"]?.locomotion.first?.vx, 0.25)
        XCTAssertEqual(show.meta.duration, result.recordedSeconds)
        await duck.stop()
    }

    // MARK: layering a role the show does not cast yet

    /// docs/authoring.md §2 / README: take 1 creates a one-role cast; take
    /// 2 layers the next role with `--show` pointing at that file. The role
    /// joins the temp show's cast (as does any other roster role the show
    /// does not cast yet, standing idle) so the whole roster loads it, and
    /// `merge` adds only the recorded role to the output's cast.
    func testLayeringARoleNotYetInTheCastAddsItAndLetsUncastRosterDucksStandIdle() async throws {
        let lead = TestDuck(id: "duck-01")
        let wing = TestDuck(id: "duck-02")
        let kick = TestDuck(id: "duck-03")
        let extra = TestDuck(id: "duck-04")
        let leadPort = try await lead.start()
        let wingPort = try await wing.start()
        let kickPort = try await kick.start()
        let extraPort = try await extra.start()
        let dir = try XCTUnwrap(tmpDir)
        let showURL = try writeBaseShow(duration: 0.5)
        let showsDir = showURL.deletingLastPathComponent()
        let roster = try Fixtures.writeRoster([
            RosterEntry(id: "duck-01", host: "127.0.0.1", port: leadPort, role: "lead"),
            RosterEntry(id: "duck-02", host: "127.0.0.1", port: wingPort, role: "wing"),
            RosterEntry(id: "duck-03", host: "127.0.0.1", port: kickPort, role: "kick"),
            RosterEntry(id: "duck-04", host: "127.0.0.1", port: extraPort, role: "extra")
        ], in: dir)
        let master = SwarmMaster(masterPort: 0)
        let recorder = Recorder(
            master: master, input: ScriptedInput(frames: [InputFrame(t: 0, ly: 1), InputFrame(t: 2, ly: 1)]),
            configuration: RecorderConfiguration(
                rosterURL: roster, duck: DuckID("duck-03"), role: "kick", outputURL: showURL,
                showURL: showURL, showsDirectory: showsDir, leadSeconds: 0.2
            )
        )
        let run = Task { try await recorder.run() }

        let loads = await kick.waitForCommands(named: "load", count: 1, timeoutMs: 3000)
        guard case .load(let tempID, _, let role)? = loads.first?.payload else {
            return XCTFail("the new role's duck must load the temp show: \(loads)")
        }
        XCTAssertEqual(role, "kick")
        let temp = try Show.load(contentsOf: showsDir.appendingPathComponent("\(tempID).duckshow.json"))
        XCTAssertEqual(temp.cast.map(\.role), ["lead", "wing", "kick", "extra"], "the recorded role and the uncast roster role join the temp cast")
        XCTAssertEqual(temp.tracks["kick"], RoleTracks())
        XCTAssertEqual(temp.tracks["extra"], RoleTracks(), "an unrecorded, uncast roster role stands idle")
        XCTAssertEqual(temp.tracks["lead"]?.locomotion.first?.vx, 0.1, "the cast roles keep their tracks")
        XCTAssertTrue(temp.validate().isValid, "\(temp.validate().errors)")

        let result = try await run.value
        XCTAssertTrue(result.written)
        XCTAssertFalse(result.interrupted)
        XCTAssertEqual(result.recordedSeconds, 0.5, accuracy: 1e-9)
        let extraLoads = await extra.commands(named: "load")
        XCTAssertEqual(extraLoads.count, 1, "no roster duck NACKs the temp show")
        let plays = await extra.commands(named: "play")
        XCTAssertEqual(plays.count, 1)
        XCTAssertTrue(try tempShows(in: showsDir).isEmpty, "temp show removed")

        let output = try Show.load(contentsOf: showURL)
        XCTAssertEqual(output.cast.map(\.role), ["lead", "wing", "kick"], "only the recorded role is added to the output's cast")
        XCTAssertNil(output.tracks["extra"])
        XCTAssertEqual(output.tracks["kick"]?.locomotion.first?.vx, 0.25)
        XCTAssertEqual(output.tracks["lead"]?.locomotion.first?.vx, 0.1, "other roles untouched")
        XCTAssertEqual(output.tracks["wing"]?.head.first?.headYaw, 0.4)
        XCTAssertEqual(output.meta.duration, 0.5)
        XCTAssertTrue(result.validation.isValid, "\(result.validation.errors)")
        await lead.stop()
        await wing.stop()
        await kick.stop()
        await extra.stop()
    }

    // MARK: an unreadable output is not an absent one

    /// docs/authoring.md §2: a one-role show is created only when `--out`
    /// is absent; other roles are untouched. A file that exists but cannot
    /// be read (or is not a .duckshow document) must never be mistaken for
    /// absent — the atomic write would rename a one-role document over
    /// every other role's tracks. It fails before the take, with nothing
    /// sent to the flock and the file intact.
    func testAnUnreadableOutputFailsBeforeTheTakeAndIsNeverOverwritten() async throws {
        let duck = TestDuck()
        let port = try await duck.start()
        let dir = try XCTUnwrap(tmpDir)
        let roster = try Fixtures.writeRoster([RosterEntry(id: "duck-01", host: "127.0.0.1", port: port, role: "lead")], in: dir)
        let master = SwarmMaster(masterPort: 0)

        // A directory at the output path: exists, cannot be read as a file.
        let directoryOut = dir.appendingPathComponent("taken.duckshow.json", isDirectory: true)
        try FileManager.default.createDirectory(at: directoryOut, withIntermediateDirectories: true)
        let onDirectory = Recorder(
            master: master, input: ScriptedInput(frames: [InputFrame(t: 0, ly: 1), InputFrame(t: 1, ly: 1)]),
            configuration: RecorderConfiguration(rosterURL: roster, duck: duck01, role: "lead", outputURL: directoryOut, leadSeconds: 0)
        )
        do {
            _ = try await onDirectory.run()
            XCTFail("must refuse")
        } catch let error as RecorderError {
            guard case .outputUnreadable(let path, _) = error else { return XCTFail("\(error)") }
            XCTAssertEqual(path, directoryOut.path)
        }
        var isDirectory: ObjCBool = false
        XCTAssertTrue(FileManager.default.fileExists(atPath: directoryOut.path, isDirectory: &isDirectory) && isDirectory.boolValue)

        // A file that is not a .duckshow document (half-synced, garbage).
        let garbageOut = dir.appendingPathComponent("garbage.duckshow.json")
        let garbage = Data("{\"format\": \"duckshow/1\", \"cast\": [{\"role\": \"wing\"".utf8)
        try garbage.write(to: garbageOut)
        let onGarbage = Recorder(
            master: master, input: ScriptedInput(frames: [InputFrame(t: 0, ly: 1), InputFrame(t: 1, ly: 1)]),
            configuration: RecorderConfiguration(rosterURL: roster, duck: duck01, role: "lead", outputURL: garbageOut, leadSeconds: 0)
        )
        do {
            _ = try await onGarbage.run()
            XCTFail("must refuse")
        } catch let error as RecorderError {
            guard case .outputUnreadable = error else { return XCTFail("\(error)") }
        }
        XCTAssertEqual(try Data(contentsOf: garbageOut), garbage, "the file is left exactly as it was")

        let puppets = await duck.receivedPuppets
        XCTAssertTrue(puppets.isEmpty, "refused before the take: nothing streamed")
        await duck.stop()
    }

    // MARK: countdown

    /// The printed count comes from the configured lead: a 1 s lead prints
    /// "1" then GO (a 3 s lead 3-2-1). Re-deriving it from `now` floored
    /// every whole lead one short, so the default 3 s never printed "3".
    func testCountdownPrintsEveryWholeSecondOfTheConfiguredLead() async throws {
        let duck = TestDuck()
        let port = try await duck.start()
        let dir = try XCTUnwrap(tmpDir)
        let roster = try Fixtures.writeRoster([RosterEntry(id: "duck-01", host: "127.0.0.1", port: port, role: "lead")], in: dir)
        let master = SwarmMaster(masterPort: 0)
        let logLines = LogSink()
        let recorder = Recorder(
            master: master, input: ScriptedInput(frames: [InputFrame(t: 0, stopRequested: true)]),
            configuration: RecorderConfiguration(
                rosterURL: roster, duck: duck01, role: "lead", outputURL: dir.appendingPathComponent("none.duckshow.json"),
                leadSeconds: 1.0
            ),
            log: { line in logLines.append(line) }
        )
        let result = try await recorder.run()
        XCTAssertFalse(result.written, "stop at t=0: nothing captured")
        let lines = logLines.lines
        let one = try XCTUnwrap(lines.firstIndex(of: "1"), "the 1 s lead prints '1': \(lines)")
        let go = try XCTUnwrap(lines.firstIndex(of: "GO — recording"), "\(lines)")
        XCTAssertLessThan(one, go)
        XCTAssertFalse(lines.contains("2"), "\(lines)")
        await duck.stop()
    }

    // MARK: argument checks

    func testRecorderRefusesARoleTheRosterDoesNotCastOnTheDuck() async throws {
        let duck = TestDuck()
        let port = try await duck.start()
        let dir = try XCTUnwrap(tmpDir)
        let showURL = try writeBaseShow(duration: 1.0)
        let roster = try Fixtures.writeRoster([RosterEntry(id: "duck-01", host: "127.0.0.1", port: port, role: "wing")], in: dir)
        let master = SwarmMaster(masterPort: 0)
        let recorder = Recorder(
            master: master, input: ScriptedInput(frames: []),
            configuration: RecorderConfiguration(
                rosterURL: roster, duck: duck01, role: "lead", outputURL: dir.appendingPathComponent("x.duckshow.json"),
                showURL: showURL, showsDirectory: showURL.deletingLastPathComponent(), leadSeconds: 0
            )
        )
        do {
            _ = try await recorder.run()
            XCTFail("must refuse")
        } catch {
            XCTAssertEqual(error as? RecorderError, .duckRoleMismatch(duck: duck01, rosterRole: "wing", role: "lead"))
        }
        let ghost = Recorder(
            master: master, input: ScriptedInput(frames: []),
            configuration: RecorderConfiguration(
                rosterURL: roster, duck: "duck-09", role: "lead", outputURL: dir.appendingPathComponent("x.duckshow.json"), leadSeconds: 0
            )
        )
        do {
            _ = try await ghost.run()
            XCTFail("must refuse")
        } catch {
            XCTAssertEqual(error as? RecorderError, .duckNotOnRoster("duck-09"))
        }
        let commands = await duck.received
        XCTAssertTrue(commands.isEmpty, "refused before anything reached the flock")
        XCTAssertTrue(try tempShows(in: showURL.deletingLastPathComponent()).isEmpty)
        await duck.stop()
    }
}

/// Thread-safe collector for the recorder's log lines.
private final class LogSink: @unchecked Sendable {
    private let lock = NSLock()
    private var storage: [String] = []

    func append(_ line: String) {
        lock.lock(); defer { lock.unlock() }
        storage.append(line)
    }

    var lines: [String] {
        lock.lock(); defer { lock.unlock() }
        return storage
    }
}

/// Wraps a `ScriptedInput` but never delivers anything over
/// `frames(from:)` — an artificial stand-in for "infinitely slow machine",
/// used to provoke the capture race `Recorder.foldSchedule` fixes
/// deterministically (see `testScheduledInputIsResolvedFromItsScheduleNeverFromDeliveryTiming`).
/// It still conforms to `ScheduledPuppetInputSource` via `scheduledFrames`,
/// which is the only thing the fixed recorder actually consults for a
/// scripted take.
private struct BrokenDeliveryInput: PuppetInputSource, ScheduledPuppetInputSource {
    let script: ScriptedInput

    var displayName: String { script.displayName }
    var scheduledFrames: [InputFrame] { script.frames }

    func frames(from epochNs: Int64) -> AsyncStream<InputFrame> {
        AsyncStream { continuation in continuation.finish() }
    }
}
