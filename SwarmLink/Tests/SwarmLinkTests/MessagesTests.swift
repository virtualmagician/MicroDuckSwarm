import XCTest
@testable import SwarmLink

final class MessagesTests: XCTestCase {
    private func jsonString(_ data: Data) -> String {
        String(data: data, encoding: .utf8) ?? ""
    }

    func testTimeRequestRoundTripAndSnakeCase() throws {
        let original = TimeRequest(duck: "duck-01", t0: 123)
        let data = try JSONEncoder().encode(original)
        let text = jsonString(data)
        XCTAssertTrue(text.contains("\"type\":\"time_req\""))
        XCTAssertTrue(text.contains("\"t0\":123"))
        XCTAssertTrue(text.contains("\"duck\":\"duck-01\""))

        let decoded = try JSONDecoder().decode(TimeRequest.self, from: data)
        XCTAssertEqual(decoded, original)
    }

    func testTimeResponseRoundTrip() throws {
        let original = TimeResponse(t0: 1, t1: 2, t2: 3)
        let data = try JSONEncoder().encode(original)
        XCTAssertTrue(jsonString(data).contains("\"type\":\"time_resp\""))
        let decoded = try JSONDecoder().decode(TimeResponse.self, from: data)
        XCTAssertEqual(decoded, original)
    }

    func testStateMessageRoundTripAndSnakeCase() throws {
        let original = StateMessage(seq: 421, show: "demo", transport: .playing, showTime: 12.48, masterTime: 999)
        let data = try JSONEncoder().encode(original)
        let text = jsonString(data)
        XCTAssertTrue(text.contains("\"show_time\":12.48"))
        XCTAssertTrue(text.contains("\"master_time\":999"))
        XCTAssertTrue(text.contains("\"transport\":\"playing\""))

        let decoded = try JSONDecoder().decode(StateMessage.self, from: data)
        XCTAssertEqual(decoded, original)
    }

    func testLoadCommandRoundTripAndSnakeCase() throws {
        let original = CommandMessage(cmdID: "uuid-1", payload: .load(show: "demo", sha256: "abc123", role: "lead"))
        let data = try JSONEncoder().encode(original)
        let text = jsonString(data)
        XCTAssertTrue(text.contains("\"cmd_id\":\"uuid-1\""))
        XCTAssertTrue(text.contains("\"cmd\":\"load\""))
        XCTAssertTrue(text.contains("\"sha256\":\"abc123\""))
        XCTAssertTrue(text.contains("\"role\":\"lead\""))

        let decoded = try JSONDecoder().decode(CommandMessage.self, from: data)
        XCTAssertEqual(decoded, original)
    }

    func testPlayCommandRoundTripAndSnakeCase() throws {
        let original = CommandMessage(
            cmdID: "uuid-2",
            payload: .play(show: "demo", atMasterTime: 5_000_000_000, fromShowTime: 0.0)
        )
        let data = try JSONEncoder().encode(original)
        let text = jsonString(data)
        XCTAssertTrue(text.contains("\"at_master_time\":5000000000"))
        XCTAssertTrue(text.contains("\"from_show_time\":0"))

        let decoded = try JSONDecoder().decode(CommandMessage.self, from: data)
        XCTAssertEqual(decoded, original)
    }

    func testSeekCommandRoundTripAndSnakeCase() throws {
        let original = CommandMessage(cmdID: "uuid-3", payload: .seek(showTime: 45.0, atMasterTime: 42))
        let data = try JSONEncoder().encode(original)
        let text = jsonString(data)
        XCTAssertTrue(text.contains("\"show_time\":45"))
        XCTAssertTrue(text.contains("\"at_master_time\":42"))
        // seek must not carry a "show" field per docs/swarmlink-protocol.md's cmd table.
        XCTAssertFalse(text.contains("\"show\":"))

        let decoded = try JSONDecoder().decode(CommandMessage.self, from: data)
        XCTAssertEqual(decoded, original)
    }

    func testStopAndPanicCommandsCarryNoExtraFields() throws {
        for payload: CommandMessage.Payload in [.stop, .panic] {
            let original = CommandMessage(cmdID: "uuid-4", payload: payload)
            let data = try JSONEncoder().encode(original)
            let decoded = try JSONDecoder().decode(CommandMessage.self, from: data)
            XCTAssertEqual(decoded, original)
        }
    }

