// DuckShow.swift
//
// Codable model for the `.duckshow` file format.
// Mirrors docs/duckshow-format.md exactly. Unknown JSON fields are ignored
// everywhere (default JSONDecoder behavior against explicit CodingKeys),
// matching "forward compatibility, same discipline as StageWizard show files".
//
// Optionality mirrors the canonical loader in `python/duckshow/loader.py`
// (docs/architecture.md names it the shared parse/validate implementation):
// the only required fields are `format`, `meta.duration`, `cast[].role`,
// every keyframe's `t`, and `requires.policies[].name/file/sha256`.
// Everything else defaults exactly as the Python model does (scalars 0.0,
// `pose.active` false, `interp` linear, `servo.mode` "hold", strings nil),
// so a file that loads on every duck also loads on the master.

import Foundation
#if canImport(CryptoKit)
import CryptoKit
#endif

// MARK: - Errors

public enum DuckShowError: Error, Sendable, Equatable, CustomStringConvertible {
    /// `format` was present but not a major version this package understands.
    case unsupportedFormat(String)
    /// The top-level `format` field is missing or not a string.
    case missingFormat

    public var description: String {
        switch self {
        case .unsupportedFormat(let format):
            return "unsupported .duckshow format '\(format)' (expected duckshow/\(SwarmLinkInfo.duckShowFormatMajor))"
        case .missingFormat:
            return "missing or non-string top-level 'format' field (expected duckshow/\(SwarmLinkInfo.duckShowFormatMajor))"
        }
    }
}

// MARK: - Interpolation

/// Interpolation from a keyframe to the next one. See "Curve tracks" in
/// docs/duckshow-format.md. Default when omitted is `.linear`.
public enum Interp: String, Codable, Sendable, Equatable {
    case step
    case linear
    case smooth
}

// MARK: - Top level

public struct Show: Codable, Sendable, Equatable {
    public var format: String
    public var meta: Meta
    public var requires: Requirements
    public var cast: [CastMember]
    /// Keyed by role name. Every role in `cast` must have an entry
    /// (possibly empty, meaning the duck stands idle).
    public var tracks: [String: RoleTracks]

    private enum CodingKeys: String, CodingKey {
        case format, meta, requires, cast, tracks
    }

    public init(
        format: String,
        meta: Meta,
        requires: Requirements = Requirements(),
        cast: [CastMember],
        tracks: [String: RoleTracks]
    ) {
        self.format = format
        self.meta = meta
        self.requires = requires
        self.cast = cast
        self.tracks = tracks
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        format = try c.decode(String.self, forKey: .format)
        meta = try c.decode(Meta.self, forKey: .meta)
        requires = try c.decodeIfPresent(Requirements.self, forKey: .requires) ?? Requirements()
        cast = try c.decode([CastMember].self, forKey: .cast)
        tracks = try c.decodeIfPresent([String: RoleTracks].self, forKey: .tracks) ?? [:]
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(format, forKey: .format)
        try c.encode(meta, forKey: .meta)
        try c.encode(requires, forKey: .requires)
        try c.encode(cast, forKey: .cast)
        try c.encode(tracks, forKey: .tracks)
    }
}

public struct Meta: Codable, Sendable, Equatable {
    /// Optional descriptive fields (the Python model treats them as
    /// `Optional[str]`; the format doc never marks them required).
    public var name: String?
    public var author: String?
    /// Kept as the raw string from the file (e.g. "2026-09-01"). The show
    /// format does not specify a time component, so we avoid a lossy/strict
    /// Date decode here and let callers parse it if they need to.
    public var created: String?
    /// Seconds; "playback ends here regardless of track contents". The one
    /// piece of meta a master/agent cannot run a show without.
    public var duration: Double
    public var music: Music?

    private enum CodingKeys: String, CodingKey { case name, author, created, duration, music }

    public init(name: String? = nil, author: String? = nil, created: String? = nil, duration: Double, music: Music? = nil) {
        self.name = name
        self.author = author
        self.created = created
        self.duration = duration
        self.music = music
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        name = try c.decodeIfPresent(String.self, forKey: .name)
        author = try c.decodeIfPresent(String.self, forKey: .author)
        created = try c.decodeIfPresent(String.self, forKey: .created)
        duration = try c.decode(Double.self, forKey: .duration)
        music = try c.decodeIfPresent(Music.self, forKey: .music)
    }
}

public struct Music: Codable, Sendable, Equatable {
    public var file: String?
    public var bpm: Double?
    /// Seconds to the first downbeat; defaults to 0 when omitted (same as
    /// `python/duckshow/loader.py`).
    public var beatOffset: Double

    private enum CodingKeys: String, CodingKey {
        case file, bpm
        case beatOffset = "beat_offset"
    }

    public init(file: String? = nil, bpm: Double? = nil, beatOffset: Double = 0.0) {
        self.file = file
        self.bpm = bpm
        self.beatOffset = beatOffset
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        file = try c.decodeIfPresent(String.self, forKey: .file)
        bpm = try c.decodeIfPresent(Double.self, forKey: .bpm)
        beatOffset = try c.decodeIfPresent(Double.self, forKey: .beatOffset) ?? 0.0
    }
}

public struct Requirements: Codable, Sendable, Equatable {
    public var policies: [RequiredPolicy]

    public init(policies: [RequiredPolicy] = []) {
        self.policies = policies
    }

    private enum CodingKeys: String, CodingKey { case policies }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        policies = try c.decodeIfPresent([RequiredPolicy].self, forKey: .policies) ?? []
    }
}

