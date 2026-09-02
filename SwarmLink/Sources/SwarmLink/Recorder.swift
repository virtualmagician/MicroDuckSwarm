// Recorder.swift
//
// docs/authoring.md §2 — `swarmctl record`: puppeteer one duck with a
// gamepad (or a scripted input file), stream the intents over the puppet
// channel (docs/swarmlink-protocol.md §6) while the rest of the cast plays
// the show back, and capture the stream as that role's `.duckshow` tracks.
//
// Four pieces, each testable on its own:
//
//  - `PuppetInputSource` — where input frames come from. `ScriptedInput`
//    replays a JSON list of timed frames on the monotonic clock (what tests
//    and CI use; a recording made from a script is reproducible).
//    `GamepadInput` reads the first connected controller through
//    GameController.framework (macOS only, zero deps).
//  - `ControllerMap` — the documented default map from an `InputFrame` to
//    the `PuppetFrame` to send this tick: dead zone, scaling to the
//    validation limits, button edges → `do`/`sound` events.
//  - `TrackCapture` — samples the emitted puppet frames at 50 Hz against
//    show time and decimates them into keyframes (epsilon per quantity, at
//    least one keyframe per 100 ms, `interp: "linear"`), with button
//    presses as events (0.25 s spacing rule → dropped with a warning).
//  - `Recorder` — the orchestrator: temp show with the target role emptied,
//    load to the roster, countdown, play, stream from the play epoch,
//    capture, merge into the output file, validate, clean up.
//
// Show-night invariants respected here: puppet frames are unacknowledged
// and never retried (the agent's 250 ms deadman covers a gap, and the
// stream ends with a neutral frame so nobody keeps the last velocity);
// panic is what an interrupt sends; the recorder never asks a duck to
// catch up — its ticks are nominal show times, and a tick the process
// could not make in time (more than two ticks late) is skipped, never
// squeezed in late as a burst of back-dated frames.

import Foundation
#if canImport(GameController)
import GameController
#endif

// MARK: - Input frames

/// One sample of the puppeteer's controller: sticks in -1…1 (up/right
/// positive), triggers in 0…1, the set of held buttons by name (see
/// `GamepadButton`), and whether the operator asked to stop recording.
/// `t` is seconds since the recording epoch.
///
/// This is also the element of a scripted input file
/// (docs/authoring.md §2): `[{"t": 0.0, "lx": 0.0, "ly": 0.5, "rx": 0,
/// "ry": 0, "lt": 0, "rt": 0, "buttons": ["a"]}, …]`. Every key but `t`
/// is optional; `"stop": true` is the scripted form of `stopRequested`.
public struct InputFrame: Sendable, Equatable {
    public var t: Double
    public var lx: Double
    public var ly: Double
    public var rx: Double
    public var ry: Double
    public var lt: Double
    public var rt: Double
    public var buttons: Set<String>
    public var stopRequested: Bool

    public init(
        t: Double, lx: Double = 0, ly: Double = 0, rx: Double = 0, ry: Double = 0,
        lt: Double = 0, rt: Double = 0, buttons: Set<String> = [], stopRequested: Bool = false
    ) {
        self.t = t
        self.lx = lx; self.ly = ly; self.rx = rx; self.ry = ry
        self.lt = lt; self.rt = rt
        self.buttons = buttons
        self.stopRequested = stopRequested
    }
}

extension InputFrame: Codable {
    private enum CodingKeys: String, CodingKey { case t, lx, ly, rx, ry, lt, rt, buttons, stop }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        t = try c.decode(Double.self, forKey: .t)
        lx = try c.decodeIfPresent(Double.self, forKey: .lx) ?? 0
        ly = try c.decodeIfPresent(Double.self, forKey: .ly) ?? 0
        rx = try c.decodeIfPresent(Double.self, forKey: .rx) ?? 0
        ry = try c.decodeIfPresent(Double.self, forKey: .ry) ?? 0
        lt = try c.decodeIfPresent(Double.self, forKey: .lt) ?? 0
        rt = try c.decodeIfPresent(Double.self, forKey: .rt) ?? 0
        buttons = Set(try c.decodeIfPresent([String].self, forKey: .buttons) ?? [])
        stopRequested = try c.decodeIfPresent(Bool.self, forKey: .stop) ?? false
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(t, forKey: .t)
        try c.encode(lx, forKey: .lx)
        try c.encode(ly, forKey: .ly)
        try c.encode(rx, forKey: .rx)
        try c.encode(ry, forKey: .ry)
        try c.encode(lt, forKey: .lt)
        try c.encode(rt, forKey: .rt)
        try c.encode(buttons.sorted(), forKey: .buttons)
        if stopRequested { try c.encode(true, forKey: .stop) }
    }
}

/// Button names as they appear in `InputFrame.buttons` and in scripted
/// input files (extended-gamepad layout, GameController.framework naming).
public enum GamepadButton: String, CaseIterable, Sendable {
    case a, b, x, y
    case leftShoulder = "left_shoulder"
    case rightShoulder = "right_shoulder"
    case dpadUp = "dpad_up"
    case dpadDown = "dpad_down"
    case dpadLeft = "dpad_left"
    case dpadRight = "dpad_right"
    case menu
    case options
}

// MARK: - Input sources

/// A stream of controller frames for one recording. `frames(from:)` is
/// called once, at the recording epoch (master-monotonic ns); each frame's
/// `t` is seconds since that epoch. When the stream finishes, the
/// recording stops (a script that ran out, a controller that disconnected).
public protocol PuppetInputSource: Sendable {
    /// One line for the operator log ("script: take3.json", "gamepad: …").
    var displayName: String { get }
    func frames(from epochNs: Int64) -> AsyncStream<InputFrame>
}

public enum ScriptedInputError: Error, Sendable, Equatable, CustomStringConvertible {
    case notAnArray
    case badTime(index: Int, t: Double)

    public var description: String {
        switch self {
        case .notAnArray: return "scripted input must be a JSON array of frames"
        case .badTime(let index, let t): return "scripted input frame \(index) has an invalid t (\(t)): must be finite and ≥ 0"
        }
    }
}

/// Replays a JSON list of timed frames on the monotonic clock: each frame
/// is yielded when `epoch + t` comes due (immediately if already past),
/// in ascending `t`, and the stream finishes after the last one.
///
/// Frames are delivered `deliveryLead` seconds *ahead* of their nominal
/// time (10 ms by default). The recorder samples a few ms after each
/// nominal 20 ms tick, so a frame scripted for `t` is in hand for the
/// tick at `t` — and cannot reach the tick before it, which fired 20 ms
/// earlier. That is what makes a scripted take land the same way on a
/// loaded CI runner as on an idle Mac.
public struct ScriptedInput: PuppetInputSource, Sendable, Equatable {
    public let frames: [InputFrame]
    public let name: String
    public let deliveryLead: Double

    public var displayName: String { "script: \(name)" }

    /// Frames are sorted by `t` (stable) so a hand-written script may list
    /// them in any order.
    public init(frames: [InputFrame], name: String = "inline", deliveryLead: Double = 0.010) {
        self.frames = frames.enumerated().sorted { a, b in
            a.element.t == b.element.t ? a.offset < b.offset : a.element.t < b.element.t
        }.map(\.element)
        self.name = name
        self.deliveryLead = max(0, deliveryLead)
    }

    public init(contentsOf url: URL) throws {
        let data = try Data(contentsOf: url)
        self.init(frames: try Self.decode(data), name: url.lastPathComponent)
    }

