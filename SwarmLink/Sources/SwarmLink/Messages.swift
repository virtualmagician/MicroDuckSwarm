// Messages.swift
//
// Codable wire types for docs/swarmlink-protocol.md. Every message is one
// JSON object in one UDP datagram; field names are snake_case on the wire.
// Unknown `type` is dropped silently by `SwarmMessage.decode`; unknown
// fields within a known message are ignored by JSONDecoder by default.

import Foundation

/// Transport state as broadcast in `state` messages and mirrored locally
/// by `SwarmMaster`. See docs/swarmlink-protocol.md §2 and §5.
public enum Transport: String, Codable, Sendable, Equatable {
    case stopped
    case armed
    case playing
}

/// Agent-reported state, as carried in `telemetry`. See §4 and §5.
public enum AgentState: String, Codable, Sendable, Equatable {
    case idle
    case loaded
    case armed
    case playing
    case degraded
    case fault
}

// MARK: - time_req / time_resp (§1)

/// `agent → master`: `{"v":1,"type":"time_req","duck":"duck-01","t0":<ns>}`
public struct TimeRequest: Sendable, Equatable {
    public static let type = "time_req"

    public var v: Int
    public var duck: DuckID
    /// Agent's monotonic send time, nanoseconds.
    public var t0: Int64

    public init(v: Int = SwarmLinkInfo.protocolVersion, duck: DuckID, t0: Int64) {
        self.v = v
        self.duck = duck
        self.t0 = t0
    }
}

extension TimeRequest: Codable {
    enum CodingKeys: String, CodingKey { case v, type, duck, t0 }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        v = try c.decode(Int.self, forKey: .v)
        duck = try c.decode(DuckID.self, forKey: .duck)
        t0 = try c.decode(Int64.self, forKey: .t0)
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(v, forKey: .v)
        try c.encode(Self.type, forKey: .type)
        try c.encode(duck, forKey: .duck)
        try c.encode(t0, forKey: .t0)
    }
}

/// `master → agent`: `{"v":1,"type":"time_resp","t0":<echoed>,"t1":<rx_ns>,"t2":<tx_ns>}`
public struct TimeResponse: Sendable, Equatable {
    public static let type = "time_resp"

    public var v: Int
    /// Echoed from the request.
    public var t0: Int64
    /// Master's monotonic receive time, nanoseconds.
    public var t1: Int64
    /// Master's monotonic send time, nanoseconds.
    public var t2: Int64

    public init(v: Int = SwarmLinkInfo.protocolVersion, t0: Int64, t1: Int64, t2: Int64) {
        self.v = v
        self.t0 = t0
        self.t1 = t1
        self.t2 = t2
    }
}

extension TimeResponse: Codable {
    enum CodingKeys: String, CodingKey { case v, type, t0, t1, t2 }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        v = try c.decode(Int.self, forKey: .v)
        t0 = try c.decode(Int64.self, forKey: .t0)
        t1 = try c.decode(Int64.self, forKey: .t1)
        t2 = try c.decode(Int64.self, forKey: .t2)
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(v, forKey: .v)
        try c.encode(Self.type, forKey: .type)
        try c.encode(t0, forKey: .t0)
        try c.encode(t1, forKey: .t1)
        try c.encode(t2, forKey: .t2)
    }
}

// MARK: - state (§2)

/// `master → agent`, unicast, 5 Hz:
/// `{"v":1,"type":"state","seq":421,"show":"<id>","transport":"...","show_time":12.48,"master_time":<ns>}`
public struct StateMessage: Sendable, Equatable {
    public static let type = "state"

    public var v: Int
    public var seq: Int
    public var show: String
    public var transport: Transport
    public var showTime: Double
    public var masterTime: Int64

    public init(
        v: Int = SwarmLinkInfo.protocolVersion, seq: Int, show: String, transport: Transport,
        showTime: Double, masterTime: Int64
    ) {
        self.v = v
        self.seq = seq
        self.show = show
        self.transport = transport
        self.showTime = showTime
        self.masterTime = masterTime
    }
}