/// A custom `.onnx` policy a show requires. See "Custom .onnx policies" in
/// docs/duckshow-format.md. Mirrors Python's `PolicyRequirement`: `name` is
/// a human label only -- for logs and error messages -- and is never sent
/// to robotd. `slot` is what matters: the fixed robotd policy slot
/// (`walk`, `stand`, `sitstand`, `ground_pick`, `kick_left`, `kick_right`,
/// `roulade`, or a roller-family equivalent) this `.onnx` occupies once
/// installed. There is deliberately no per-policy `mode` field: the
/// drive-mode string sent at runtime by a `mode` event is always just
/// `"walk"` or `"roller"` (see `Event.Action.mode` / `Show.driveModes`),
/// completely independent of which policy is behind that slot. A `.duckshow`
/// file written before that was clarified may still carry a `mode` key on a
/// policy entry -- CodingKeys below doesn't list it, so JSONDecoder's
/// default behavior of ignoring unrequested keys drops it silently rather
/// than throwing (same "unknown fields are ignored everywhere" discipline
/// as the rest of this format).
public struct RequiredPolicy: Codable, Sendable, Equatable {
    public var name: String
    public var file: String
    public var sha256: String
    /// Optional, like `PolicyRequirement.slot` in the Python model.
    public var slot: String?

    private enum CodingKeys: String, CodingKey { case name, file, sha256, slot }

    public init(name: String, file: String, sha256: String, slot: String? = nil) {
        self.name = name
        self.file = file
        self.sha256 = sha256
        self.slot = slot
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        name = try c.decode(String.self, forKey: .name)
        file = try c.decode(String.self, forKey: .file)
        sha256 = try c.decode(String.self, forKey: .sha256)
        slot = try c.decodeIfPresent(String.self, forKey: .slot)
    }
}

public struct CastMember: Codable, Sendable, Equatable {
    public var role: String
    public var notes: String?

    public init(role: String, notes: String? = nil) {
        self.role = role
        self.notes = notes
    }
}

// MARK: - Per-role tracks

public struct RoleTracks: Codable, Sendable, Equatable {
    public var locomotion: [LocomotionKeyframe]
    public var head: [HeadKeyframe]
    public var pose: [PoseKeyframe]
    public var mouth: [MouthKeyframe]
    public var events: [Event]
    public var servo: [ServoWindow]

    public init(
        locomotion: [LocomotionKeyframe] = [],
        head: [HeadKeyframe] = [],
        pose: [PoseKeyframe] = [],
        mouth: [MouthKeyframe] = [],
        events: [Event] = [],
        servo: [ServoWindow] = []
    ) {
        self.locomotion = locomotion
        self.head = head
        self.pose = pose
        self.mouth = mouth
        self.events = events
        self.servo = servo
    }

    private enum CodingKeys: String, CodingKey {
        case locomotion, head, pose, mouth, events, servo
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        locomotion = try c.decodeIfPresent([LocomotionKeyframe].self, forKey: .locomotion) ?? []
        head = try c.decodeIfPresent([HeadKeyframe].self, forKey: .head) ?? []
        pose = try c.decodeIfPresent([PoseKeyframe].self, forKey: .pose) ?? []
        mouth = try c.decodeIfPresent([MouthKeyframe].self, forKey: .mouth) ?? []
        events = try c.decodeIfPresent([Event].self, forKey: .events) ?? []
        servo = try c.decodeIfPresent([ServoWindow].self, forKey: .servo) ?? []
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(locomotion, forKey: .locomotion)
        try c.encode(head, forKey: .head)
        try c.encode(pose, forKey: .pose)
        try c.encode(mouth, forKey: .mouth)
        try c.encode(events, forKey: .events)
        try c.encode(servo, forKey: .servo)
    }
}

public struct LocomotionKeyframe: Codable, Sendable, Equatable {
    public var t: Double
    public var vx: Double
    public var vy: Double
    public var vyaw: Double
    public var interp: Interp

    private enum CodingKeys: String, CodingKey { case t, vx, vy, vyaw, interp }

    public init(t: Double, vx: Double, vy: Double, vyaw: Double, interp: Interp = .linear) {
        self.t = t; self.vx = vx; self.vy = vy; self.vyaw = vyaw; self.interp = interp
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        t = try c.decode(Double.self, forKey: .t)
        vx = try c.decodeIfPresent(Double.self, forKey: .vx) ?? 0.0
        vy = try c.decodeIfPresent(Double.self, forKey: .vy) ?? 0.0
        vyaw = try c.decodeIfPresent(Double.self, forKey: .vyaw) ?? 0.0
        interp = try c.decodeIfPresent(Interp.self, forKey: .interp) ?? .linear
    }
}

public struct HeadKeyframe: Codable, Sendable, Equatable {
    public var t: Double
    public var neckPitch: Double
    public var headPitch: Double
    public var headYaw: Double
    public var headRoll: Double
    public var interp: Interp

    private enum CodingKeys: String, CodingKey {
        case t
        case neckPitch = "neck_pitch"
        case headPitch = "head_pitch"
        case headYaw = "head_yaw"
        case headRoll = "head_roll"
        case interp
    }

    public init(
        t: Double, neckPitch: Double, headPitch: Double, headYaw: Double, headRoll: Double,
        interp: Interp = .linear
    ) {
        self.t = t
        self.neckPitch = neckPitch
        self.headPitch = headPitch
        self.headYaw = headYaw
        self.headRoll = headRoll
        self.interp = interp
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        t = try c.decode(Double.self, forKey: .t)
        neckPitch = try c.decodeIfPresent(Double.self, forKey: .neckPitch) ?? 0.0
        headPitch = try c.decodeIfPresent(Double.self, forKey: .headPitch) ?? 0.0
        headYaw = try c.decodeIfPresent(Double.self, forKey: .headYaw) ?? 0.0
        headRoll = try c.decodeIfPresent(Double.self, forKey: .headRoll) ?? 0.0
        interp = try c.decodeIfPresent(Interp.self, forKey: .interp) ?? .linear
    }
}

