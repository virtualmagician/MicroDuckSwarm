import XCTest
@testable import SwarmLink

/// The OSC 1.0 codec against hand-assembled byte vectors (exact wire
/// bytes, both directions), round trips for every argument type, and
/// malformed/truncated input that must throw — never crash.
final class OSCCodecTests: XCTestCase {
    private func oscString(_ s: String) -> [UInt8] {
        var bytes = Array(s.utf8) + [0]
        while bytes.count % 4 != 0 { bytes.append(0) }
        return bytes
    }

    // MARK: exact wire bytes

    func testPlayWithFloatLeadIsExactOSC10Bytes() throws {
        let data = OSCMessage(address: "/duckswarm/play", args: [.float32(1.5)]).encode()
        // "/duckswarm/play" is 15 bytes + NUL = 16 (already 4-aligned),
        // ",f" + NUL padded to 4, then IEEE-754 1.5 big-endian.
        let expected: [UInt8] = Array("/duckswarm/play".utf8) + [0]
            + Array(",f".utf8) + [0, 0]
            + [0x3F, 0xC0, 0x00, 0x00]
        XCTAssertEqual([UInt8](data), expected)
        XCTAssertEqual(data.count, 24)
        XCTAssertEqual(try OSCMessage.decode(Data(expected)), OSCMessage(address: "/duckswarm/play", args: [.float32(1.5)]))
    }

    func testLoadWithStringArgPadsToFourBytes() throws {
        let data = OSCMessage(address: "/duckswarm/load", args: [.string("demo")]).encode()
        let expected = oscString("/duckswarm/load") + oscString(",s") + Array("demo".utf8) + [0, 0, 0, 0]
        XCTAssertEqual([UInt8](data), expected)
        XCTAssertEqual(data.count, 28)
        XCTAssertEqual(try OSCMessage.decode(data), OSCMessage(address: "/duckswarm/load", args: [.string("demo")]))
    }

    func testSummaryWithThreeIntsAndAddressPadding() throws {
        let data = OSCMessage(address: "/duckswarm/status/summary", args: [.int32(2), .int32(1), .int32(-1)]).encode()
        // 25-char address + NUL = 26 → 28; ",iii" + NUL = 5 → 8; 3 × 4.
        let expected = oscString("/duckswarm/status/summary") + oscString(",iii")
            + [0, 0, 0, 2] + [0, 0, 0, 1] + [0xFF, 0xFF, 0xFF, 0xFF]
        XCTAssertEqual([UInt8](data), expected)
        XCTAssertEqual(data.count, 48)
        XCTAssertEqual(try OSCMessage.decode(data).args, [.int32(2), .int32(1), .int32(-1)])
    }

    func testNoArgsStillCarriesCommaTypeTagString() throws {
        let data = OSCMessage(address: "/duckswarm/go").encode()
        XCTAssertEqual([UInt8](data), oscString("/duckswarm/go") + [0x2C, 0, 0, 0])
        XCTAssertEqual(data.count, 20)
        XCTAssertEqual(try OSCMessage.decode(data), OSCMessage(address: "/duckswarm/go"))
    }

    func testBooleansCarryNoPayload() throws {
        let data = OSCMessage(address: "/x", args: [.true, .false, .int32(7)]).encode()
        XCTAssertEqual([UInt8](data), oscString("/x") + oscString(",TFi") + [0, 0, 0, 7])
        XCTAssertEqual(data.count, 16)
        XCTAssertEqual(try OSCMessage.decode(data).args, [.true, .false, .int32(7)])
    }

    func testBlobIsSizePrefixedAndPadded() throws {
        let payload = Data([1, 2, 3, 4, 5])
        let data = OSCMessage(address: "/b", args: [.blob(payload), .int32(9)]).encode()
        XCTAssertEqual([UInt8](data), oscString("/b") + oscString(",bi") + [0, 0, 0, 5] + [1, 2, 3, 4, 5, 0, 0, 0] + [0, 0, 0, 9])
        XCTAssertEqual(try OSCMessage.decode(data).args, [.blob(payload), .int32(9)])
        // An exactly-aligned blob gets no padding.
        let aligned = OSCMessage(address: "/b", args: [.blob(Data([1, 2, 3, 4]))]).encode()
        XCTAssertEqual([UInt8](aligned), oscString("/b") + oscString(",b") + [0, 0, 0, 4] + [1, 2, 3, 4])
        XCTAssertEqual(try OSCMessage.decode(OSCMessage(address: "/b", args: [.blob(Data())]).encode()).args, [.blob(Data())])
    }