extension StateMessage: Codable {
    enum CodingKeys: String, CodingKey {
        case v, type, seq, show, transport
        case showTime = "show_time"
        case masterTime = "master_time"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        v = try c.decode(Int.self, forKey: .v)
        seq = try c.decode(Int.self, forKey: .seq)
        show = try c.decode(String.self, forKey: .show)
        transport = try c.decode(Transport.self, forKey: .transport)
        showTime = try c.decode(Double.self, forKey: .showTime)
        masterTime = try c.decode(Int64.self, forKey: .masterTime)
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(v, forKey: .v)
        try c.encode(Self.type, forKey: .type)
        try c.encode(seq, forKey: .seq)
        try c.encode(show, forKey: .show)
        try c.encode(transport, forKey: .transport)
        try c.encode(showTime, forKey: .showTime)
        try c.encode(masterTime, forKey: .masterTime)
    }
}

// MARK: - cmd / ack (§3)

/// `master → agent`, unicast, repeated, ACKed:
/// `{"v":1,"type":"cmd","cmd_id":"<uuid>","cmd":"load"|"play"|"stop"|"seek"|"panic", ...}`
public struct CommandMessage: Sendable, Equatable {
    public static let type = "cmd"

    public var v: Int
    public var cmdID: String
    public var payload: Payload

    public enum Payload: Sendable, Equatable {
        /// `"show": id, "sha256": …, "role": "lead"`
        case load(show: String, sha256: String, role: String)
        /// `"show": id, "at_master_time": <ns>, "from_show_time": 0.0`
        case play(show: String, atMasterTime: Int64, fromShowTime: Double)
        /// `"show_time": 45.0, "at_master_time": <ns>`
        case seek(showTime: Double, atMasterTime: Int64)
        case stop
        case panic

        var cmdName: String {
            switch self {
            case .load: return "load"
            case .play: return "play"
            case .seek: return "seek"
            case .stop: return "stop"
            case .panic: return "panic"
            }
        }
    }

    public init(v: Int = SwarmLinkInfo.protocolVersion, cmdID: String, payload: Payload) {
        self.v = v
        self.cmdID = cmdID
        self.payload = payload
    }
}

extension CommandMessage: Codable {
    enum CodingKeys: String, CodingKey {
        case v, type
        case cmdID = "cmd_id"
        case cmd
        case show, sha256, role
        case atMasterTime = "at_master_time"
        case fromShowTime = "from_show_time"
        case showTime = "show_time"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        v = try c.decode(Int.self, forKey: .v)
        cmdID = try c.decode(String.self, forKey: .cmdID)
        let cmd = try c.decode(String.self, forKey: .cmd)
        switch cmd {
        case "load":
            payload = .load(
                show: try c.decode(String.self, forKey: .show),
                sha256: try c.decode(String.self, forKey: .sha256),
                role: try c.decode(String.self, forKey: .role)
            )
        case "play":
            payload = .play(
                show: try c.decode(String.self, forKey: .show),
                atMasterTime: try c.decode(Int64.self, forKey: .atMasterTime),
                fromShowTime: try c.decodeIfPresent(Double.self, forKey: .fromShowTime) ?? 0.0
            )
        case "seek":
            payload = .seek(
                showTime: try c.decode(Double.self, forKey: .showTime),
                atMasterTime: try c.decode(Int64.self, forKey: .atMasterTime)
            )
        case "stop":
            payload = .stop
        case "panic":
            payload = .panic
        default:
            throw DecodingError.dataCorruptedError(forKey: .cmd, in: c, debugDescription: "unknown cmd '\(cmd)'")
        }
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(v, forKey: .v)
        try c.encode(Self.type, forKey: .type)
        try c.encode(cmdID, forKey: .cmdID)
        try c.encode(payload.cmdName, forKey: .cmd)
        switch payload {
        case .load(let show, let sha256, let role):
            try c.encode(show, forKey: .show)
            try c.encode(sha256, forKey: .sha256)
            try c.encode(role, forKey: .role)
        case .play(let show, let atMasterTime, let fromShowTime):
            try c.encode(show, forKey: .show)
            try c.encode(atMasterTime, forKey: .atMasterTime)
            try c.encode(fromShowTime, forKey: .fromShowTime)
        case .seek(let showTime, let atMasterTime):
            try c.encode(showTime, forKey: .showTime)
            try c.encode(atMasterTime, forKey: .atMasterTime)
        case .stop, .panic:
            break
        }
    }
}