public struct PoseKeyframe: Codable, Sendable, Equatable {
    public var t: Double
    public var z: Double
    public var roll: Double
    public var pitch: Double
    /// Booleans always step (see docs/duckshow-format.md, "Curve tracks").
    public var active: Bool
    public var interp: Interp

    private enum CodingKeys: String, CodingKey { case t, z, roll, pitch, active, interp }

    public init(t: Double, z: Double, roll: Double, pitch: Double, active: Bool, interp: Interp = .linear) {
        self.t = t; self.z = z; self.roll = roll; self.pitch = pitch; self.active = active; self.interp = interp
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        t = try c.decode(Double.self, forKey: .t)
        z = try c.decodeIfPresent(Double.self, forKey: .z) ?? 0.0
        roll = try c.decodeIfPresent(Double.self, forKey: .roll) ?? 0.0
        pitch = try c.decodeIfPresent(Double.self, forKey: .pitch) ?? 0.0
        active = try c.decodeIfPresent(Bool.self, forKey: .active) ?? false
        interp = try c.decodeIfPresent(Interp.self, forKey: .interp) ?? .linear
    }
}

public struct MouthKeyframe: Codable, Sendable, Equatable {
    public var t: Double
    public var open: Double
    public var interp: Interp

    private enum CodingKeys: String, CodingKey { case t, open, interp }

    public init(t: Double, open: Double, interp: Interp = .linear) {
        self.t = t; self.open = open; self.interp = interp
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        t = try c.decode(Double.self, forKey: .t)
        open = try c.decodeIfPresent(Double.self, forKey: .open) ?? 0.0
        interp = try c.decodeIfPresent(Interp.self, forKey: .interp) ?? .linear
    }
}

// MARK: - Events

/// One point event. Exactly one of `do` / `sound` / `mode` is set per entry,
/// per docs/duckshow-format.md "Event track".
public struct Event: Codable, Sendable, Equatable {
    public var t: Double
    public var action: Action
    /// Action keys that were present in the file *in addition to* the one
    /// decoded into `action` (precedence do > sound > mode, same as the
    /// Python model's `action_kind()`). The format allows exactly one, so
    /// `Show.validate()` reports an error when this is non-empty — the
    /// loader keeps the file loadable, mirroring `python/duckshow`, where
    /// the loader preserves what was present and the validator flags it.
    /// Never re-encoded.
    public var extraActionKeys: [String]

    /// The `"mode"` value as it appeared in the file, kept even when another
    /// action key won the do > sound > mode precedence.
    ///
    /// `extraActionKeys` records only the NAMES of the losing keys, so an
    /// event carrying both `sound` and `mode` used to discard the mode value
    /// entirely: `modeAt`, `validateModeValue` and `validateModeOverlap` all
    /// read it back out of `action` and therefore could not see it. Python
    /// and the editor both store all three fields unconditionally, so Swift
    /// was one against two. Measured: `{"sound":"chirp","mode":"bogus_mode"}`
    /// produced two errors in Python and one here.
    public var declaredMode: String?

    /// The drive mode this event declares, however it was spelled: the
    /// winning `.mode` action, or a `mode` key that lost the precedence.
    public var modeName: String? {
        if let declaredMode { return declaredMode }
        if case .mode(let name) = action { return name }
        return nil
    }

    public enum Action: Sendable, Equatable {
        /// `"do": "<skill>"` → `robot.do`.
        case skill(String)
        /// `"sound": "<tag>"`, optional `"hold": <seconds>` → `robot.sound` (+ release after hold).
        case sound(String, hold: Double?)
        /// `"mode": "<policy-mode>"` → `robot.setMode`.
        case mode(String)
    }

    public init(t: Double, action: Action, extraActionKeys: [String] = [], declaredMode: String? = nil) {
        self.t = t
        self.action = action
        self.extraActionKeys = extraActionKeys
        self.declaredMode = declaredMode
    }

    private enum CodingKeys: String, CodingKey {
        case t
        case doAction = "do"
        case sound
        case mode
        case hold
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        t = try c.decode(Double.self, forKey: .t)
        let skill = try c.decodeIfPresent(String.self, forKey: .doAction)
        let sound = try c.decodeIfPresent(String.self, forKey: .sound)
        let mode = try c.decodeIfPresent(String.self, forKey: .mode)
        var present: [String] = []
        if skill != nil { present.append("do") }
        if sound != nil { present.append("sound") }
        if mode != nil { present.append("mode") }
        if let skill {
            action = .skill(skill)
        } else if let sound {
            let hold = try c.decodeIfPresent(Double.self, forKey: .hold)
            action = .sound(sound, hold: hold)
        } else if let mode {
            action = .mode(mode)
        } else {
            throw DecodingError.dataCorruptedError(
                forKey: .t, in: c,
                debugDescription: "event at t=\(t) has none of do/sound/mode"
            )
        }
        extraActionKeys = Array(present.dropFirst())
        declaredMode = mode
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(t, forKey: .t)
        switch action {
        case .skill(let name):
            try c.encode(name, forKey: .doAction)
        case .sound(let tag, let hold):
            try c.encode(tag, forKey: .sound)
            try c.encodeIfPresent(hold, forKey: .hold)
        case .mode(let name):
            try c.encode(name, forKey: .mode)
        }
    }
}

