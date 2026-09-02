import XCTest
@testable import SwarmLink

/// The puppet datagram of docs/swarmlink-protocol.md §6: snake_case on the
/// wire, every field but `seq` optional, `do` as the skill key.
final class PuppetMessageTests: XCTestCase {
    private func jsonString(_ data: Data) -> String {
        String(data: data, encoding: .utf8) ?? ""
    }

    func testFullFrameRoundTripAndSnakeCase() throws {
        let original = PuppetFrame(
            seq: 1042, masterTime: 123_456_789,
            move: PuppetMove(vx: 0.1, vy: 0.0, vyaw: -0.5),
            head: PuppetHead(neckPitch: 0, headPitch: -0.2, headYaw: 0.3, headRoll: 0),
            pose: PuppetPose(z: -0.02, roll: 0, pitch: 0.1, active: true),
            mouth: PuppetMouth(open: 0.75),
            skill: "kick_left", sound: "chirp"
        )
        let data = try JSONEncoder().encode(original)
        let text = jsonString(data)
        XCTAssertTrue(text.contains("\"type\":\"puppet\""), text)
        XCTAssertTrue(text.contains("\"seq\":1042"), text)
        XCTAssertTrue(text.contains("\"master_time\":123456789"), text)
        XCTAssertTrue(text.contains("\"head_pitch\":-0.2"), text)
        XCTAssertTrue(text.contains("\"neck_pitch\":0"), text)
        XCTAssertTrue(text.contains("\"do\":\"kick_left\""), text)
        XCTAssertTrue(text.contains("\"sound\":\"chirp\""), text)
        XCTAssertTrue(text.contains("\"active\":true"), text)
        XCTAssertFalse(text.contains("skill"), "the wire key is `do`, never the Swift property name: \(text)")

        let decoded = try JSONDecoder().decode(PuppetFrame.self, from: data)
        XCTAssertEqual(decoded, original)
    }

    func testMinimalFrameCarriesOnlySeq() throws {
        let data = try JSONEncoder().encode(PuppetFrame(seq: 7))
        // Key order is not part of the contract; check the set.
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        XCTAssertEqual(Set(object.keys), ["v", "type", "seq"])

        let decoded = try JSONDecoder().decode(PuppetFrame.self, from: Data("{\"v\":1,\"type\":\"puppet\",\"seq\":5}".utf8))
        XCTAssertEqual(decoded.seq, 5)
        XCTAssertNil(decoded.masterTime)
        XCTAssertNil(decoded.move)
        XCTAssertNil(decoded.head)
        XCTAssertNil(decoded.pose)
        XCTAssertNil(decoded.mouth)
        XCTAssertNil(decoded.skill)
        XCTAssertNil(decoded.sound)
        XCTAssertFalse(decoded.hasEvent)
    }

    func testPartialSubObjectsDefaultToZeroAndUnknownFieldsAreIgnored() throws {
        let json = """
        {"v":1,"type":"puppet","seq":9,"move":{"vx":0.2},"pose":{"active":true},"mouth":{},"future":{"x":1},"do":"roulade"}
        """
        let decoded = try JSONDecoder().decode(PuppetFrame.self, from: Data(json.utf8))
        XCTAssertEqual(decoded.move, PuppetMove(vx: 0.2, vy: 0, vyaw: 0))
        XCTAssertEqual(decoded.pose, PuppetPose(z: 0, roll: 0, pitch: 0, active: true))
        XCTAssertEqual(decoded.mouth, PuppetMouth(open: 0))
        XCTAssertNil(decoded.head)
        XCTAssertEqual(decoded.skill, "roulade")
        XCTAssertTrue(decoded.hasEvent)
    }

    func testEnvelopeDispatchesPuppet() throws {
        let frame = PuppetFrame(seq: 3, move: PuppetMove(vx: 0.1))
        let data = try SwarmMessage.encode(.puppet(frame))
        guard case .puppet(let decoded)? = SwarmMessage.decode(data) else {
            return XCTFail("expected a puppet envelope")
        }
        XCTAssertEqual(decoded, frame)
        XCTAssertNil(SwarmMessage.decode(Data("{\"v\":1,\"type\":\"puppet\"}".utf8)), "seq is required")
    }
}