/// `agent → master`: `{"v":1,"type":"ack","duck":"duck-01","cmd_id":"<uuid>","ok":true,"error":null}`
public struct AckMessage: Sendable, Equatable {
    public static let type = "ack"

    public var v: Int
    public var duck: DuckID
    public var cmdID: String
    public var ok: Bool
    public var error: String?

    public init(v: Int = SwarmLinkInfo.protocolVersion, duck: DuckID, cmdID: String, ok: Bool, error: String? = nil) {
        self.v = v
        self.duck = duck
        self.cmdID = cmdID
        self.ok = ok
        self.error = error
    }
}

extension AckMessage: Codable {
    enum CodingKeys: String, CodingKey {
        case v, type, duck
        case cmdID = "cmd_id"
        case ok, error
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        v = try c.decode(Int.self, forKey: .v)
        duck = try c.decode(DuckID.self, forKey: .duck)
        cmdID = try c.decode(String.self, forKey: .cmdID)
        ok = try c.decode(Bool.self, forKey: .ok)
        error = try c.decodeIfPresent(String.self, forKey: .error)
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(v, forKey: .v)
        try c.encode(Self.type, forKey: .type)
        try c.encode(duck, forKey: .duck)
        try c.encode(cmdID, forKey: .cmdID)
        try c.encode(ok, forKey: .ok)
        try c.encodeIfPresent(error, forKey: .error)
    }
}

// MARK: - telemetry (§4)

/// `agent → master`, unicast, 1 Hz (5 Hz while PLAYING).
public struct TelemetryMessage: Sendable, Equatable {
    public static let type = "telemetry"

    public var v: Int
    public var duck: DuckID
    public var seq: Int
    public var state: AgentState
    public var show: String?
    public var showTime: Double
    /// Milliseconds; `nil` until the first successful time-sync exchange
    /// (docs/swarmlink-protocol.md §4: "null until the first successful
    /// time-sync exchange — masters must decode them as optional and treat
    /// null as 'not yet synced', never as 0").
    public var clockOffsetMs: Double?
    /// Milliseconds; `nil` until the first successful time-sync exchange —
    /// see `clockOffsetMs`.
    public var clockRttMs: Double?
    public var policiesOk: Bool
    public var batteryPct: Double?
    public var rssiDbm: Double?
    public var lastError: String?

    public init(
        v: Int = SwarmLinkInfo.protocolVersion, duck: DuckID, seq: Int, state: AgentState, show: String?,
        showTime: Double, clockOffsetMs: Double?, clockRttMs: Double?, policiesOk: Bool,
        batteryPct: Double? = nil, rssiDbm: Double? = nil, lastError: String? = nil
    ) {
        self.v = v
        self.duck = duck
        self.seq = seq
        self.state = state
        self.show = show
        self.showTime = showTime
        self.clockOffsetMs = clockOffsetMs
        self.clockRttMs = clockRttMs
        self.policiesOk = policiesOk
        self.batteryPct = batteryPct
        self.rssiDbm = rssiDbm
        self.lastError = lastError
    }
}