/// Reserved-in-v1 servo window. See docs/duckshow-format.md "Servo track".
public struct ServoWindow: Codable, Sendable, Equatable {
    public var t: Double
    /// Defaults to `"hold"` when omitted (the only v1 mode; same default as
    /// the Python `ServoEvent`).
    public var mode: String
    public var duration: Double?
    /// Used by future modes (`color_homing`, etc.); v1 agents only honor `hold`.
    public var target: String?

    private enum CodingKeys: String, CodingKey { case t, mode, duration, target }

    public init(t: Double, mode: String = "hold", duration: Double? = nil, target: String? = nil) {
        self.t = t
        self.mode = mode
        self.duration = duration
        self.target = target
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        t = try c.decode(Double.self, forKey: .t)
        mode = try c.decodeIfPresent(String.self, forKey: .mode) ?? "hold"
        duration = try c.decodeIfPresent(Double.self, forKey: .duration)
        target = try c.decodeIfPresent(String.self, forKey: .target)
    }
}

// MARK: - Loading

public extension Show {
    /// Decodes a `.duckshow.json` file and rejects unknown major format
    /// versions ("Parsers reject unknown major versions" in the doc).
    static func load(contentsOf url: URL) throws -> Show {
        let data = try Data(contentsOf: url)
        return try decode(data)
    }

    /// Decodes an in-memory `.duckshow` document with the same format gate
    /// as `load(contentsOf:)`. The `format` field is checked *before* the
    /// full decode (as `python/duckshow/loader.py` does), so a future-major
    /// file whose schema no longer matches v1 is reported as
    /// `DuckShowError.unsupportedFormat` naming the version rather than as
    /// a `DecodingError` about some missing v1 key.
    static func decode(_ data: Data) throws -> Show {
        let probe: FormatProbe
        do {
            probe = try JSONDecoder().decode(FormatProbe.self, from: data)
        } catch is DecodingError {
            throw DuckShowError.missingFormat
        }
        guard let format = probe.format else { throw DuckShowError.missingFormat }
        guard format == "duckshow/\(SwarmLinkInfo.duckShowFormatMajor)" else {
            throw DuckShowError.unsupportedFormat(format)
        }
        return try JSONDecoder().decode(Show.self, from: data)
    }

    private struct FormatProbe: Decodable {
        let format: String?
    }

    /// Hex-encoded SHA-256 of the raw file contents, used for the `load`
    /// command's `sha256` field and for agent-side verification.
    static func sha256(of fileURL: URL) throws -> String {
        let data = try Data(contentsOf: fileURL)
        let digest = SHA256.hash(data: data)
        return digest.map { String(format: "%02x", $0) }.joined()
    }
}

// MARK: - Validator summary

/// One validation finding. Mirrors the checks in docs/duckshow-format.md's
/// "Validation limits" table plus the structural rules described in the
/// surrounding prose. This is a summary for preflight/UI purposes — the
/// canonical validator lives in `python/duckshow` (see docs/architecture.md).
public struct ValidationIssue: Sendable, Equatable, CustomStringConvertible {
    public var role: String?
    public var track: String
    public var t: Double?
    public var message: String

    public var description: String {
        var loc = track
        if let role { loc = "\(role).\(loc)" }
        if let t { loc += "@\(t)" }
        return "\(loc): \(message)"
    }
}

public struct ValidationReport: Sendable, Equatable {
    public var errors: [ValidationIssue] = []
    public var warnings: [ValidationIssue] = []

    public var isValid: Bool { errors.isEmpty }
}

public extension Show {
    /// Conservative structural + limits validation, per the
    /// "Validation limits" table in docs/duckshow-format.md. Values are
    /// duplicated here deliberately (not imported from Python) — keep them
    /// in sync with `python/duckshow/limits.py` if either changes.
    func validate() -> ValidationReport {
        var report = ValidationReport()

        // Parity with validator.py:_check_meta_duration. A missing duration
        // cannot reach here (Meta.duration is non-optional, so the decode
        // fails first), but zero, negative and non-finite all can, and the
        // sampler's end-of-show safety -- zero locomotion, robot.stop -- then
        // either never runs or runs on the first tick.
        if !meta.duration.isFinite || meta.duration <= 0 {
            report.errors.append(ValidationIssue(role: nil, track: "meta", t: nil,
                message: "meta.duration=\(meta.duration) must be a finite number > 0"))
        }

        let castRoles = Set(cast.map(\.role))
        // Parity with `validator.py:validate` — a cast role with no tracks
        // entry at all is an ERROR (docs/duckshow-format.md: "every role in
        // `cast` must have a track entry"), not a warning: unlike an empty
        // `{}` entry (which validly means "stands idle"), an entirely
        // missing entry means the file itself is incomplete.
        for role in castRoles where tracks[role] == nil {
            report.errors.append(ValidationIssue(role: role, track: "tracks", t: nil,
                message: "cast role '\(role)' has no tracks entry"))
        }
        for trackRole in tracks.keys where !castRoles.contains(trackRole) {
            report.warnings.append(ValidationIssue(role: trackRole, track: "tracks", t: nil,
                message: "track entry has no matching cast role"))
        }

        // Iterate the CAST, in order, exactly as validator.py does. Walking
        // `tracks` instead validated orphan track entries the canonical
        // validator never looks at (so the skill-occupancy warning fired only
        // here), and a Swift Dictionary has no stable order, so issue order
        // differed run to run.
        for member in cast {
            guard let roleTracks = tracks[member.role] else { continue }
            let role = member.role
            validateLocomotion(roleTracks.locomotion, role: role, into: &report)
            validateHead(roleTracks.head, role: role, into: &report)
            validatePose(roleTracks.pose, role: role, into: &report)
            validateMouth(roleTracks.mouth, role: role, into: &report)
            validateEvents(roleTracks.events, role: role, into: &report)
            validateEventActionNames(roleTracks.events, role: role, into: &report)
            validateModeValue(roleTracks.events, role: role, into: &report)
            validateModeOverlap(roleTracks.events, locomotion: roleTracks.locomotion, role: role, into: &report)
            validateSkillOccupancyOverlap(roleTracks.events, role: role, into: &report)
            validateEventFields(roleTracks.events, role: role, into: &report)
            validateServo(roleTracks.servo, role: role, into: &report)
        }

        return report
    }

