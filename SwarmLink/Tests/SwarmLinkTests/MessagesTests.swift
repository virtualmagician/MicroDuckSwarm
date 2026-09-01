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