    // MARK: round trips

    func testRoundTripEveryArgType() throws {
        let message = OSCMessage(address: "/duckswarm/status/duck", args: [
            .string("duck-01"), .string(""), .string("playing"),
            .float32(12.375), .float32(-1.0), .int32(1),
            .blob(Data([0xDE, 0xAD, 0xBE, 0xEF, 0x00])), .true, .false, .int32(Int32.min)
        ])
        XCTAssertEqual(try OSCMessage.decode(message.encode()), message)
    }

    func testRoundTripStringPaddingForEveryLengthMod4() throws {
        for length in 0..<9 {
            let text = String(repeating: "a", count: length)
            let message = OSCMessage(address: "/s", args: [.string(text), .int32(Int32(length))])
            let data = message.encode()
            XCTAssertEqual(data.count % 4, 0, "length \(length)")
            XCTAssertEqual(try OSCMessage.decode(data), message, "length \(length)")
        }
    }

    func testRoundTripUnicodeStrings() throws {
        let message = OSCMessage(address: "/duckswarm/error", args: [.string("Ente 🦆 — Ökonomie"), .string("日本語")])
        XCTAssertEqual(try OSCMessage.decode(message.encode()), message)
    }

    func testRoundTripIntegerEdges() throws {
        let message = OSCMessage(address: "/i", args: [.int32(0), .int32(-1), .int32(Int32.max), .int32(Int32.min), .int32(1)])
        XCTAssertEqual(try OSCMessage.decode(message.encode()), message)
    }

    func testRoundTripFloatEdgesPreserveBitPatterns() throws {
        let values: [Float] = [0, -0.0, 1.5, -2.25, .greatestFiniteMagnitude, .leastNonzeroMagnitude, .infinity, -.infinity, .nan]
        let message = OSCMessage(address: "/f", args: values.map { .float32($0) })
        let decoded = try OSCMessage.decode(message.encode())
        XCTAssertEqual(decoded.args.count, values.count)
        for (arg, expected) in zip(decoded.args, values) {
            guard case .float32(let value) = arg else { return XCTFail("\(arg)") }
            XCTAssertEqual(value.bitPattern, expected.bitPattern)
        }
    }

    // MARK: malformed input throws, never crashes

    func testEmptyDatagramThrows() {
        XCTAssertThrowsError(try OSCMessage.decode(Data())) { error in
            XCTAssertEqual(error as? OSCDecodeError, .empty)
        }
    }

    func testEveryStrictPrefixOfValidMessagesThrows() {
        let messages: [OSCMessage] = [
            OSCMessage(address: "/duckswarm/go"),
            OSCMessage(address: "/duckswarm/play", args: [.float32(1.5)]),
            OSCMessage(address: "/duckswarm/load", args: [.string("demo")]),
            OSCMessage(address: "/x", args: [.true, .false, .int32(7)]),
            OSCMessage(address: "/b", args: [.blob(Data([1, 2, 3, 4, 5])), .string("tail")]),
            OSCMessage(address: "/duckswarm/status/summary", args: [.int32(2), .int32(1), .int32(1)])
        ]
        for message in messages {
            let data = message.encode()
            XCTAssertNoThrow(try OSCMessage.decode(data))
            for length in 0..<data.count {
                XCTAssertThrowsError(try OSCMessage.decode(data.prefix(length)), "\(message.address) truncated to \(length) bytes must throw")
            }
        }
    }

    func testAddressWithoutLeadingSlashThrows() {
        let data = Data(oscString("duckswarm") + oscString(","))
        XCTAssertThrowsError(try OSCMessage.decode(data)) { error in
            XCTAssertEqual(error as? OSCDecodeError, .invalidAddress)
        }
    }