    // Closed enums from docs/duckshow-format.md's "Event track" table — these
    // mirror robotd-api.md's Skill / SoundTag enums, so an event referencing
    // anything outside these sets can never succeed on real hardware. Kept
    // in sync with `python/duckshow/limits.py`'s `SKILLS`/`SOUND_TAGS`.
    // Arrays (not Set) so the "expected one of" message text is
    // deterministic and matches Python's tuple order.
    static let skills: [String] = [
        "ground_pick", "kick_left", "kick_right", "sit_toggle", "roulade"
    ]
    static let soundTags: [String] = [
        "alarm", "greet", "inquire", "peck", "chirp", "coo", "wheee"
    ]
    // The only two drive-mode strings real robotd accepts over the wire
    // (docs/robotd-api.md "Custom .onnx policies & modes"). There is no
    // mechanism to register a custom-named mode -- a custom-trained gait
    // is installed by pointing a fixed policy *slot* at a different .onnx
    // file (requires.policies[].slot), never by inventing a new mode
    // string. A `mode` event's value must be one of these two. Kept in
    // sync with `python/duckshow/limits.py`'s `DRIVE_MODES`.
    static let driveModes: [String] = ["walk", "roller"]

    // Per-skill occupancy durations (seconds), sourced from
    // assets/microduck/policies/manifest.json (schema_version 2, control_hz
    // 50; see docs/duckshow-format.md "Skill durations and occupancy" for
    // the full authoring mapping table). Each of these `do` skills is an
    // *episodic* policy clip: once started, it runs to completion, so a
    // discrete event scheduled inside that window is scheduling against a
    // duck that physically cannot have finished the first skill yet (see
    // `validateSkillOccupancyOverlap` below). Kept in sync with
    // `python/duckshow/limits.py`'s `SKILL_DURATIONS_S`.
    //
    // `sit_toggle` (alpha_sitstand.onnx) is deliberately absent: the
    // manifest marks it "kind": "scripted", not "episodic", and gives it a
    // ramp_s/unwind_s posture transition rather than a fixed duration_s --
    // docs/bake-format.md records that the hand-off semantics for a second
    // sit_toggle mid-ramp are unverified. There is no confirmed number to
    // warn against, so sit_toggle never OCCUPIES for the purposes of this
    // check: it opens no window, so nothing scheduled after one is warned
    // about. It can still be the INTERRUPTING event, warning like any other
    // skill landing inside another's window.
    static let skillDurationsS: [String: Double] = [
        "ground_pick": 2.8, // alpha_ground_pick.onnx, walk-mode duration
        "roulade": 1.0, // roulade.onnx
        "kick_left": 0.5, // ball_kick_left.onnx
        "kick_right": 0.5, // ball_kick_right.onnx
    ]

    /// ground_pick's occupancy in roller mode: the robot runs
    /// roller_crouch.onnx instead of alpha_ground_pick.onnx
    /// (docs/duckshow-format.md's authoring mapping table names
    /// roller_crouch as "the roller-mode variant of ground pick", never
    /// itself authored directly) -- a longer clip, not just a renamed one.
    /// Kept in sync with `python/duckshow/limits.py`'s
    /// `GROUND_PICK_ROLLER_DURATION_S`.
    static let groundPickRollerDurationS: Double = 3.5

    /// Skills whose manifest.json entry is "chain": true -- a repeat of one
    /// of these immediately after itself is the documented way to keep the
    /// effect going, not an authoring mistake, so the occupancy-overlap
    /// check below must never warn about that specific pairing. Kept in
    /// sync with `python/duckshow/limits.py`'s `CHAINING_SKILLS`.
    static let chainingSkills: Set<String> = ["roulade"]

    /// Occupancy duration (seconds) for a `do` skill event, given the
    /// drive mode active when it starts (`"walk"`, `"roller"`, or `nil`
    /// when no `mode` event precedes it). `nil` when no confirmed duration
    /// exists (currently only `sit_toggle`). Mirrors
    /// `python/duckshow/limits.py`'s `skill_duration_s`.
    static func skillDurationS(_ skill: String, mode: String?) -> Double? {
        if skill == "ground_pick" && mode == "roller" { return groundPickRollerDurationS }
        return skillDurationsS[skill]
    }

    // Limits — see docs/duckshow-format.md "Validation limits".
    /// 0.40 = the edge of alpha_walking.onnx's training distribution
    /// (lin_vel_x sampled uniformly from (-0.4, 0.4)). The previous
    /// 0.25/0.20 sat below the policy's stand/walk gate on three of four
    /// axes, producing no motion rather than slow motion. Mirrors
    /// python/duckshow/limits.py; see docs/duckshow-format.md
    /// "Why the translation limits are 0.40".
    private var limitVx: Double { 0.40 }
    private var limitVy: Double { 0.40 }
    private var limitVyaw: Double { 1.5 }
    private var limitHeadAngle: Double { 1.2 }
    private var limitPoseZ: Double { 0.05 }
    private var limitPoseRollPitch: Double { 0.5 }
    private var minEventGapSeconds: Double { 0.25 }
    /// `mode_locomotion_guard_s` in python/duckshow/limits.py.
    private var modeLocomotionGuardSeconds: Double { 0.5 }
    /// `_EPS` in python/duckshow/validator.py.
    private var epsilon: Double { 1e-9 }

