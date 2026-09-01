// DuckShow.swift
//
// Codable model for the `.duckshow` file format.
// Mirrors docs/duckshow-format.md exactly. Unknown JSON fields are ignored
// everywhere (default JSONDecoder behavior against explicit CodingKeys),
// matching "forward compatibility, same discipline as StageWizard show files".

import Foundation
#if canImport(CryptoKit)
import CryptoKit
#endif

// MARK: - Errors

public enum DuckShowError: Error, Sendable, Equatable, CustomStringConvertible {
    /// `format` was present but not a major version this package understands.
    case unsupportedFormat(String)

    public var description: String {
        switch self {
        case .unsupportedFormat(let format):
            return "unsupported .duckshow format '\(format)' (expected duckshow/\(SwarmLinkInfo.duckShowFormatMajor))"
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
    public var name: String
    public var author: String
    /// Kept as the raw string from the file (e.g. "2026-09-01"). The show
    /// format does not specify a time component, so we avoid a lossy/strict
    /// Date decode here and let callers parse it if they need to.
    public var created: String
    public var duration: Double
    public var music: Music?

    public init(name: String, author: String, created: String, duration: Double, music: Music? = nil) {
        self.name = name
        self.author = author
        self.created = created
        self.duration = duration
        self.music = music
    }
}

public struct Music: Codable, Sendable, Equatable {
    public var file: String
    public var bpm: Double
    public var beatOffset: Double

    private enum CodingKeys: String, CodingKey {
        case file, bpm
        case beatOffset = "beat_offset"
    }

    public init(file: String, bpm: Double, beatOffset: Double) {
        self.file = file
        self.bpm = bpm
        self.beatOffset = beatOffset
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
/// docs/duckshow-format.md.
public struct RequiredPolicy: Codable, Sendable, Equatable {
    public var name: String
    public var mode: String
    public var file: String
    public var sha256: String
    public var slot: String

    public init(name: String, mode: String, file: String, sha256: String, slot: String) {
        self.name = name
        self.mode = mode
        self.file = file
        self.sha256 = sha256
        self.slot = slot
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
        vx = try c.decode(Double.self, forKey: .vx)
        vy = try c.decode(Double.self, forKey: .vy)
        vyaw = try c.decode(Double.self, forKey: .vyaw)
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
        neckPitch = try c.decode(Double.self, forKey: .neckPitch)
        headPitch = try c.decode(Double.self, forKey: .headPitch)
        headYaw = try c.decode(Double.self, forKey: .headYaw)
        headRoll = try c.decode(Double.self, forKey: .headRoll)
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
        z = try c.decode(Double.self, forKey: .z)
        roll = try c.decode(Double.self, forKey: .roll)
        pitch = try c.decode(Double.self, forKey: .pitch)
        active = try c.decode(Bool.self, forKey: .active)
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
        open = try c.decode(Double.self, forKey: .open)
        interp = try c.decodeIfPresent(Interp.self, forKey: .interp) ?? .linear
    }
}

// MARK: - Events

/// One point event. Exactly one of `do` / `sound` / `mode` is set per entry,
/// per docs/duckshow-format.md "Event track".
public struct Event: Codable, Sendable, Equatable {
    public var t: Double
    public var action: Action

    public enum Action: Sendable, Equatable {
        /// `"do": "<skill>"` → `robot.do`.
        case skill(String)
        /// `"sound": "<tag>"`, optional `"hold": <seconds>` → `robot.sound` (+ release after hold).
        case sound(String, hold: Double?)
        /// `"mode": "<policy-mode>"` → `robot.setMode`.
        case mode(String)
    }

    public init(t: Double, action: Action) {
        self.t = t
        self.action = action
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
        if let skill = try c.decodeIfPresent(String.self, forKey: .doAction) {
            action = .skill(skill)
        } else if let sound = try c.decodeIfPresent(String.self, forKey: .sound) {
            let hold = try c.decodeIfPresent(Double.self, forKey: .hold)
            action = .sound(sound, hold: hold)
        } else if let mode = try c.decodeIfPresent(String.self, forKey: .mode) {
            action = .mode(mode)
        } else {
            throw DecodingError.dataCorruptedError(
                forKey: .t, in: c,
                debugDescription: "event at t=\(t) has none of do/sound/mode"
            )
        }
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
    public var mode: String
    public var duration: Double?
    /// Used by future modes (`color_homing`, etc.); v1 agents only honor `hold`.
    public var target: String?

    public init(t: Double, mode: String, duration: Double? = nil, target: String? = nil) {
        self.t = t
        self.mode = mode
        self.duration = duration
        self.target = target
    }
}

// MARK: - Loading

public extension Show {
    /// Decodes a `.duckshow.json` file and rejects unknown major format
    /// versions ("Parsers reject unknown major versions" in the doc).
    static func load(contentsOf url: URL) throws -> Show {
        let data = try Data(contentsOf: url)
        let show = try JSONDecoder().decode(Show.self, from: data)
        guard show.format == "duckshow/\(SwarmLinkInfo.duckShowFormatMajor)" else {
            throw DuckShowError.unsupportedFormat(show.format)
        }
        return show
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

        let castRoles = Set(cast.map(\.role))
        for role in castRoles where tracks[role] == nil {
            report.warnings.append(ValidationIssue(role: role, track: "tracks", t: nil,
                message: "cast role has no track entry (treated as idle)"))
        }
        for trackRole in tracks.keys where !castRoles.contains(trackRole) {
            report.warnings.append(ValidationIssue(role: trackRole, track: "tracks", t: nil,
                message: "track entry has no matching cast role"))
        }

        for (role, roleTracks) in tracks {
            validateLocomotion(roleTracks.locomotion, role: role, into: &report)
            validateHead(roleTracks.head, role: role, into: &report)
            validatePose(roleTracks.pose, role: role, into: &report)
            validateMouth(roleTracks.mouth, role: role, into: &report)
            validateEvents(roleTracks.events, role: role, into: &report)
            validateModeOverlap(roleTracks.events, locomotion: roleTracks.locomotion, role: role, into: &report)
        }

        return report
    }

    // Limits — see docs/duckshow-format.md "Validation limits".
    private var limitVx: Double { 0.25 }
    private var limitVy: Double { 0.20 }
    private var limitVyaw: Double { 1.5 }
    private var limitHeadAngle: Double { 1.2 }
    private var limitPoseZ: Double { 0.05 }
    private var limitPoseRollPitch: Double { 0.5 }
    private var minEventGapSeconds: Double { 0.25 }

    private func checkSorted<T>(
        _ keyframes: [T], role: String, track: String, t: (T) -> Double, into report: inout ValidationReport
    ) {
        var previous: Double?
        for kf in keyframes {
            let value = t(kf)
            if let previous {
                if value == previous {
                    report.errors.append(ValidationIssue(role: role, track: track, t: value,
                        message: "duplicate keyframe time"))
                } else if value < previous {
                    report.errors.append(ValidationIssue(role: role, track: track, t: value,
                        message: "keyframes not sorted by t"))
                }
            }
            previous = value
        }
    }

    private func validateLocomotion(_ kfs: [LocomotionKeyframe], role: String, into report: inout ValidationReport) {
        checkSorted(kfs, role: role, track: "locomotion", t: { $0.t }, into: &report)
        for kf in kfs {
            if abs(kf.vx) > limitVx {
                report.errors.append(ValidationIssue(role: role, track: "locomotion", t: kf.t,
                    message: "|vx|=\(abs(kf.vx)) exceeds \(limitVx) m/s"))
            }
            if abs(kf.vy) > limitVy {
                report.errors.append(ValidationIssue(role: role, track: "locomotion", t: kf.t,
                    message: "|vy|=\(abs(kf.vy)) exceeds \(limitVy) m/s"))
            }
            if abs(kf.vyaw) > limitVyaw {
                report.errors.append(ValidationIssue(role: role, track: "locomotion", t: kf.t,
                    message: "|vyaw|=\(abs(kf.vyaw)) exceeds \(limitVyaw) rad/s"))
            }
        }
    }

    private func validateHead(_ kfs: [HeadKeyframe], role: String, into report: inout ValidationReport) {
        checkSorted(kfs, role: role, track: "head", t: { $0.t }, into: &report)
        for kf in kfs {
            for (name, value) in [
                ("neck_pitch", kf.neckPitch), ("head_pitch", kf.headPitch),
                ("head_yaw", kf.headYaw), ("head_roll", kf.headRoll)
            ] where abs(value) > limitHeadAngle {
                report.errors.append(ValidationIssue(role: role, track: "head", t: kf.t,
                    message: "|\(name)|=\(abs(value)) exceeds \(limitHeadAngle) rad"))
            }
        }
    }

    private func validatePose(_ kfs: [PoseKeyframe], role: String, into report: inout ValidationReport) {
        checkSorted(kfs, role: role, track: "pose", t: { $0.t }, into: &report)
        for kf in kfs {
            if abs(kf.z) > limitPoseZ {
                report.errors.append(ValidationIssue(role: role, track: "pose", t: kf.t,
                    message: "|z|=\(abs(kf.z)) exceeds \(limitPoseZ) m"))
            }
            if abs(kf.roll) > limitPoseRollPitch {
                report.errors.append(ValidationIssue(role: role, track: "pose", t: kf.t,
                    message: "|roll|=\(abs(kf.roll)) exceeds \(limitPoseRollPitch) rad"))
            }
            if abs(kf.pitch) > limitPoseRollPitch {
                report.errors.append(ValidationIssue(role: role, track: "pose", t: kf.t,
                    message: "|pitch|=\(abs(kf.pitch)) exceeds \(limitPoseRollPitch) rad"))
            }
        }
    }

    private func validateMouth(_ kfs: [MouthKeyframe], role: String, into report: inout ValidationReport) {
        checkSorted(kfs, role: role, track: "mouth", t: { $0.t }, into: &report)
        for kf in kfs where !(0.0...1.0).contains(kf.open) {
            report.errors.append(ValidationIssue(role: role, track: "mouth", t: kf.t,
                message: "open=\(kf.open) outside 0.0-1.0"))
        }
    }

    private func validateEvents(_ events: [Event], role: String, into report: inout ValidationReport) {
        checkSorted(events, role: role, track: "events", t: { $0.t }, into: &report)
        var previous: Double?
        for event in events {
            if let previous, event.t - previous < minEventGapSeconds, event.t >= previous {
                report.errors.append(ValidationIssue(role: role, track: "events", t: event.t,
                    message: "event density: \(event.t - previous)s since previous event, minimum \(minEventGapSeconds)s"))
            }
            previous = event.t
        }
    }

    private func validateModeOverlap(
        _ events: [Event], locomotion: [LocomotionKeyframe], role: String, into report: inout ValidationReport
    ) {
        guard !locomotion.isEmpty else { return }
        for event in events {
            guard case .mode = event.action else { continue }
            let window = (event.t - 0.5)...(event.t + 0.5)
            let nonzeroNearby = locomotion.contains { kf in
                window.contains(kf.t) && (kf.vx != 0 || kf.vy != 0 || kf.vyaw != 0)
            }
            if nonzeroNearby {
                report.warnings.append(ValidationIssue(role: role, track: "events", t: event.t,
                    message: "mode event overlaps nonzero locomotion within ±0.5s"))
            }
        }
    }
}