    func testMissingTypeTagStringThrows() {
        XCTAssertThrowsError(try OSCMessage.decode(Data(oscString("/duckswarm/go")))) { error in
            XCTAssertEqual(error as? OSCDecodeError, .missingTypeTags)
        }
        // Something that is not a `,`-string where the tags belong.
        XCTAssertThrowsError(try OSCMessage.decode(Data(oscString("/x") + oscString("s") + oscString("demo")))) { error in
            XCTAssertEqual(error as? OSCDecodeError, .missingTypeTags)
        }
    }

    func testUnsupportedTypeTagThrowsWholeMessage() {
        // OSC 1.1 `d` (float64): width unknown to an OSC 1.0 parser, so the
        // whole message is rejected rather than a partial argument list.
        let data = Data(oscString("/x") + oscString(",id") + [0, 0, 0, 1] + [UInt8](repeating: 0, count: 8))
        XCTAssertThrowsError(try OSCMessage.decode(data)) { error in
            XCTAssertEqual(error as? OSCDecodeError, .unsupportedTypeTag("d"))
        }
    }

    func testInvalidUTF8Throws() {
        let data = Data([0x2F, 0xFF, 0xFE, 0x00] + oscString(","))
        XCTAssertThrowsError(try OSCMessage.decode(data)) { error in
            XCTAssertEqual(error as? OSCDecodeError, .invalidUTF8)
        }
    }

    func testNegativeAndOversizedBlobSizesThrow() {
        let negative = Data(oscString("/b") + oscString(",b") + [0xFF, 0xFF, 0xFF, 0xFF])
        XCTAssertThrowsError(try OSCMessage.decode(negative)) { error in
            XCTAssertEqual(error as? OSCDecodeError, .invalidBlobSize(-1))
        }
        let oversized = Data(oscString("/b") + oscString(",b") + [0x7F, 0xFF, 0xFF, 0xFF] + [1, 2, 3, 4])
        XCTAssertThrowsError(try OSCMessage.decode(oversized)) { error in
            XCTAssertEqual(error as? OSCDecodeError, .truncated("blob"))
        }
    }

    // MARK: bundles (inbound only — the facade never emits them)

    /// `#bundle\0`, the "immediately" time tag, then int32-size-prefixed
    /// elements — what TouchDesigner, python-osc bundle builders and
    /// bundling cue systems put on the wire.
    private func bundle(_ elements: [Data]) -> Data {
        var data = Data("#bundle\0".utf8) + Data([0, 0, 0, 0, 0, 0, 0, 1])
        for element in elements {
            let size = UInt32(element.count)
            data += Data([UInt8(size >> 24), UInt8((size >> 16) & 0xFF), UInt8((size >> 8) & 0xFF), UInt8(size & 0xFF)])
            data += element
        }
        return data
    }

    func testSingleMessageDecodeRejectsBundlesButDecodePacketUnwrapsThemInOrder() throws {
        let panic = OSCMessage(address: "/duckswarm/panic")
        let load = OSCMessage(address: "/duckswarm/load", args: [.string("demo")])
        let seek = OSCMessage(address: "/duckswarm/seek", args: [.float32(30)])
        let packet = bundle([panic.encode(), bundle([load.encode(), seek.encode()]), load.encode()])

        XCTAssertThrowsError(try OSCMessage.decode(packet)) { error in
            XCTAssertEqual(error as? OSCDecodeError, .bundle)
        }
        XCTAssertEqual(try OSCCodec.decodePacket(packet), [panic, load, seek, load], "elements in order, nested bundles flattened")
        XCTAssertEqual(try OSCCodec.decodePacket(panic.encode()), [panic], "a plain message is a one-element packet")
        XCTAssertEqual(try OSCCodec.decodePacket(bundle([])), [], "an empty bundle carries nothing")
        XCTAssertThrowsError(try OSCCodec.decodePacket(Data())) { error in
            XCTAssertEqual(error as? OSCDecodeError, .empty)
        }
    }