    /// Parity with `validator.py:_check_servo`. The servo track is reserved
    /// in v1, but a file can carry it today and the diagnostics are cheap: a
    /// zero or negative window is silently never entered, and a mode other
    /// than "hold" has no effect on a v1 agent -- a warning, not an error,
    /// because the file is legal. This was never called at all here, so the
    /// whole track went uninspected while Python and the editor checked it.
    private func validateServo(_ entries: [ServoWindow], role: String, into report: inout ValidationReport) {
        for e in entries {
            if !e.t.isFinite {
                report.errors.append(ValidationIssue(role: role, track: "servo", t: e.t,
                    message: "t=\(e.t) is not a finite number"))
            } else if e.t < 0 {
                report.errors.append(ValidationIssue(role: role, track: "servo", t: e.t,
                    message: "servo t=\(e.t) must be >= 0"))
            }
            if let duration = e.duration {
                if !duration.isFinite {
                    report.errors.append(ValidationIssue(role: role, track: "servo", t: e.t,
                        message: "duration=\(duration) is not a finite number"))
                } else if duration <= 0 {
                    report.errors.append(ValidationIssue(role: role, track: "servo", t: e.t,
                        message: "servo duration=\(duration) must be > 0"))
                }
            }
            if e.mode != "hold" {
                report.warnings.append(ValidationIssue(role: role, track: "servo", t: e.t,
                    message: "servo mode '\(e.mode)' is not honored by v1 agents (only 'hold' has any effect)"))
            }
        }
    }

    /// Parity with `validator.py:_check_event_fields`. Had no Swift
    /// equivalent, so a negative or non-finite event time passed silently.
    private func validateEventFields(_ events: [Event], role: String, into report: inout ValidationReport) {
        for e in events {
            if !e.t.isFinite {
                report.errors.append(ValidationIssue(role: role, track: "events", t: e.t,
                    message: "t=\(e.t) is not a finite number"))
            } else if e.t < 0 {
                report.errors.append(ValidationIssue(role: role, track: "events", t: e.t,
                    message: "event t=\(e.t) must be >= 0"))
            }
            // `hold` lives on the sound case rather than on Event itself.
            if case .sound(_, let hold) = e.action, let hold, !hold.isFinite {
                report.errors.append(ValidationIssue(role: role, track: "events", t: e.t,
                    message: "hold=\(hold) is not a finite number"))
            }
        }
    }

    /// Renders a collection the way Python's `repr()` of a tuple does --
    /// `('walk', 'roller')` -- because the canonical validator embeds tuple
    /// reprs directly in its message text, and shows/fixtures/expected.json's
    /// `error_substr` entries are matched against that text. Swift's own array
    /// interpolation gives `["walk", "roller"]`, so the same show produced
    /// different issue strings here than in the other two implementations.
    /// The editor solves this with its own `pyTuple()`.
    private func pyTuple(_ items: [String]) -> String {
        if items.count == 1 { return "('\(items[0])',)" }  // Python's 1-tuple has a trailing comma
        return "(" + items.map { "'\($0)'" }.joined(separator: ", ") + ")"
    }

    private func checkSorted<T>(
        _ keyframes: [T], role: String, track: String, t: (T) -> Double, into report: inout ValidationReport
    ) {
        var previous: Double?
        for kf in keyframes {
            let value = t(kf)
            // Parity with validator.py:_check_sorted_unique. A keyframe time
            // that is negative or not a number is not a point on any
            // timeline; this was silently accepted here while Python and the
            // editor both rejected it.
            if !value.isFinite {
                report.errors.append(ValidationIssue(role: role, track: track, t: value,
                    message: "t=\(value) is not a finite number"))
            } else if value < 0 {
                report.errors.append(ValidationIssue(role: role, track: track, t: value,
                    message: "\(track) keyframe t=\(value) must be >= 0"))
            }
            if let previous {
                if value == previous {
                    report.errors.append(ValidationIssue(role: role, track: track, t: value,
                        message: "duplicate t=\(value) in \(track) track"))
                } else if value < previous {
                    report.errors.append(ValidationIssue(role: role, track: track, t: value,
                        message: "\(track) keyframes are not sorted by t"))
                }
            }
            previous = value
        }
    }

    /// Same wording as `validator.py:_check_scalar_limit`, so tooling that
    /// greps either validator's output (and shows/fixtures/expected.json's
    /// `error_substr`) sees the same text.
    private func checkScalarLimit(
        _ value: Double, name: String, limit: Double, role: String, track: String, t: Double,
        into report: inout ValidationReport
    ) {
        if abs(value) > limit + epsilon {
            report.errors.append(ValidationIssue(role: role, track: track, t: t,
                message: "\(name)=\(value) exceeds limit of +/-\(limit)"))
        }
    }

    private func validateLocomotion(_ kfs: [LocomotionKeyframe], role: String, into report: inout ValidationReport) {
        checkSorted(kfs, role: role, track: "locomotion", t: { $0.t }, into: &report)
        for kf in kfs {
            checkScalarLimit(kf.vx, name: "vx", limit: limitVx, role: role, track: "locomotion", t: kf.t, into: &report)
            checkScalarLimit(kf.vy, name: "vy", limit: limitVy, role: role, track: "locomotion", t: kf.t, into: &report)
            checkScalarLimit(kf.vyaw, name: "vyaw", limit: limitVyaw, role: role, track: "locomotion", t: kf.t, into: &report)
        }
    }