extension TelemetryMessage: Codable {
    enum CodingKeys: String, CodingKey {
        case v, type, duck, seq, state, show
        case showTime = "show_time"
        case clockOffsetMs = "clock_offset_ms"
        case clockRttMs = "clock_rtt_ms"
        case policiesOk = "policies_ok"
        case batteryPct = "battery_pct"
        case rssiDbm = "rssi_dbm"
        case lastError = "last_error"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        v = try c.decode(Int.self, forKey: .v)
        duck = try c.decode(DuckID.self, forKey: .duck)
        seq = try c.decode(Int.self, forKey: .seq)
        state = try c.decode(AgentState.self, forKey: .state)
        show = try c.decodeIfPresent(String.self, forKey: .show)
        showTime = try c.decode(Double.self, forKey: .showTime)
        clockOffsetMs = try c.decodeIfPresent(Double.self, forKey: .clockOffsetMs)
        clockRttMs = try c.decodeIfPresent(Double.self, forKey: .clockRttMs)
        policiesOk = try c.decode(Bool.self, forKey: .policiesOk)
        batteryPct = try c.decodeIfPresent(Double.self, forKey: .batteryPct)
        rssiDbm = try c.decodeIfPresent(Double.self, forKey: .rssiDbm)
        lastError = try c.decodeIfPresent(String.self, forKey: .lastError)
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(v, forKey: .v)
        try c.encode(Self.type, forKey: .type)
        try c.encode(duck, forKey: .duck)
        try c.encode(seq, forKey: .seq)
        try c.encode(state, forKey: .state)
        try c.encodeIfPresent(show, forKey: .show)
        try c.encode(showTime, forKey: .showTime)
        // Explicit `null` (not an omitted key) while unsynced, matching the
        // wire example in docs/swarmlink-protocol.md §4 and the Python
        // agent's `build_telemetry`, which always includes the key.
        if let clockOffsetMs { try c.encode(clockOffsetMs, forKey: .clockOffsetMs) }
        else { try c.encodeNil(forKey: .clockOffsetMs) }
        if let clockRttMs { try c.encode(clockRttMs, forKey: .clockRttMs) }
        else { try c.encodeNil(forKey: .clockRttMs) }
        try c.encode(policiesOk, forKey: .policiesOk)
        try c.encodeIfPresent(batteryPct, forKey: .batteryPct)
        try c.encodeIfPresent(rssiDbm, forKey: .rssiDbm)
        try c.encodeIfPresent(lastError, forKey: .lastError)
    }
}

// MARK: - Envelope

/// Any decoded wire message, tagged by which `type` it came from.
public enum Envelope: Sendable, Equatable {
    case timeRequest(TimeRequest)
    case timeResponse(TimeResponse)
    case state(StateMessage)
    case cmd(CommandMessage)
    case ack(AckMessage)
    case telemetry(TelemetryMessage)
}

/// Decodes and dispatches on `"type"`. Per docs/swarmlink-protocol.md §3:
/// "unknown `type` is dropped silently" — this returns `nil` rather than
/// throwing for anything unrecognized or malformed.
public enum SwarmMessage {
    private struct TypeProbe: Decodable { let type: String }

    public static func decode(_ data: Data) -> Envelope? {
        guard let probe = try? JSONDecoder().decode(TypeProbe.self, from: data) else { return nil }
        let decoder = JSONDecoder()
        switch probe.type {
        case TimeRequest.type:
            return (try? decoder.decode(TimeRequest.self, from: data)).map(Envelope.timeRequest)
        case TimeResponse.type:
            return (try? decoder.decode(TimeResponse.self, from: data)).map(Envelope.timeResponse)
        case StateMessage.type:
            return (try? decoder.decode(StateMessage.self, from: data)).map(Envelope.state)
        case CommandMessage.type:
            return (try? decoder.decode(CommandMessage.self, from: data)).map(Envelope.cmd)
        case AckMessage.type:
            return (try? decoder.decode(AckMessage.self, from: data)).map(Envelope.ack)
        case TelemetryMessage.type:
            return (try? decoder.decode(TelemetryMessage.self, from: data)).map(Envelope.telemetry)
        default:
            return nil
        }
    }

    public static func encode(_ envelope: Envelope) throws -> Data {
        let encoder = JSONEncoder()
        switch envelope {
        case .timeRequest(let m): return try encoder.encode(m)
        case .timeResponse(let m): return try encoder.encode(m)
        case .state(let m): return try encoder.encode(m)
        case .cmd(let m): return try encoder.encode(m)
        case .ack(let m): return try encoder.encode(m)
        case .telemetry(let m): return try encoder.encode(m)
        }
    }
}