    func testMalformedBundlesThrowWholePacket() {
        let panic = OSCMessage(address: "/duckswarm/panic").encode()
        let valid = bundle([panic])
        XCTAssertNoThrow(try OSCCodec.decodePacket(valid))

        // Cut anywhere inside the header, a size prefix or an element.
        for length in 1..<valid.count where length != 16 {
            XCTAssertThrowsError(try OSCCodec.decodePacket(valid.prefix(length)), "bundle truncated to \(length) bytes must throw")
        }
        XCTAssertEqual(try OSCCodec.decodePacket(valid.prefix(16)), [], "header + time tag alone is an empty bundle")

        // A negative element size.
        let negative = Data("#bundle\0".utf8) + Data(repeating: 0, count: 8) + Data([0xFF, 0xFF, 0xFF, 0xFF]) + panic
        XCTAssertThrowsError(try OSCCodec.decodePacket(negative)) { error in
            XCTAssertEqual(error as? OSCDecodeError, .truncated("bundle element"))
        }

        // One bad element (unsupported tag) rejects the whole packet — no partial dispatch.
        let bad = Data(oscString("/duckswarm/panic") + oscString(",d") + [UInt8](repeating: 0, count: 8))
        XCTAssertThrowsError(try OSCCodec.decodePacket(bundle([panic, bad]))) { error in
            XCTAssertEqual(error as? OSCDecodeError, .unsupportedTypeTag("d"))
        }

        // Nesting is capped like StageWizard's parser.
        var nested = panic
        for _ in 0..<OSCCodec.maxBundleDepth { nested = bundle([nested]) }
        XCTAssertEqual(try OSCCodec.decodePacket(nested).count, 1, "\(OSCCodec.maxBundleDepth) levels are fine")
        XCTAssertThrowsError(try OSCCodec.decodePacket(bundle([nested]))) { error in
            XCTAssertEqual(error as? OSCDecodeError, .bundleNestedTooDeep)
        }
    }

    func testRandomGarbageNeverCrashes() {
        // Deterministic LCG so a failure is reproducible.
        var state: UInt64 = 0x5EED_0DD5_1234_ABCD
        func next() -> UInt8 {
            state = state &* 6364136223846793005 &+ 1442695040888963407
            return UInt8(truncatingIfNeeded: state >> 56)
        }
        var decodedCount = 0
        for round in 0..<2000 {
            let length = round % 64
            var bytes = (0..<length).map { _ in next() }
            // Bias some rounds towards plausible headers so the argument
            // readers are exercised, not just the address check.
            if round % 3 == 0, bytes.count >= 8 {
                bytes.replaceSubrange(0..<4, with: [0x2F, 0x78, 0, 0])   // "/x\0\0"
                bytes.replaceSubrange(4..<8, with: [0x2C, 0x73, 0x62, 0]) // ",sb\0"
            }
            if let message = try? OSCMessage.decode(Data(bytes)) {
                decodedCount += 1
                XCTAssertTrue(message.address.hasPrefix("/"))
            }
        }
        XCTAssertLessThan(decodedCount, 2000)
    }

    // MARK: leniency helpers used by the facade

    func testNumberStringAndBoolAccessors() {
        XCTAssertEqual(OSCArg.int32(3).numberValue, 3.0)
        XCTAssertEqual(OSCArg.float32(1.5).numberValue, 1.5)
        XCTAssertNil(OSCArg.string("1.5").numberValue)
        XCTAssertNil(OSCArg.true.numberValue)
        XCTAssertEqual(OSCArg.string("demo").stringValue, "demo")
        XCTAssertNil(OSCArg.int32(1).stringValue)
        XCTAssertEqual(OSCArg.true.boolValue, true)
        XCTAssertEqual(OSCArg.false.boolValue, false)
        XCTAssertEqual(OSCArg.int32(2).boolValue, true)
        XCTAssertEqual(OSCArg.float32(0).boolValue, false)
        XCTAssertNil(OSCArg.string("yes").boolValue)
        XCTAssertEqual(OSCArg.blob(Data()).typeTag, "b")
    }
}