    func testRelaxCommandRoundTripsAndDefaultsToOn() throws {
        for on in [true, false] {
            let original = CommandMessage(cmdID: "uuid-relax", payload: .relax(on: on))
            let data = try JSONEncoder().encode(original)
            XCTAssertTrue(jsonString(data).contains("\"cmd\":\"relax\""))
            XCTAssertEqual(try JSONDecoder().decode(CommandMessage.self, from: data), original)
        }
        // A console (or an older master) that sends a bare `relax` means the
        // useful half: make the duck handleable. Same default as the agent.
        let bare = Data(#"{"v":1,"type":"cmd","cmd_id":"uuid-relax","cmd":"relax"}"#.utf8)
        let decoded = try JSONDecoder().decode(CommandMessage.self, from: bare)
        XCTAssertEqual(decoded.payload, .relax(on: true))
    }

    func testAckRoundTripAndSnakeCase() throws {
        let original = AckMessage(duck: "duck-02", cmdID: "uuid-5", ok: false, error: "bad_sha256")
        let data = try JSONEncoder().encode(original)
        let text = jsonString(data)
        XCTAssertTrue(text.contains("\"cmd_id\":\"uuid-5\""))
        XCTAssertTrue(text.contains("\"ok\":false"))
        XCTAssertTrue(text.contains("\"error\":\"bad_sha256\""))

        let decoded = try JSONDecoder().decode(AckMessage.self, from: data)
        XCTAssertEqual(decoded, original)
    }

    func testTelemetryRoundTripAndSnakeCase() throws {
        let original = TelemetryMessage(
            duck: "duck-03", seq: 88, state: .playing, show: "demo", showTime: 12.5,
            clockOffsetMs: 1.8, clockRttMs: 4.2, policiesOk: true,
            batteryPct: nil, rssiDbm: nil, lastError: nil
        )
        let data = try JSONEncoder().encode(original)
        let text = jsonString(data)
        XCTAssertTrue(text.contains("\"clock_offset_ms\":1.8"))
        XCTAssertTrue(text.contains("\"clock_rtt_ms\":4.2"))
        XCTAssertTrue(text.contains("\"policies_ok\":true"))
        XCTAssertTrue(text.contains("\"show_time\":12.5"))

        let decoded = try JSONDecoder().decode(TelemetryMessage.self, from: data)
        XCTAssertEqual(decoded, original)
    }

    /// docs/swarmlink-protocol.md §4: `clock_offset_ms`/`clock_rtt_ms` are
    /// null until the first successful time-sync exchange — a datagram
    /// sent before that point (e.g. the Python duck-agent pre-sync) must
    /// still decode, not be silently dropped.
    func testTelemetryWithNullClockFieldsDecodesAndRoundTrips() throws {
        let json = """
        {"v":1,"type":"telemetry","duck":"duck-03","seq":1,"state":"idle","show":null,
         "show_time":0.0,"clock_offset_ms":null,"clock_rtt_ms":null,"policies_ok":false,
         "battery_pct":null,"rssi_dbm":null,"last_error":null}
        """
        let decoded = try JSONDecoder().decode(TelemetryMessage.self, from: Data(json.utf8))
        XCTAssertNil(decoded.clockOffsetMs)
        XCTAssertNil(decoded.clockRttMs)
        XCTAssertNil(decoded.show)

        let data = try JSONEncoder().encode(decoded)
        let text = jsonString(data)
        XCTAssertTrue(text.contains("\"clock_offset_ms\":null"), text)
        XCTAssertTrue(text.contains("\"clock_rtt_ms\":null"), text)

        let roundTripped = try JSONDecoder().decode(TelemetryMessage.self, from: data)
        XCTAssertEqual(roundTripped, decoded)
    }

    /// docs/swarmlink-protocol.md §6: "Telemetry adds `puppet: true|false`"
    /// — the Python agent's `build_telemetry` always sends it; the master
    /// must keep it (a duck under a forgotten puppet sender is otherwise
    /// indistinguishable from one on the timeline) and decode a datagram
    /// without it as `false`.
    func testTelemetryPuppetFlagDecodesFromAPythonShapedDatagramAndDefaultsToFalse() throws {
        let live = """
        {"v": 1, "type": "telemetry", "duck": "duck-03", "seq": 12, "state": "playing", "show": "demo",
         "show_time": 4.2, "clock_offset_ms": 0.7, "clock_rtt_ms": 3.1, "policies_ok": true,
         "battery_pct": null, "rssi_dbm": null, "last_error": null, "puppet": true}
        """
        let decoded = try JSONDecoder().decode(TelemetryMessage.self, from: Data(live.utf8))
        XCTAssertTrue(decoded.puppet)
        XCTAssertEqual(decoded.state, .playing)
        guard case .telemetry(let viaEnvelope)? = SwarmMessage.decode(Data(live.utf8)) else {
            return XCTFail("expected a telemetry envelope")
        }
        XCTAssertTrue(viaEnvelope.puppet, "the flag survives the envelope path SwarmMaster ingests through")

        let quiet = live.replacingOccurrences(of: "\"puppet\": true", with: "\"puppet\": false")
        XCTAssertFalse(try JSONDecoder().decode(TelemetryMessage.self, from: Data(quiet.utf8)).puppet)

        let legacy = """
        {"v": 1, "type": "telemetry", "duck": "duck-03", "seq": 1, "state": "idle", "show": null,
         "show_time": 0.0, "clock_offset_ms": null, "clock_rtt_ms": null, "policies_ok": true}
        """
        XCTAssertFalse(try JSONDecoder().decode(TelemetryMessage.self, from: Data(legacy.utf8)).puppet,
                       "an agent without the puppet channel omits the key: not under a stream")

        let encoded = jsonString(try JSONEncoder().encode(decoded))
        XCTAssertTrue(encoded.contains("\"puppet\":true"), encoded)
        let roundTripped = try JSONDecoder().decode(TelemetryMessage.self, from: Data(encoded.utf8))
        XCTAssertEqual(roundTripped, decoded)
        XCTAssertTrue(jsonString(try JSONEncoder().encode(TelemetryMessage(
            duck: "duck-01", seq: 1, state: .idle, show: nil, showTime: 0, clockOffsetMs: nil, clockRttMs: nil, policiesOk: true
        ))).contains("\"puppet\":false"), "always on the wire, like the Python agent")
    }

    func testEnvelopeDispatchesOnType() throws {
        let ack = AckMessage(duck: "duck-01", cmdID: "uuid-6", ok: true, error: nil)
        let data = try JSONEncoder().encode(ack)
        guard case .ack(let decoded) = SwarmMessage.decode(data) else {
            return XCTFail("expected .ack envelope")
        }
        XCTAssertEqual(decoded, ack)
    }

    func testEnvelopeDropsUnknownTypeSilently() {
        let data = Data(#"{"v":1,"type":"from_the_future","duck":"duck-01"}"#.utf8)
        XCTAssertNil(SwarmMessage.decode(data))
    }

    func testEnvelopeDropsMalformedJSONSilently() {
        let data = Data("not json at all".utf8)
        XCTAssertNil(SwarmMessage.decode(data))
    }
}