    private func validateHead(_ kfs: [HeadKeyframe], role: String, into report: inout ValidationReport) {
        checkSorted(kfs, role: role, track: "head", t: { $0.t }, into: &report)
        for kf in kfs {
            for (name, value) in [
                ("neck_pitch", kf.neckPitch), ("head_pitch", kf.headPitch),
                ("head_yaw", kf.headYaw), ("head_roll", kf.headRoll)
            ] {
                checkScalarLimit(value, name: name, limit: limitHeadAngle, role: role, track: "head", t: kf.t, into: &report)
            }
        }
    }

    private func validatePose(_ kfs: [PoseKeyframe], role: String, into report: inout ValidationReport) {
        checkSorted(kfs, role: role, track: "pose", t: { $0.t }, into: &report)
        for kf in kfs {
            checkScalarLimit(kf.z, name: "z", limit: limitPoseZ, role: role, track: "pose", t: kf.t, into: &report)
            checkScalarLimit(kf.roll, name: "roll", limit: limitPoseRollPitch, role: role, track: "pose", t: kf.t, into: &report)
            checkScalarLimit(kf.pitch, name: "pitch", limit: limitPoseRollPitch, role: role, track: "pose", t: kf.t, into: &report)
        }
    }

    private func validateMouth(_ kfs: [MouthKeyframe], role: String, into report: inout ValidationReport) {
        checkSorted(kfs, role: role, track: "mouth", t: { $0.t }, into: &report)
        for kf in kfs where kf.open < 0.0 - epsilon || kf.open > 1.0 + epsilon {
            report.errors.append(ValidationIssue(role: role, track: "mouth", t: kf.t,
                message: "open=\(kf.open) outside allowed range [0.0, 1.0]"))
        }
    }

    /// Event-track rules. Unlike the curve tracks, the doc imposes no
    /// ordering on `events` (its own example lists them in authoring
    /// order), so — like `validator.py:_check_event_density` — the density
    /// check runs on a copy sorted by `t`.
    private func validateEvents(_ events: [Event], role: String, into report: inout ValidationReport) {
        for event in events where !event.extraActionKeys.isEmpty {
            // Python renders the list with repr(), so the strings are quoted:
            // ["'sound'", "'mode'"] -> ['sound', 'mode']. The editor already
            // matches via pyRepr(); Swift printed them bare, so the same show
            // produced different issue text here than in the other two.
            let keys = ([primaryActionKey(event.action)] + event.extraActionKeys)
                .map { "'\($0)'" }.joined(separator: ", ")
            report.errors.append(ValidationIssue(role: role, track: "events", t: event.t,
                message: "event has more than one action key: [\(keys)]"))
        }
        var previous: Double?
        for event in events.sorted(by: { $0.t < $1.t }) {
            if let previous, event.t - previous < minEventGapSeconds - epsilon {
                report.errors.append(ValidationIssue(role: role, track: "events", t: event.t,
                    message: "event at t=\(event.t) is less than \(minEventGapSeconds)s after previous event at t=\(previous)"))
            }
            previous = event.t
        }
    }

    /// Parity with `validator.py:_check_event_action`'s enum checks: a `do`
    /// naming a skill outside `Show.skills`, or a `sound` naming a tag
    /// outside `Show.soundTags`, can never succeed on real hardware
    /// (docs/robotd-api.md's closed Skill/SoundTag enums), so both are
    /// errors, not warnings.
    private func validateEventActionNames(_ events: [Event], role: String, into report: inout ValidationReport) {
        for event in events {
            switch event.action {
            case .skill(let name):
                guard !Self.skills.contains(name) else { continue }
                report.errors.append(ValidationIssue(role: role, track: "events", t: event.t,
                    message: "do='\(name)' is not a recognized skill (expected one of \(pyTuple(Self.skills)))"))
            case .sound(let tag, hold: _):
                guard !Self.soundTags.contains(tag) else { continue }
                report.errors.append(ValidationIssue(role: role, track: "events", t: event.t,
                    message: "sound='\(tag)' is not a recognized sound tag (expected one of \(pyTuple(Self.soundTags)))"))
            case .mode:
                continue
            }
        }
    }

    private func primaryActionKey(_ action: Event.Action) -> String {
        switch action {
        case .skill: return "do"
        case .sound: return "sound"
        case .mode: return "mode"
        }
    }

    /// Parity with `validator.py:_check_mode_value`: a `mode` event's value
    /// must be a real robotd drive mode -- real hardware accepts exactly
    /// "walk" or "roller" over the wire and has no mechanism to register a
    /// custom-named mode (docs/robotd-api.md "Custom .onnx policies &
    /// modes"; docs/duckshow-format.md "Custom .onnx policies"). A
    /// custom-trained gait is installed by pointing a fixed policy *slot*
    /// at a different .onnx file (requires.policies[]), never by inventing
    /// a new mode string, so `requires.policies` plays no part in whether a
    /// `mode` event is valid -- unlike the old (incorrect) contract this
    /// replaces, this is an ERROR, not a warning.
    private func validateModeValue(_ events: [Event], role: String, into report: inout ValidationReport) {
        for event in events {
            guard let name = event.modeName, !Self.driveModes.contains(name) else { continue }
            report.errors.append(ValidationIssue(role: role, track: "events", t: event.t,
                message: "mode='\(name)' is not a valid drive mode (expected one of \(pyTuple(Self.driveModes)))"))
        }
    }