    /// Decodes the documented script format, rejecting frames whose `t`
    /// is not a finite number ≥ 0. Unknown keys are ignored.
    public static func decode(_ data: Data) throws -> [InputFrame] {
        guard (try? JSONSerialization.jsonObject(with: data)) as? [Any] != nil else {
            throw ScriptedInputError.notAnArray
        }
        let frames = try JSONDecoder().decode([InputFrame].self, from: data)
        for (index, frame) in frames.enumerated() where !frame.t.isFinite || frame.t < 0 {
            throw ScriptedInputError.badTime(index: index, t: frame.t)
        }
        return frames
    }

    public func frames(from epochNs: Int64) -> AsyncStream<InputFrame> {
        let frames = self.frames
        let leadNs = Int64(deliveryLead * 1_000_000_000)
        return AsyncStream { continuation in
            let task = Task {
                for frame in frames {
                    let dueNs = epochNs + Int64(frame.t * 1_000_000_000) - leadNs
                    do {
                        try await Self.sleep(untilNs: dueNs)
                    } catch {
                        break // cancelled: the consumer went away
                    }
                    if Task.isCancelled { break }
                    continuation.yield(frame)
                }
                continuation.finish()
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }

    /// Sleeps until master-clock `dueNs` in slices of at most 5 ms. One long
    /// `Task.sleep` overshoots by roughly 5 % of its length on macOS (timer
    /// leeway: ~15 ms after 290 ms, ~80 ms after 1.4 s), which would deliver
    /// a frame *after* the tick it is meant for and end a scripted take one
    /// tick late on some runs; a 5 ms slice overshoots by ~2 ms at most,
    /// well inside the 10 ms delivery lead.
    static func sleep(untilNs dueNs: Int64) async throws {
        while true {
            let remaining = dueNs - MasterClock.nowNanoseconds()
            if remaining <= 0 { return }
            try await Task.sleep(nanoseconds: UInt64(min(remaining, 5_000_000)))
        }
    }
}

#if canImport(GameController)
/// The first connected extended gamepad, polled at `pollHz` through
/// GameController.framework. The stream finishes if the controller goes
/// away mid-recording (the recorder then stops safely rather than holding
/// the last frame). macOS only; nothing here is exercised in CI.
public final class GamepadInput: PuppetInputSource, @unchecked Sendable {
    public let pollHz: Double

    public init(pollHz: Double = 100) {
        self.pollHz = max(1, pollHz)
    }

    public var displayName: String {
        "gamepad: \(Self.connectedControllerName() ?? "(none)")"
    }

    /// Vendor name of the first connected controller with an extended
    /// gamepad profile, or nil when none is connected.
    public static func connectedControllerName() -> String? {
        GCController.controllers().first(where: { $0.extendedGamepad != nil })?.vendorName ?? nil
    }

    /// Polls for a controller until one is connected or `timeoutSeconds`
    /// elapse; returns its name, or nil on timeout.
    public static func waitForController(timeoutSeconds: Double) async -> String? {
        let deadline = MasterClock.nowNanoseconds() + Int64(max(0, timeoutSeconds) * 1_000_000_000)
        while true {
            if let name = connectedControllerName() { return name }
            if MasterClock.nowNanoseconds() >= deadline { return nil }
            try? await Task.sleep(nanoseconds: 250_000_000)
        }
    }

    public func frames(from epochNs: Int64) -> AsyncStream<InputFrame> {
        let intervalNs = UInt64(1_000_000_000.0 / pollHz)
        return AsyncStream { continuation in
            let task = Task {
                while !Task.isCancelled {
                    guard let frame = Self.readFrame(epochNs: epochNs) else { break } // controller gone
                    continuation.yield(frame)
                    do {
                        try await Task.sleep(nanoseconds: intervalNs)
                    } catch {
                        break
                    }
                }
                continuation.finish()
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }

    /// One snapshot of the extended-gamepad profile, or nil when no
    /// controller is connected.
    static func readFrame(epochNs: Int64) -> InputFrame? {
        guard let pad = GCController.controllers().first(where: { $0.extendedGamepad != nil })?.extendedGamepad else {
            return nil
        }
        var buttons: Set<String> = []
        if pad.buttonA.isPressed { buttons.insert(GamepadButton.a.rawValue) }
        if pad.buttonB.isPressed { buttons.insert(GamepadButton.b.rawValue) }
        if pad.buttonX.isPressed { buttons.insert(GamepadButton.x.rawValue) }
        if pad.buttonY.isPressed { buttons.insert(GamepadButton.y.rawValue) }
        if pad.leftShoulder.isPressed { buttons.insert(GamepadButton.leftShoulder.rawValue) }
        if pad.rightShoulder.isPressed { buttons.insert(GamepadButton.rightShoulder.rawValue) }
        if pad.dpad.up.isPressed { buttons.insert(GamepadButton.dpadUp.rawValue) }
        if pad.dpad.down.isPressed { buttons.insert(GamepadButton.dpadDown.rawValue) }
        if pad.dpad.left.isPressed { buttons.insert(GamepadButton.dpadLeft.rawValue) }
        if pad.dpad.right.isPressed { buttons.insert(GamepadButton.dpadRight.rawValue) }
        if pad.buttonMenu.isPressed { buttons.insert(GamepadButton.menu.rawValue) }
        if pad.buttonOptions?.isPressed == true { buttons.insert(GamepadButton.options.rawValue) }
        let t = Double(MasterClock.nowNanoseconds() - epochNs) / 1_000_000_000
        return InputFrame(
            t: t,
            lx: Double(pad.leftThumbstick.xAxis.value), ly: Double(pad.leftThumbstick.yAxis.value),
            rx: Double(pad.rightThumbstick.xAxis.value), ry: Double(pad.rightThumbstick.yAxis.value),
            lt: Double(pad.leftTrigger.value), rt: Double(pad.rightTrigger.value),
            buttons: buttons,
            stopRequested: buttons.contains(GamepadButton.options.rawValue)
        )
    }
}
#endif

// MARK: - Controller map

/// The validation limits of docs/duckshow-format.md, as the full-scale
/// values a stick maps to. Kept in sync with `Show.validate()` and
/// `python/duckshow/limits.py`.
public struct PuppetLimits: Sendable, Equatable {
    public var vx: Double = 0.25
    public var vy: Double = 0.20
    public var vyaw: Double = 1.5
    public var headAngle: Double = 1.2
    public var poseZ: Double = 0.05
    public var poseRollPitch: Double = 0.5

    public init() {}
}

/// The documented default map (docs/authoring.md §2, `--map default`):
///
/// | input | intent |
/// |---|---|
/// | left stick Y (up = forward) | `move.vx` |
/// | left stick X | `move.vy` |
/// | right stick X | `move.vyaw` |
/// | right stick Y | `head.head_pitch` |
/// | D-pad left / right | `head.head_yaw` steps of `headYawStep` |
/// | left trigger | `pose.z` crouch (down = negative), `pose.active` while > 0 |
/// | right trigger | `mouth.open` |
/// | A / B / X / Y | sound `chirp` / `greet` / `coo` / `wheee` |
/// | left / right shoulder | skill `kick_left` / `kick_right` |
/// | menu | skill `sit_toggle` |
/// | options | stop recording |
///
/// Sticks are dead-zoned at `deadZone` (values inside it read as 0, the
/// rest is rescaled so full deflection still reaches the limit) and scaled
/// to the validation limits; axis signs are passed straight through —
/// which way the duck actually turns for a given sign is an M1 hardware
/// question, so flip here once measured. Buttons fire on the press edge
/// only (holding a button does not repeat). A value type with a little
/// state (previous buttons, the stepped head yaw): call `apply` for every
/// input frame, in order.
public struct ControllerMap: Sendable, Equatable {
    public static let `default` = ControllerMap()

    public var deadZone: Double = 0.08
    public var limits = PuppetLimits()
    /// Radians per D-pad press.
    public var headYawStep: Double = 0.2
    public var stopButton: String = GamepadButton.options.rawValue

    private var previousButtons: Set<String> = []
    private var headYaw: Double = 0

    /// What one input frame maps to: the continuous intents to assert this
    /// tick, plus the discrete events its button edges fired (in a fixed
    /// order, so the same script always yields the same events).
    public struct Mapped: Sendable, Equatable {
        public var frame: PuppetFrame
        public var skills: [String]
        public var sounds: [String]
        public var stop: Bool
    }

    public init() {}

    /// Forgets button state and the stepped head yaw (a new take).
    public mutating func reset() {
        previousButtons = []
        headYaw = 0
    }

    /// Dead zone with rescaling: |v| ≤ `deadZone` → 0; beyond it the
    /// remaining travel is stretched back to 0…1 so there is no jump at the
    /// edge and full deflection still reaches 1.
    public static func applyDeadZone(_ value: Double, deadZone: Double) -> Double {
        let v = min(1, max(-1, value.isFinite ? value : 0))
        let dz = min(0.99, max(0, deadZone))
        guard abs(v) > dz else { return 0 }
        let scaled = (abs(v) - dz) / (1 - dz)
        return v < 0 ? -scaled : scaled
    }

    public mutating func apply(_ input: InputFrame) -> Mapped {
        let pressed = input.buttons.subtracting(previousButtons)
        previousButtons = input.buttons

        if pressed.contains(GamepadButton.dpadLeft.rawValue) { headYaw += headYawStep }
        if pressed.contains(GamepadButton.dpadRight.rawValue) { headYaw -= headYawStep }
        headYaw = min(limits.headAngle, max(-limits.headAngle, headYaw))

        let lt = min(1, max(0, input.lt.isFinite ? input.lt : 0))
        let rt = min(1, max(0, input.rt.isFinite ? input.rt : 0))

        let frame = PuppetFrame(
            move: PuppetMove(
                vx: Self.applyDeadZone(input.ly, deadZone: deadZone) * limits.vx,
                vy: Self.applyDeadZone(input.lx, deadZone: deadZone) * limits.vy,
                vyaw: Self.applyDeadZone(input.rx, deadZone: deadZone) * limits.vyaw
            ),
            head: PuppetHead(
                neckPitch: 0,
                headPitch: Self.applyDeadZone(input.ry, deadZone: deadZone) * limits.headAngle,
                headYaw: headYaw,
                headRoll: 0
            ),
            pose: PuppetPose(z: -lt * limits.poseZ, roll: 0, pitch: 0, active: lt > 0),
            mouth: PuppetMouth(open: rt)
        )

        var skills: [String] = []
        if pressed.contains(GamepadButton.leftShoulder.rawValue) { skills.append("kick_left") }
        if pressed.contains(GamepadButton.rightShoulder.rawValue) { skills.append("kick_right") }
        if pressed.contains(GamepadButton.menu.rawValue) { skills.append("sit_toggle") }
        var sounds: [String] = []
        if pressed.contains(GamepadButton.a.rawValue) { sounds.append("chirp") }
        if pressed.contains(GamepadButton.b.rawValue) { sounds.append("greet") }
        if pressed.contains(GamepadButton.x.rawValue) { sounds.append("coo") }
        if pressed.contains(GamepadButton.y.rawValue) { sounds.append("wheee") }

        let stop = input.stopRequested || input.buttons.contains(stopButton)
        return Mapped(frame: frame, skills: skills, sounds: sounds, stop: stop)
    }
}

// MARK: - Track capture

/// Turns the puppet frames the recorder emits into one role's tracks.
/// Feed `sample` every emitted frame with its show time (ascending), then
/// `finish()`.
///
/// Decimation (docs/authoring.md §2): a keyframe is written when the
/// track's first sample arrives, when any of its values has moved more
/// than that quantity's epsilon since the last *written* keyframe, when
/// `pose.active` flips, or when `maxKeyframeGap` (100 ms) has elapsed
/// since the last keyframe — so a held stick still yields ≥ one keyframe
/// per 100 ms and playback never drifts far from what was streamed. When a
/// move is detected, the sample *before* it is written first whenever the
/// linear ramp playback would otherwise draw from the last keyframe strays
/// more than the epsilon from what was streamed: a stick slammed from rest
/// then ramps over one 20 ms tick, not over the whole gap since the last
/// keyframe (up to 100 ms early — several times the show's sync budget).
/// A steady ramp lies on that line already and gets no extra keyframe.
/// Every keyframe is `interp: "linear"`. A frame's `do`/`sound` becomes an
/// event unless it lands closer than `minEventGap` (0.25 s, the format's
/// density limit) to the previous event, in which case it is dropped and
/// a warning recorded. Keyframe times are rounded to 1 ms and values to
/// 1e-4 so the file diffs cleanly.
public struct TrackCapture: Sendable, Equatable {
    public var epsilonVelocity: Double = 0.01   // m/s and rad/s
    public var epsilonAngle: Double = 0.01      // rad
    public var epsilonPoseZ: Double = 0.005     // m
    public var epsilonMouth: Double = 0.02
    public var maxKeyframeGap: Double = 0.1
    public var minEventGap: Double = 0.25

    public private(set) var locomotion: [LocomotionKeyframe] = []
    public private(set) var head: [HeadKeyframe] = []
    public private(set) var pose: [PoseKeyframe] = []
    public private(set) var mouth: [MouthKeyframe] = []
    public private(set) var events: [Event] = []
    public private(set) var warnings: [String] = []
    public private(set) var sampleCount = 0
    public private(set) var droppedEvents = 0
    public private(set) var lastSampleTime: Double?

    private var lastMove: (t: Double, value: PuppetMove)?
    private var lastHead: (t: Double, value: PuppetHead)?
    private var lastPose: (t: Double, value: PuppetPose)?
    private var lastMouth: (t: Double, value: PuppetMouth)?
    private var lastEventTime: Double?

    public init() {}

    public static func == (lhs: TrackCapture, rhs: TrackCapture) -> Bool {
        lhs.locomotion == rhs.locomotion && lhs.head == rhs.head && lhs.pose == rhs.pose
            && lhs.mouth == rhs.mouth && lhs.events == rhs.events && lhs.warnings == rhs.warnings
    }

    private static let comparisonSlack = 1e-6

    static func round3(_ v: Double) -> Double { (v * 1000).rounded() / 1000 }
    static func round4(_ v: Double) -> Double { (v * 10000).rounded() / 10000 }

    private func moved(_ a: Double, _ b: Double, epsilon: Double) -> Bool {
        abs(a - b) > epsilon + Self.comparisonSlack
    }

    private func gapElapsed(since lastT: Double, now t: Double) -> Bool {
        t - lastT >= maxKeyframeGap - Self.comparisonSlack
    }

    /// Whether the sample `previous` (at `tPrev`) lies more than `epsilon`
    /// off the straight line playback would draw from the last written
    /// keyframe (`last` at `tLast`) to the one about to be written (`next`
    /// at `tNext`) — i.e. whether leaving it out would smear a step across
    /// the gap.
    private func offLine(
        _ last: Double, at tLast: Double, _ previous: Double, at tPrev: Double, _ next: Double, at tNext: Double,
        epsilon: Double
    ) -> Bool {
        guard tNext > tLast, tPrev > tLast, tPrev < tNext else { return false }
        let expected = last + (next - last) * (tPrev - tLast) / (tNext - tLast)
        return abs(previous - expected) > epsilon + Self.comparisonSlack
    }

    /// Records `frame` as emitted at show time `t`. Tracks the frame does
    /// not carry are left untouched this tick.
    public mutating func sample(_ frame: PuppetFrame, at rawT: Double) {
        let t = Self.round3(rawT)
        sampleCount += 1
        lastSampleTime = t

        if let move = frame.move {
            let write: Bool
            if let last = locomotion.last {
                write = moved(move.vx, last.vx, epsilon: epsilonVelocity)
                    || moved(move.vy, last.vy, epsilon: epsilonVelocity)
                    || moved(move.vyaw, last.vyaw, epsilon: epsilonVelocity)
                    || gapElapsed(since: last.t, now: t)
            } else {
                write = true
            }
            if write, locomotion.last?.t != t {
                if let last = locomotion.last, let previous = lastMove,
                   offLine(last.vx, at: last.t, previous.value.vx, at: previous.t, move.vx, at: t, epsilon: epsilonVelocity)
                    || offLine(last.vy, at: last.t, previous.value.vy, at: previous.t, move.vy, at: t, epsilon: epsilonVelocity)
                    || offLine(last.vyaw, at: last.t, previous.value.vyaw, at: previous.t, move.vyaw, at: t, epsilon: epsilonVelocity) {
                    locomotion.append(Self.keyframe(previous.value, at: previous.t))
                }
                locomotion.append(Self.keyframe(move, at: t))
            }
            lastMove = (t, move)
        }

        if let head = frame.head {
            let write: Bool
            if let last = self.head.last {
                write = moved(head.neckPitch, last.neckPitch, epsilon: epsilonAngle)
                    || moved(head.headPitch, last.headPitch, epsilon: epsilonAngle)
                    || moved(head.headYaw, last.headYaw, epsilon: epsilonAngle)
                    || moved(head.headRoll, last.headRoll, epsilon: epsilonAngle)
                    || gapElapsed(since: last.t, now: t)
            } else {
                write = true
            }
            if write, self.head.last?.t != t {
                if let last = self.head.last, let previous = lastHead,
                   offLine(last.neckPitch, at: last.t, previous.value.neckPitch, at: previous.t, head.neckPitch, at: t, epsilon: epsilonAngle)
                    || offLine(last.headPitch, at: last.t, previous.value.headPitch, at: previous.t, head.headPitch, at: t, epsilon: epsilonAngle)
                    || offLine(last.headYaw, at: last.t, previous.value.headYaw, at: previous.t, head.headYaw, at: t, epsilon: epsilonAngle)
                    || offLine(last.headRoll, at: last.t, previous.value.headRoll, at: previous.t, head.headRoll, at: t, epsilon: epsilonAngle) {
                    self.head.append(Self.keyframe(previous.value, at: previous.t))
                }
                self.head.append(Self.keyframe(head, at: t))
            }
            lastHead = (t, head)
        }

        if let pose = frame.pose {
            let write: Bool
            if let last = self.pose.last {
                write = pose.active != last.active
                    || moved(pose.z, last.z, epsilon: epsilonPoseZ)
                    || moved(pose.roll, last.roll, epsilon: epsilonAngle)
                    || moved(pose.pitch, last.pitch, epsilon: epsilonAngle)
                    || gapElapsed(since: last.t, now: t)
            } else {
                write = true
            }
            if write, self.pose.last?.t != t {
                // `active` steps on its own (booleans always step); only the
                // continuous members can be smeared by the ramp.
                if let last = self.pose.last, let previous = lastPose,
                   offLine(last.z, at: last.t, previous.value.z, at: previous.t, pose.z, at: t, epsilon: epsilonPoseZ)
                    || offLine(last.roll, at: last.t, previous.value.roll, at: previous.t, pose.roll, at: t, epsilon: epsilonAngle)
                    || offLine(last.pitch, at: last.t, previous.value.pitch, at: previous.t, pose.pitch, at: t, epsilon: epsilonAngle) {
                    self.pose.append(Self.keyframe(previous.value, at: previous.t))
                }
                self.pose.append(Self.keyframe(pose, at: t))
            }
            lastPose = (t, pose)
        }

        if let mouth = frame.mouth {
            let write: Bool
            if let last = self.mouth.last {
                write = moved(mouth.open, last.open, epsilon: epsilonMouth) || gapElapsed(since: last.t, now: t)
            } else {
                write = true
            }
            if write, self.mouth.last?.t != t {
                if let last = self.mouth.last, let previous = lastMouth,
                   offLine(last.open, at: last.t, previous.value.open, at: previous.t, mouth.open, at: t, epsilon: epsilonMouth) {
                    self.mouth.append(Self.keyframe(previous.value, at: previous.t))
                }
                self.mouth.append(Self.keyframe(mouth, at: t))
            }
            lastMouth = (t, mouth)
        }

        if let skill = frame.skill {
            record(Event(t: t, action: .skill(skill)), label: "do '\(skill)'")
        }
        if let sound = frame.sound {
            record(Event(t: t, action: .sound(sound, hold: nil)), label: "sound '\(sound)'")
        }
    }

    private mutating func record(_ event: Event, label: String) {
        if let previous = lastEventTime, event.t - previous < minEventGap - Self.comparisonSlack {
            droppedEvents += 1
            warnings.append(
                "dropped \(label) at t=\(event.t): only \(Self.round3(event.t - previous)) s after the previous event at t=\(previous) (min \(minEventGap) s)"
            )
            return
        }
        events.append(event)
        lastEventTime = event.t
    }

    /// The captured tracks. Each curve track is closed with the last
    /// sampled value at the last sample time when that sample was not
    /// itself written, so the hold-after-last matches what was streamed.
    public func finish() -> RoleTracks {
        var tracks = RoleTracks(locomotion: locomotion, head: head, pose: pose, mouth: mouth, events: events)
        if let last = lastMove, tracks.locomotion.last.map({ $0.t < last.t }) ?? false {
            tracks.locomotion.append(Self.keyframe(last.value, at: last.t))
        }
        if let last = lastHead, tracks.head.last.map({ $0.t < last.t }) ?? false {
            tracks.head.append(Self.keyframe(last.value, at: last.t))
        }
        if let last = lastPose, tracks.pose.last.map({ $0.t < last.t }) ?? false {
            tracks.pose.append(Self.keyframe(last.value, at: last.t))
        }
        if let last = lastMouth, tracks.mouth.last.map({ $0.t < last.t }) ?? false {
            tracks.mouth.append(Self.keyframe(last.value, at: last.t))
        }
        return tracks
    }

    private static func keyframe(_ m: PuppetMove, at t: Double) -> LocomotionKeyframe {
        LocomotionKeyframe(t: t, vx: round4(m.vx), vy: round4(m.vy), vyaw: round4(m.vyaw), interp: .linear)
    }

    private static func keyframe(_ h: PuppetHead, at t: Double) -> HeadKeyframe {
        HeadKeyframe(
            t: t, neckPitch: round4(h.neckPitch), headPitch: round4(h.headPitch),
            headYaw: round4(h.headYaw), headRoll: round4(h.headRoll), interp: .linear
        )
    }

    private static func keyframe(_ p: PuppetPose, at t: Double) -> PoseKeyframe {
        PoseKeyframe(t: t, z: round4(p.z), roll: round4(p.roll), pitch: round4(p.pitch), active: p.active, interp: .linear)
    }

    private static func keyframe(_ m: PuppetMouth, at t: Double) -> MouthKeyframe {
        MouthKeyframe(t: t, open: round4(m.open), interp: .linear)
    }
}

// MARK: - Recorder

public typealias RecorderLog = @Sendable (String) -> Void

public struct RecorderConfiguration: Sendable {
    /// Roster the master dials; must contain `duck`.
    public var rosterURL: URL
    /// The duck being puppeteered.
    public var duck: DuckID
    /// The cast role whose tracks are recorded.
    public var role: String
    /// The `.duckshow.json` the role's tracks are merged into (created
    /// with a one-role cast if absent).
    public var outputURL: URL
    /// Layering: the show the rest of the cast plays back meanwhile.
    public var showURL: URL?
    /// Where the temporary copy of `showURL` is written so the agents can
    /// resolve it by id (`swarmctl serve`'s `--shows-dir`). Required with
    /// `showURL`.
    public var showsDirectory: URL?
    /// Beat grid for the output's `meta.music` and for rounding a fresh
    /// recording's duration up to the next beat.
    public var bpm: Double?
    public var beatOffset: Double
    /// Hard cap on the recording length, seconds.
    public var maxDuration: Double?
    /// Countdown before the epoch, seconds (3-2-1 printed for the last 3).
    public var leadSeconds: Double
    public var map: ControllerMap
    /// Puppet stream / capture rate.
    public var tickHz: Double

    public init(
        rosterURL: URL, duck: DuckID, role: String, outputURL: URL,
        showURL: URL? = nil, showsDirectory: URL? = nil,
        bpm: Double? = nil, beatOffset: Double = 0, maxDuration: Double? = nil,
        leadSeconds: Double = 3.0, map: ControllerMap = .default, tickHz: Double = 50
    ) {
        self.rosterURL = rosterURL
        self.duck = duck
        self.role = role
        self.outputURL = outputURL
        self.showURL = showURL
        self.showsDirectory = showsDirectory
        self.bpm = bpm
        self.beatOffset = beatOffset
        self.maxDuration = maxDuration
        self.leadSeconds = leadSeconds
        self.map = map
        self.tickHz = tickHz
    }
}

public struct RecorderResult: Sendable, Equatable {
    public var outputURL: URL
    public var role: String
    public var tracks: RoleTracks
    /// Nominal show time of the last frame sent (the neutral closing frame).
    public var recordedSeconds: Double
    public var framesSent: Int
    /// Capture warnings (dropped events).
    public var warnings: [String]
    /// `Show.validate()` of the written file; empty when nothing was written.
    public var validation: ValidationReport
    /// `cancel()` ended the run (SIGINT).
    public var interrupted: Bool
    /// False when the run ended before any frame was captured (interrupted
    /// during the countdown): nothing is merged, the output is untouched.
    public var written: Bool
}

public enum RecorderError: Error, Sendable, Equatable, CustomStringConvertible {
    case duckNotOnRoster(DuckID)
    case duckRoleMismatch(duck: DuckID, rosterRole: String, role: String)
    case showsDirectoryRequired
    case showsDirectoryMissing(String)
    case loadFailed([DuckID: LoadOutcome.Status])
    case playFailed([DuckID: CommandStatus])
    case duckNotConnected(DuckID)
    /// The output file exists but could not be read or is not a JSON
    /// object: never mistaken for "absent" (docs/authoring.md §2 only lets
    /// a fresh one-role show be created when the file is absent), so other
    /// roles' tracks are never renamed over.
    case outputUnreadable(path: String, reason: String)

    public var description: String {
        switch self {
        case .duckNotOnRoster(let duck):
            return "duck \(duck) is not on the roster"
        case .duckRoleMismatch(let duck, let rosterRole, let role):
            return "duck \(duck) is cast as '\(rosterRole)' in the roster, not '\(role)': the roster decides which duck plays which role while the show runs, so --role must match (or edit the roster)"
        case .showsDirectoryRequired:
            return "--shows-dir is required with --show (the temporary show is written there for the agents to load)"
        case .showsDirectoryMissing(let path):
            return "--shows-dir is not a directory: \(path)"
        case .loadFailed(let outcomes):
            let failed = outcomes.filter { $0.value != .ok }.keys.sorted().map(\.raw).joined(separator: ", ")
            return "not every duck loaded the temporary show (\(failed))"
        case .playFailed(let outcomes):
            let failed = outcomes.filter { $0.value != .ok }.keys.sorted().map(\.raw).joined(separator: ", ")
            return "not every duck ACKed play (\(failed)) — panic sent"
        case .duckNotConnected(let duck):
            return "lost the connection to \(duck) while streaming"
        case .outputUnreadable(let path, let reason):
            return "output \(path) exists but could not be read (\(reason)) — refusing to overwrite it: an unreadable file is not an absent one, and the other roles' tracks in it would be lost"
        }
    }
}

/// One recording session. Create it, `run()` it once; `cancel()` from
/// anywhere (a SIGINT handler) ends the run at the next tick — the flock
/// is panicked, the temporary show removed, and whatever was captured so
/// far is still merged into the output.
public actor Recorder {
    private let master: SwarmMaster
    private let input: any PuppetInputSource
    private let configuration: RecorderConfiguration
    private let log: RecorderLog?

    private var map: ControllerMap
    private var capture = TrackCapture()
    private var latestContinuous: PuppetFrame?
    private var pendingSkills: [String] = []
    private var pendingSounds: [String] = []
    /// Nominal show time at which the input ended the take: the `t` of
    /// the frame that pressed stop, or of the last frame delivered when
    /// the input ran out (a script exhausted, a controller gone). The take
    /// ends at the first tick at or after it and nothing on that frame
    /// plays. Nominal times rather than "when a flag was noticed", so a
    /// scripted take ends on the same tick on every run.
    private var inputEndShowTime: Double?
    private var lastInputShowTime: Double?
    private var cancelled = false
    private var tempShowURL: URL?

    public init(master: SwarmMaster, input: any PuppetInputSource, configuration: RecorderConfiguration, log: RecorderLog? = nil) {
        self.master = master
        self.input = input
        self.configuration = configuration
        self.log = log
        self.map = configuration.map
    }

    /// Ends the run at the next tick (or during the countdown). Idempotent.
    public func cancel() {
        cancelled = true
    }

    /// Path of the temporary show while one exists (test hook).
    public var temporaryShowURL: URL? { tempShowURL }

    // MARK: Run

    public func run() async throws -> RecorderResult {
        let configuration = self.configuration
        let roster = try [RosterEntry].load(contentsOf: configuration.rosterURL)
        guard let entry = roster.first(where: { $0.id == configuration.duck }) else {
            throw RecorderError.duckNotOnRoster(configuration.duck)
        }

        var show: Show?
        if let showURL = configuration.showURL {
            // A role the show does not cast yet is the normal case when
            // layering role by role (take 1 created a one-role cast): it
            // joins the temp show's cast below and `merge` adds it to the
            // output's. The roster, though, must cast it on this duck.
            let loaded = try Show.load(contentsOf: showURL)
            guard entry.role == configuration.role else {
                throw RecorderError.duckRoleMismatch(duck: entry.id, rosterRole: entry.role, role: configuration.role)
            }
            show = loaded
        }

        // Pre-flight the output: an existing file that cannot be read fails
        // here, before the take, rather than after a long one — and is never
        // taken for "absent" (see `readExistingOutput`).
        _ = try Self.readExistingOutput(at: configuration.outputURL)

        defer { removeTempShow() }

        let tickHz = max(1, configuration.tickHz)
        let tickNs = Int64(1_000_000_000.0 / tickHz)
        // Ticks fire this much after their nominal show time, so an input
        // frame due exactly at a tick (a script's) is ingested first and
        // lands in that tick, not the next. Invisible on the wire.
        let tickPhaseNs: Int64 = min(5_000_000, tickNs / 4)
        // How late a tick may still be sent. Scheduler jitter is a tick or
        // two; anything beyond is a stalled process, and its missed ticks
        // are skipped rather than fired as a burst of back-dated frames
        // that would record the resumed input as if held since the stall.
        let lateTickTolerance: Int64 = 2
        var endShowTime = configuration.maxDuration
        if let show {
            endShowTime = min(endShowTime ?? .infinity, show.meta.duration)
        }

        log?("[record] \(configuration.duck) as '\(configuration.role)' → \(configuration.outputURL.path) · input \(input.displayName) · lead \(configuration.leadSeconds) s"
            + (show.map { " · layered on \(configuration.showURL!.lastPathComponent) (\($0.meta.duration) s)" } ?? " · from t=0")
            + (configuration.maxDuration.map { " · max \($0) s" } ?? ""))

        // Temp show: the show with the target role emptied, so the duck
        // being puppeteered stands idle on the timeline and the rest of
        // the cast performs around it.
        if let show, let showURL = configuration.showURL {
            guard let showsDirectory = configuration.showsDirectory else { throw RecorderError.showsDirectoryRequired }
            var isDirectory: ObjCBool = false
            guard FileManager.default.fileExists(atPath: showsDirectory.path, isDirectory: &isDirectory), isDirectory.boolValue else {
                throw RecorderError.showsDirectoryMissing(showsDirectory.path)
            }
            var copy = show
            copy.tracks[configuration.role] = RoleTracks()
            // Layering role by role: the role being recorded, and any other
            // roster role the show does not cast yet, join the temp cast
            // with empty tracks — so the whole roster loads it and a duck
            // whose role is still unrecorded stands idle instead of NACKing
            // (`SwarmMaster.load` refuses a roster role missing from the
            // cast). Only the recorded role is merged into --out.
            var addedRoles: [String] = []
            for role in [configuration.role] + roster.map(\.role)
            where !copy.cast.contains(where: { $0.role == role }) {
                copy.cast.append(CastMember(role: role))
                if copy.tracks[role] == nil { copy.tracks[role] = RoleTracks() }
                addedRoles.append(role)
            }
            let tempID = "rec-" + String(UUID().uuidString.replacingOccurrences(of: "-", with: "").prefix(8)).lowercased()
            let tempURL = showsDirectory.appendingPathComponent("\(tempID).duckshow.json")
            let encoder = JSONEncoder()
            encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
            try encoder.encode(copy).write(to: tempURL, options: .atomic)
            tempShowURL = tempURL
            log?("[record] temp show \(tempID) (\(showURL.lastPathComponent) with '\(configuration.role)' emptied"
                + (addedRoles.isEmpty ? "" : ", cast + \(addedRoles.map { "'\($0)'" }.joined(separator: ", ")) idle")
                + ") → loading to the roster")

            let outcomes = try await master.load(show: tempURL, roster: configuration.rosterURL)
            for duckID in outcomes.keys.sorted() {
                log?("[load] \(duckID): \(Self.describe(outcomes[duckID]!.status))")
            }
            guard !outcomes.isEmpty, outcomes.values.allSatisfy(\.isOK) else {
                throw RecorderError.loadFailed(outcomes.mapValues(\.status))
            }
        } else {
            try await master.connect(roster: configuration.rosterURL)
        }
        if cancelled { return abandoned() }

        // Epoch and countdown. With a show, play is scheduled for the very
        // same instant, so the puppet stream's t=0 is the show's t=0.
        let leadNs = Int64(max(0, configuration.leadSeconds) * 1_000_000_000)
        let epochNs = MasterClock.nowNanoseconds() + leadNs

        // Input consumer, started now so frames already flow during the
        // countdown (a puppeteer holding a pose at GO is sent that pose at
        // t=0, and a script's t=0 frame is in hand before the first tick).
        // Every frame is mapped as it arrives, so no button edge is missed
        // between ticks; the tick loop sends the newest continuous values
        // plus one pending event of each kind.
        let stream = input.frames(from: epochNs)
        let consumer = Task { [weak self] in
            for await frame in stream {
                guard let self else { return }
                await self.ingest(frame)
            }
            await self?.markInputEnded()
        }
        defer { consumer.cancel() }

        if show != nil {
            async let playOutcomes = master.play(atMasterTime: epochNs)
            await countdown(to: epochNs, leadSeconds: configuration.leadSeconds)
            let outcomes = try await playOutcomes
            for duckID in outcomes.keys.sorted() {
                log?("[play] \(duckID): \(Self.describe(outcomes[duckID]!))")
            }
            guard !outcomes.isEmpty, outcomes.values.allSatisfy({ $0 == .ok }) else {
                _ = await master.panic()
                throw RecorderError.playFailed(outcomes)
            }
        } else {
            await countdown(to: epochNs, leadSeconds: configuration.leadSeconds)
        }
        if cancelled {
            if show != nil { _ = await master.panic() }
            return abandoned()
        }

        var framesSent = 0
        var tick = 0
        var lastShowTime = 0.0
        streaming: while true {
            let showTime = Double(tick) / tickHz
            if cancelled || takeEnded(at: showTime, endShowTime: endShowTime) { break streaming }
            let due = epochNs + Int64(tick) * tickNs + tickPhaseNs
            let now = MasterClock.nowNanoseconds()
            if due > now {
                await sleep(until: due)
                if cancelled { break streaming }
            } else if now - due > lateTickTolerance * tickNs {
                // Fell behind by more than `lateTickTolerance` ticks (a
                // stalled process): skip ahead rather than fire a burst of
                // stale frames.
                let missed = Int((now - due) / tickNs)
                log?("[record] \(missed) ticks behind at t=\(TrackCapture.round3(showTime)) — skipping ahead")
                tick += missed
                continue
            }
            // Decide this tick's fate again now: a script delivers the frame
            // for show time t up to 10 ms *before* t and this tick fires 5 ms
            // *after* t, so a stop (or the end of the script) at t that
            // arrived during the sleep ends the take at this tick on every
            // run. Checking only before the sleep raced the delivery: a fast
            // machine sent one more frame and closed the take a tick late,
            // which rounded meta.duration up to the next beat.
            if takeEnded(at: showTime, endShowTime: endShowTime) { break streaming }
            let frame = nextFrame()
            do {
                try await master.puppet(duck: configuration.duck, frame: frame)
            } catch {
                throw RecorderError.duckNotConnected(configuration.duck)
            }
            capture.sample(frame, at: showTime)
            framesSent += 1
            lastShowTime = showTime
            tick += 1
        }

        // Close the stream with a neutral frame so the duck does not hold
        // the last velocity for the deadman's 250 ms, and capture it so the
        // track ends at rest too.
        if capture.sampleCount > 0 {
            let closingShowTime = Double(tick) / tickHz
            let neutral = Self.neutralFrame(after: latestContinuous)
            if (try? await master.puppet(duck: configuration.duck, frame: neutral)) != nil {
                framesSent += 1
            }
            capture.sample(neutral, at: closingShowTime)
            lastShowTime = closingShowTime
        }
        consumer.cancel()

        let interrupted = cancelled
        if interrupted {
            log?("[record] interrupted — sending panic")
            _ = await master.panic()
        } else if show != nil {
            let transport = await master.currentTransport
            if transport != .stopped {
                _ = await master.stop()
            }
        }
        removeTempShow()

        guard capture.sampleCount > 0 else { return abandoned() }
        let tracks = capture.finish()
        log?("[record] captured \(TrackCapture.round3(lastShowTime)) s: \(tracks.locomotion.count) locomotion, \(tracks.head.count) head, \(tracks.pose.count) pose, \(tracks.mouth.count) mouth keyframes, \(tracks.events.count) events"
            + (capture.droppedEvents > 0 ? " (\(capture.droppedEvents) dropped)" : ""))
        for warning in capture.warnings { log?("[record] warning: \(warning)") }

        let existing = try Self.readExistingOutput(at: configuration.outputURL)
        let merged = try Self.merge(
            existing: existing, role: configuration.role, tracks: tracks, recordedSeconds: lastShowTime,
            layeredOn: show, bpm: configuration.bpm, beatOffset: configuration.beatOffset,
            outputName: Self.showName(of: configuration.outputURL)
        )
        try FileManager.default.createDirectory(
            at: configuration.outputURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        try merged.write(to: configuration.outputURL, options: .atomic)

        let report = try Show.decode(merged).validate()
        for issue in report.errors { log?("[validate] error: \(issue)") }
        for issue in report.warnings { log?("[validate] warning: \(issue)") }
        log?("[record] wrote \(configuration.outputURL.path)"
            + (report.isValid ? " (valid)" : " — WITH \(report.errors.count) VALIDATION ERROR(S): open it in the editor before it goes near a duck"))

        return RecorderResult(
            outputURL: configuration.outputURL, role: configuration.role, tracks: tracks,
            recordedSeconds: lastShowTime, framesSent: framesSent, warnings: capture.warnings,
            validation: report, interrupted: interrupted, written: true
        )
    }

    // MARK: Input → tick

    private func ingest(_ frame: InputFrame) {
        let mapped = map.apply(frame)
        latestContinuous = mapped.frame
        pendingSkills.append(contentsOf: mapped.skills)
        pendingSounds.append(contentsOf: mapped.sounds)
        lastInputShowTime = max(lastInputShowTime ?? 0, frame.t)
        if mapped.stop { endTake(at: frame.t) }
    }

    /// The input ran out (script exhausted, controller gone): the take ends
    /// at the last frame's `t`. A frame holds until the next one, so a
    /// script's last frame marks the end rather than playing — add a later
    /// frame to hold longer.
    private func markInputEnded() {
        endTake(at: lastInputShowTime ?? 0)
    }

    private func endTake(at showTime: Double) {
        inputEndShowTime = min(inputEndShowTime ?? .infinity, max(0, showTime))
    }

    /// True once `showTime` has reached the earliest end of the take: the
    /// show's `meta.duration` / `--duration`, or the input's stop / last frame.
    private func takeEnded(at showTime: Double, endShowTime: Double?) -> Bool {
        let end = min(endShowTime ?? .infinity, inputEndShowTime ?? .infinity)
        return showTime >= end - 1e-9
    }

    /// The frame for this tick: newest continuous intents (neutral until
    /// the first input frame has arrived) plus at most one skill and one
    /// sound — a packet carries one of each.
    private func nextFrame() -> PuppetFrame {
        var frame = latestContinuous ?? Self.neutralFrame(after: nil)
        if !pendingSkills.isEmpty { frame.skill = pendingSkills.removeFirst() }
        if !pendingSounds.isEmpty { frame.sound = pendingSounds.removeFirst() }
        return frame
    }

    /// Everything at rest; the head keeps its last commanded angles (a
    /// snap to zero is not "rest" for a head that was deliberately turned).
    static func neutralFrame(after last: PuppetFrame?) -> PuppetFrame {
        PuppetFrame(
            move: PuppetMove(), head: last?.head ?? PuppetHead(), pose: PuppetPose(), mouth: PuppetMouth()
        )
    }

    // MARK: Countdown / timing

    /// Prints the last (up to) three whole seconds of the lead, then GO.
    /// The count comes from the configured lead, not from re-deriving it
    /// from `now` — by the time this runs the epoch was fixed a little
    /// while ago, so a 3 s lead read back as 2.999… and floored to 2: "3"
    /// was never printed. The deadlines stay absolute, from the epoch.
    private func countdown(to epochNs: Int64, leadSeconds: Double) async {
        let wholeSeconds = Int((max(0, leadSeconds) + 1e-9).rounded(.down))
        for n in stride(from: min(3, wholeSeconds), through: 1, by: -1) {
            await sleep(until: epochNs - Int64(n) * 1_000_000_000)
            if cancelled { return }
            log?("\(n)")
        }
        await sleep(until: epochNs)
        if !cancelled { log?("GO — recording") }
    }

    /// Sleeps until master-monotonic `deadlineNs` in ≤ 100 ms slices (the
    /// actor is free between slices), returning early once `cancel()` was
    /// called — so Ctrl+C is honored within 100 ms even mid-countdown.
    private func sleep(until deadlineNs: Int64) async {
        while !cancelled {
            let remaining = deadlineNs - MasterClock.nowNanoseconds()
            if remaining <= 0 { return }
            try? await Task.sleep(nanoseconds: UInt64(min(remaining, 100_000_000)))
        }
    }

    private func abandoned() -> RecorderResult {
        RecorderResult(
            outputURL: configuration.outputURL, role: configuration.role, tracks: RoleTracks(),
            recordedSeconds: 0, framesSent: 0, warnings: [], validation: ValidationReport(),
            interrupted: cancelled, written: false
        )
    }

    private func removeTempShow() {
        guard let url = tempShowURL else { return }
        tempShowURL = nil
        try? FileManager.default.removeItem(at: url)
        log?("[record] removed temp show \(url.lastPathComponent)")
    }

    /// The current contents of the output file, or nil only when it does
    /// not exist — the one case docs/authoring.md §2 lets a fresh one-role
    /// show be created. Any other failure — a permissions slip, an I/O
    /// error, a cloud placeholder, a half-synced file, a directory at that
    /// path, contents that are not a JSON object — throws `outputUnreadable`
    /// instead of reading as "absent": treated as absent, `merge` would
    /// build a one-role document and the atomic write (which only needs a
    /// writable directory) would rename it over every other role's tracks,
    /// cast entries and `meta.duration`, silently, logging "(valid)".
    static func readExistingOutput(at url: URL) throws -> Data? {
        guard FileManager.default.fileExists(atPath: url.path) else { return nil }
        let data: Data
        do {
            data = try Data(contentsOf: url)
        } catch {
            throw RecorderError.outputUnreadable(path: url.path, reason: error.localizedDescription)
        }
        guard (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] != nil else {
            throw RecorderError.outputUnreadable(path: url.path, reason: "not a JSON object (a .duckshow document)")
        }
        return data
    }

    private static func describe(_ status: CommandStatus) -> String {
        switch status {
        case .ok: return "ACK"
        case .nacked(let error): return "NACK (\(error))"
        case .timeout: return "TIMEOUT"
        case .connectionFailed(let reason): return "FAILED (\(reason))"
        case .superseded: return "SUPERSEDED"
        }
    }

    /// `mine.duckshow.json` → `mine`.
    static func showName(of url: URL) -> String {
        url.deletingPathExtension().deletingPathExtension().lastPathComponent
    }

    // MARK: Merge (pure)

    /// Rounds `seconds` up to the next beat of the grid (`beat_offset +
    /// k × 60/bpm`, `k` any integer — the editor draws the beats before
    /// the downbeat too); never below `seconds`, never ≤ 0. The floor is
    /// the first grid point past 0, not the beat length: with a
    /// `beat_offset` inside the first beat the beat length itself is not
    /// on the grid, and a take shorter than the offset would land the
    /// show's end mid-beat.
    public static func roundUpToBeat(_ seconds: Double, bpm: Double, beatOffset: Double) -> Double {
        guard bpm > 0, bpm.isFinite, beatOffset.isFinite else { return seconds }
        let beat = 60.0 / bpm
        var beats = ((seconds - beatOffset) / beat - 1e-9).rounded(.up)
        var rounded = beatOffset + beats * beat
        while rounded <= 1e-9 {
            beats += 1
            rounded = beatOffset + beats * beat
        }
        return max(rounded, seconds)
    }

    /// Merges recorded tracks into a `.duckshow` document at the JSON level,
    /// so every field the model does not know (a top-level `editor` block,
    /// vendor fields on other roles) survives untouched — "other roles
    /// untouched" means byte-for-byte, not "re-encoded from a lossy model".
    ///
    /// Rules (docs/authoring.md §2): the role's tracks are replaced; the
    /// role is added to `cast` if missing; a missing file is created with a
    /// one-role cast. `meta.duration`: when layered on a show it is the
    /// longer of what the file has and the show's — the timeline the take
    /// was recorded against, so no keyframe of the fresh take lies past
    /// the end where playback would never reach it (the show's when
    /// creating); without a show it becomes the recorded length — rounded
    /// up to the next beat when `bpm` is given. It never shrinks an
    /// existing duration under other roles' tracks. `bpm`/`beatOffset` set
    /// `meta.music` when given.
    ///
    /// The document is emitted through `JSONEncoder` (via `JSONValue`):
    /// `JSONSerialization` prints 0.1 as 0.10000000000000001, which would
    /// defeat the capture's 1 ms / 1e-4 rounding and noise every number of
    /// the other roles on each re-save.
    public static func merge(
        existing: Data?, role: String, tracks: RoleTracks, recordedSeconds: Double,
        layeredOn show: Show?, bpm: Double?, beatOffset: Double, outputName: String
    ) throws -> Data {
        var root: [String: Any]
        if let existing {
            guard let object = try JSONSerialization.jsonObject(with: existing) as? [String: Any] else {
                throw DuckShowError.missingFormat
            }
            root = object
        } else {
            root = [
                "format": "duckshow/\(SwarmLinkInfo.duckShowFormatMajor)",
                "requires": ["policies": [Any]()],
                "cast": [Any](),
                "tracks": [String: Any]()
            ]
            if let show {
                root["meta"] = try jsonObject(show.meta)
                root["requires"] = try jsonObject(show.requires)
            } else {
                root["meta"] = ["name": outputName, "created": today()] as [String: Any]
            }
        }

        var meta = root["meta"] as? [String: Any] ?? [:]
        let existingDuration = (meta["duration"] as? NSNumber)?.doubleValue
        if let show {
            meta["duration"] = max(existingDuration ?? 0, show.meta.duration, recordedSeconds, 0.001)
        } else {
            var length = recordedSeconds
            if let bpm { length = roundUpToBeat(length, bpm: bpm, beatOffset: beatOffset) }
            meta["duration"] = max(existingDuration ?? 0, length, 0.001)
        }
        if let bpm {
            var music = meta["music"] as? [String: Any] ?? [:]
            music["bpm"] = bpm
            music["beat_offset"] = beatOffset
            meta["music"] = music
        }
        root["meta"] = meta

        var cast = root["cast"] as? [Any] ?? []
        let castRoles = cast.compactMap { ($0 as? [String: Any])?["role"] as? String }
        if !castRoles.contains(role) {
            cast.append(["role": role])
        }
        root["cast"] = cast

        var allTracks = root["tracks"] as? [String: Any] ?? [:]
        var roleObject = try jsonObject(tracks)
        for (key, value) in roleObject where (value as? [Any])?.isEmpty == true {
            roleObject.removeValue(forKey: key)
        }
        allTracks[role] = roleObject
        root["tracks"] = allTracks

        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
        return try encoder.encode(JSONValue(root))
    }

    private static func jsonObject<T: Encodable>(_ value: T) throws -> [String: Any] {
        let data = try JSONEncoder().encode(value)
        return try JSONSerialization.jsonObject(with: data) as? [String: Any] ?? [:]
    }

    private static func today() -> String {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone.current
        formatter.dateFormat = "yyyy-MM-dd"
        return formatter.string(from: Date())
    }
}

// MARK: - JSON value tree

/// A JSON document as a value tree, built from `JSONSerialization` objects
/// (so unknown fields survive) and encoded with `JSONEncoder`, which prints
/// doubles in their shortest round-trip form (0.1, not 0.10000000000000001
/// as `JSONSerialization` does) — what keeps a merged `.duckshow` diffable.
enum JSONValue: Encodable, Equatable {
    case object([String: JSONValue])
    case array([JSONValue])
    case string(String)
    case integer(Int64)
    case number(Double)
    case bool(Bool)
    case null

    struct UnsupportedValue: Error, CustomStringConvertible {
        var description: String
    }

    private static let integerObjCTypes: Set<String> = ["c", "i", "s", "l", "q", "C", "I", "S", "L", "Q"]

    init(_ value: Any) throws {
        switch value {
        case let dictionary as [String: Any]:
            self = .object(try dictionary.mapValues { try JSONValue($0) })
        case let array as [Any]:
            self = .array(try array.map { try JSONValue($0) })
        case let string as String:
            self = .string(string)
        case is NSNull:
            self = .null
        case let number as NSNumber:
            if CFGetTypeID(number) == CFBooleanGetTypeID() {
                self = .bool(number.boolValue)
            } else if Self.integerObjCTypes.contains(String(cString: number.objCType)) {
                self = .integer(number.int64Value)
            } else {
                self = .number(number.doubleValue)
            }
        default:
            throw UnsupportedValue(description: "unsupported JSON value of type \(type(of: value))")
        }
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .object(let object): try container.encode(object)
        case .array(let array): try container.encode(array)
        case .string(let string): try container.encode(string)
        case .integer(let integer): try container.encode(integer)
        case .number(let number): try container.encode(number)
        case .bool(let bool): try container.encode(bool)
        case .null: try container.encodeNil()
        }
    }
}