    /// "The validator warns if a `mode` event overlaps nonzero locomotion
    /// within ±0.5 s" (docs/duckshow-format.md). The condition is on the
    /// *sampled* locomotion the duck is actually commanded with — held or
    /// interpolated from earlier keyframes — not on where keyframes happen
    /// to sit, so this samples the curve at the window edges and at every
    /// keyframe inside the window, exactly like
    /// `validator.py:_locomotion_nonzero_in_window`.
    private func validateModeOverlap(
        _ events: [Event], locomotion: [LocomotionKeyframe], role: String, into report: inout ValidationReport
    ) {
        guard !locomotion.isEmpty else { return }
        let sorted = locomotion.sorted { $0.t < $1.t }
        for event in events {
            guard let name = event.modeName else { continue }
            let lo = max(0.0, event.t - modeLocomotionGuardSeconds)
            let hi = event.t + modeLocomotionGuardSeconds
            var times: Set<Double> = [lo, hi]
            for kf in sorted where lo <= kf.t && kf.t <= hi { times.insert(kf.t) }
            let nonzero = times.sorted().contains { t in
                let v = sampleLocomotion(sorted, at: t)
                return abs(v.vx) > epsilon || abs(v.vy) > epsilon || abs(v.vyaw) > epsilon
            }
            if nonzero {
                report.warnings.append(ValidationIssue(role: role, track: "events", t: event.t,
                    message: "mode event '\(name)' overlaps nonzero locomotion within ±\(modeLocomotionGuardSeconds)s"))
            }
        }
    }

    /// Minimal locomotion sampler mirroring `python/duckshow/sampler.py`:
    /// hold-first before the first keyframe, hold-last after the last,
    /// per-segment `interp` (step / linear / smoothstep) in between, and
    /// zero at/after `meta.duration`. `keyframes` must be sorted by `t`.
    private func sampleLocomotion(_ keyframes: [LocomotionKeyframe], at t: Double) -> (vx: Double, vy: Double, vyaw: Double) {
        guard let first = keyframes.first, let last = keyframes.last else { return (0, 0, 0) }
        if t >= meta.duration { return (0, 0, 0) }
        if t <= first.t { return (first.vx, first.vy, first.vyaw) }
        if t >= last.t { return (last.vx, last.vy, last.vyaw) }
        // Index of the last keyframe with kf.t <= t (bisect_right - 1).
        var lo = 0, hi = keyframes.count
        while lo < hi {
            let mid = (lo + hi) / 2
            if keyframes[mid].t <= t { lo = mid + 1 } else { hi = mid }
        }
        let kf0 = keyframes[lo - 1]
        let kf1 = keyframes[lo]
        let span = kf1.t - kf0.t
        var frac = span <= 0 ? 0.0 : (t - kf0.t) / span
        switch kf0.interp {
        case .step:
            return (kf0.vx, kf0.vy, kf0.vyaw)
        case .smooth:
            frac = min(1, max(0, frac))
            frac = frac * frac * (3 - 2 * frac)
        case .linear:
            frac = min(1, max(0, frac))
        }
        return (
            kf0.vx + (kf1.vx - kf0.vx) * frac,
            kf0.vy + (kf1.vy - kf0.vy) * frac,
            kf0.vyaw + (kf1.vyaw - kf0.vyaw) * frac
        )
    }

    /// The latest `mode` event with `t <= at` (for seek/late-join gait
    /// resolution, and here for resolving which drive mode a `do` skill
    /// starts in). Mirrors `python/duckshow/sampler.py`'s `Sampler.mode_at`.
    private func modeAt(_ events: [Event], at t: Double) -> String? {
        var best: (t: Double, mode: String)?
        for event in events {
            guard let name = event.modeName, event.t <= t else { continue }
            if best == nil || event.t > best!.t { best = (event.t, name) }
        }
        return best?.mode
    }

    /// Parity with `validator.py:_check_skill_occupancy_overlap` /
    /// `limits.py`'s `SKILL_DURATIONS_S` etc: a `do` skill runs its whole
    /// episodic clip to completion once started -- scheduling a second
    /// skill inside that window schedules against a duck that physically
    /// cannot have finished the first one yet. WARNING, not an error: the
    /// robot still accepts the command and something happens, so this is
    /// very likely not what the author meant rather than something unsafe.
    /// Distinct from `validateEvents`'s 0.25s spacing rule, which is about
    /// command flooding and applies to every discrete event regardless of
    /// type -- the two run independently and can both fire on one pair.
    ///
    /// Only consecutive pairs of `do` events are compared (mirroring
    /// `validateEvents`'s `previous` walk), each against the skill
    /// immediately before it in time order, not every earlier skill.
    /// `roulade` is "chain": true in the manifest: a `roulade` immediately
    /// following a `roulade` is the documented way to keep rolling, not two
    /// skills contending for one window (`Show.chainingSkills`), so that
    /// specific pairing never warns. `sit_toggle` has no confirmed duration
    /// (`Show.skillDurationS` returns `nil` for it) and so never opens a
    /// window here. It can still be the later, interrupting event.
    private func validateSkillOccupancyOverlap(
        _ events: [Event], role: String, into report: inout ValidationReport
    ) {
        let skillEvents = events.compactMap { event -> (t: Double, skill: String)? in
            guard case .skill(let name) = event.action else { return nil }
            return (event.t, name)
        }.sorted { $0.t < $1.t }
        guard skillEvents.count >= 2 else { return }
        var prev = skillEvents[0]
        for cur in skillEvents.dropFirst() {
            defer { prev = cur }
            if Self.chainingSkills.contains(prev.skill) && cur.skill == prev.skill { continue }
            guard let duration = Self.skillDurationS(prev.skill, mode: modeAt(events, at: prev.t)) else { continue }
            let end = prev.t + duration
            if cur.t < end - epsilon {
                let overlap = end - cur.t
                report.warnings.append(ValidationIssue(role: role, track: "events", t: cur.t,
                    message: "do='\(cur.skill)' at t=\(cur.t) begins \(overlap)s into the \(duration)s execution of do='\(prev.skill)' at t=\(prev.t)"))
            }
        }
    }
}
