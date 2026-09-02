// OSC.swift
//
// Hand-rolled OSC 1.0 codec for docs/osc-facade.md: address pattern, `,`
// type-tag string, 32-bit big-endian `i`/`f`, null-padded `s`, `b` blobs
// (parsed, ignored by the facade) and the payload-less `T`/`F` tags.
// The facade never *emits* bundles (every status message fits one
// datagram), but inbound `#bundle` datagrams are accepted by
// `decodePacket`: many rigs bundle by default (TouchDesigner's OSC Out,
// python-osc bundle builders, cue systems that bundle a fire with a fader),
// and a bundled `/duckswarm/panic` must reach the master like any other —
// panic always works. Elements are unwrapped in order, nesting is capped
// like StageWizard's parser, and the time tag is treated as "now".
//
// The encode/decode helpers are a port of the pure, `nonisolated static`
// codec in StageWizard's `OSCServer.swift` (same author, MIT —
// https://github.com/virtualmagician/StageWizard), extended with blobs
// and booleans and made *throwing*: a malformed datagram from a rig is
// reported as an error, never a crash and never a partial argument list.
// OSC arguments are not self-describing, so once a tag is unrecognised
// the width of every argument after it cannot be trusted.

import Foundation

/// One OSC argument. Only OSC 1.0 tags the facade doc lists are modelled.
public enum OSCArg: Sendable, Equatable {
    case int32(Int32)
    case float32(Float)
    case string(String)
    case blob(Data)
    case `true`
    case `false`

    /// The OSC 1.0 type tag character for this argument.
    public var typeTag: Character {
        switch self {
        case .int32: return "i"
        case .float32: return "f"
        case .string: return "s"
        case .blob: return "b"
        case .true: return "T"
        case .false: return "F"
        }
    }

    /// Numeric leniency (docs/osc-facade.md: "an `i` where an `f` is
    /// expected is accepted"): the value as a Double for `i`/`f`, else nil.
    public var numberValue: Double? {
        switch self {
        case .int32(let v): return Double(v)
        case .float32(let v): return Double(v)
        default: return nil
        }
    }

    /// The string payload of an `s` argument, else nil.
    public var stringValue: String? {
        if case .string(let v) = self { return v }
        return nil
    }

    /// Flag leniency: `T`/`F` are booleans, `i`/`f` non-zero is true.
    public var boolValue: Bool? {
        switch self {
        case .true: return true
        case .false: return false
        case .int32(let v): return v != 0
        case .float32(let v): return v != 0
        default: return nil
        }
    }
}

/// An OSC 1.0 message: an address pattern plus its typed arguments.
public struct OSCMessage: Sendable, Equatable {
    public var address: String
    public var args: [OSCArg]

    public init(address: String, args: [OSCArg] = []) {
        self.address = address
        self.args = args
    }

    /// The exact OSC 1.0 wire bytes for this message.
    public func encode() -> Data {
        OSCCodec.encode(self)
    }

    /// Parses one OSC 1.0 message datagram. Throws `OSCDecodeError` for
    /// anything malformed, truncated or unsupported (including a
    /// `#bundle`; see `OSCCodec.decodePacket` for those).
    public static func decode(_ data: Data) throws -> OSCMessage {
        try OSCCodec.decode(data)
    }
}

/// Why a datagram could not be decoded as an OSC 1.0 message.
public enum OSCDecodeError: Error, Sendable, Equatable, CustomStringConvertible {
    /// Zero-length datagram.
    case empty
    /// The datagram ended before the named field was complete.
    case truncated(String)
    /// The address pattern does not start with `/`.
    case invalidAddress
    /// No `,`-prefixed type-tag string after the address.
    case missingTypeTags
    /// A type tag this codec does not implement (OSC 1.0 subset only).
    case unsupportedTypeTag(Character)
    /// An OSC-string was not valid UTF-8.
    case invalidUTF8
    /// A blob's size prefix was negative.
    case invalidBlobSize(Int32)
    /// A `#bundle` where a single message was expected (`decode`); use
    /// `decodePacket` to unwrap bundles.
    case bundle
    /// Bundles nested deeper than `OSCCodec.maxBundleDepth`.
    case bundleNestedTooDeep

    public var description: String {
        switch self {
        case .empty: return "empty datagram"
        case .truncated(let what): return "truncated \(what)"
        case .invalidAddress: return "address pattern must start with '/'"
        case .missingTypeTags: return "missing ',' type-tag string"
        case .unsupportedTypeTag(let tag): return "unsupported type tag '\(tag)'"
        case .invalidUTF8: return "OSC-string is not valid UTF-8"
        case .invalidBlobSize(let size): return "invalid blob size \(size)"
        case .bundle: return "#bundle where a single message was expected"
        case .bundleNestedTooDeep: return "#bundle nested deeper than \(OSCCodec.maxBundleDepth) levels"
        }
    }
}

/// Pure OSC 1.0 encoding/decoding. Every function is `nonisolated static`
/// — no actor hop is needed to call it from a Network.framework receive
/// callback, and it is unit-tested against hand-assembled byte arrays
/// (OSCCodecTests).
public enum OSCCodec {
    // MARK: Encoding

    public nonisolated static func encode(_ message: OSCMessage) -> Data {
        var data = encodeString(message.address)
        var tags = ","
        for arg in message.args { tags.append(arg.typeTag) }
        data.append(encodeString(tags))
        for arg in message.args {
            switch arg {
            case .int32(let value): data.append(encodeUInt32(UInt32(bitPattern: value)))
            case .float32(let value): data.append(encodeUInt32(value.bitPattern))
            case .string(let value): data.append(encodeString(value))
            case .blob(let value): data.append(encodeBlob(value))
            case .true, .false: break // tag only, no payload
            }
        }
        return data
    }

    /// UTF-8 bytes, one null terminator, then nulls to a 4-byte boundary.
    nonisolated private static func encodeString(_ value: String) -> Data {
        var data = Data(value.utf8)
        data.append(0)
        while data.count % 4 != 0 { data.append(0) }
        return data
    }

    nonisolated private static func encodeUInt32(_ bits: UInt32) -> Data {
        Data([UInt8((bits >> 24) & 0xFF), UInt8((bits >> 16) & 0xFF), UInt8((bits >> 8) & 0xFF), UInt8(bits & 0xFF)])
    }

    /// int32 byte count, the bytes, then nulls to a 4-byte boundary.
    nonisolated private static func encodeBlob(_ value: Data) -> Data {
        var data = encodeUInt32(UInt32(truncatingIfNeeded: value.count))
        data.append(value)
        while data.count % 4 != 0 { data.append(0) }
        return data
    }

    // MARK: Decoding

    nonisolated private static let bundleTag = Data("#bundle\0".utf8)

    /// Recursion cap for nested `#bundle` elements (StageWizard uses the
    /// same): a crafted datagram must not be able to blow the stack.
    public nonisolated static let maxBundleDepth = 8

    /// Parses one datagram as it arrives on the wire: a single message, or
    /// a `#bundle` whose elements — messages and nested bundles — are
    /// unwrapped in order. The 64-bit time tag is treated as "immediately"
    /// (the facade fires everything as it arrives, like StageWizard). One
    /// malformed element rejects the whole packet, consistent with
    /// `decode`: never a partial result.
    public nonisolated static func decodePacket(_ data: Data) throws -> [OSCMessage] {
        guard !data.isEmpty else { throw OSCDecodeError.empty }
        guard data.starts(with: bundleTag) else { return [try decode(data)] }
        return try decodeBundle(data, depth: 0)
    }

    /// `#bundle\0` (8 bytes) + time tag (8 bytes), then int32-size-prefixed
    /// elements, each a message or a nested bundle.
    nonisolated private static func decodeBundle(_ data: Data, depth: Int) throws -> [OSCMessage] {
        guard depth < maxBundleDepth else { throw OSCDecodeError.bundleNestedTooDeep }
        let headerSize = 16
        guard data.count >= headerSize else { throw OSCDecodeError.truncated("bundle header") }
        var cursor = data.startIndex + headerSize
        var messages: [OSCMessage] = []
        while cursor < data.endIndex {
            let (sizeBits, afterSize) = try readUInt32(data, at: cursor, field: "bundle element size")
            let size = Int32(bitPattern: sizeBits)
            guard size >= 0, let elementEnd = data.index(afterSize, offsetBy: Int(size), limitedBy: data.endIndex) else {
                throw OSCDecodeError.truncated("bundle element")
            }
            let element = Data(data[afterSize..<elementEnd])
            if element.starts(with: bundleTag) {
                messages.append(contentsOf: try decodeBundle(element, depth: depth + 1))
            } else {
                messages.append(try decode(element))
            }
            cursor = elementEnd
        }
        return messages
    }

    public nonisolated static func decode(_ data: Data) throws -> OSCMessage {
        guard !data.isEmpty else { throw OSCDecodeError.empty }
        if data.starts(with: bundleTag) { throw OSCDecodeError.bundle }

        let (address, afterAddress) = try readString(data, at: data.startIndex, field: "address")
        guard address.hasPrefix("/") else { throw OSCDecodeError.invalidAddress }
        guard afterAddress < data.endIndex else { throw OSCDecodeError.missingTypeTags }
        let (typeTags, afterTags) = try readString(data, at: afterAddress, field: "type tags")
        guard typeTags.hasPrefix(",") else { throw OSCDecodeError.missingTypeTags }

        var args: [OSCArg] = []
        var cursor = afterTags
        for tag in typeTags.dropFirst() {
            switch tag {
            case "i":
                let (bits, next) = try readUInt32(data, at: cursor, field: "int32")
                args.append(.int32(Int32(bitPattern: bits)))
                cursor = next
            case "f":
                let (bits, next) = try readUInt32(data, at: cursor, field: "float32")
                args.append(.float32(Float(bitPattern: bits)))
                cursor = next
            case "s":
                let (value, next) = try readString(data, at: cursor, field: "string")
                args.append(.string(value))
                cursor = next
            case "b":
                let (value, next) = try readBlob(data, at: cursor)
                args.append(.blob(value))
                cursor = next
            case "T":
                args.append(.true)
            case "F":
                args.append(.false)
            default:
                throw OSCDecodeError.unsupportedTypeTag(tag)
            }
        }
        return OSCMessage(address: address, args: args)
    }

    /// An OSC-string: bytes up to (not including) the first null, followed
    /// by 1–4 nulls so the consumed length is a multiple of 4.
    nonisolated private static func readString(_ data: Data, at index: Data.Index, field: String) throws -> (String, Data.Index) {
        guard index < data.endIndex, let nullIndex = data[index...].firstIndex(of: 0) else {
            throw OSCDecodeError.truncated(field)
        }
        guard let string = String(bytes: data[index..<nullIndex], encoding: .utf8) else {
            throw OSCDecodeError.invalidUTF8
        }
        let consumed = nullIndex - index + 1
        let padded = ((consumed + 3) / 4) * 4
        let next = index + padded
        guard next <= data.endIndex else { throw OSCDecodeError.truncated(field) }
        return (string, next)
    }

    /// Big-endian 4-byte word (int32 two's complement or IEEE-754 float
    /// bit pattern — network order either way).
    nonisolated private static func readUInt32(_ data: Data, at index: Data.Index, field: String) throws -> (UInt32, Data.Index) {
        guard index <= data.endIndex, let next = data.index(index, offsetBy: 4, limitedBy: data.endIndex) else {
            throw OSCDecodeError.truncated(field)
        }
        let bits = data[index..<next].reduce(UInt32(0)) { ($0 << 8) | UInt32($1) }
        return (bits, next)
    }

    /// An OSC-blob: int32 size, that many bytes, then nulls to a 4-byte
    /// boundary.
    nonisolated private static func readBlob(_ data: Data, at index: Data.Index) throws -> (Data, Data.Index) {
        let (sizeBits, afterSize) = try readUInt32(data, at: index, field: "blob size")
        let size = Int32(bitPattern: sizeBits)
        guard size >= 0 else { throw OSCDecodeError.invalidBlobSize(size) }
        guard let end = data.index(afterSize, offsetBy: Int(size), limitedBy: data.endIndex) else {
            throw OSCDecodeError.truncated("blob")
        }
        let padded = ((Int(size) + 3) / 4) * 4
        guard let next = data.index(afterSize, offsetBy: padded, limitedBy: data.endIndex) else {
            throw OSCDecodeError.truncated("blob padding")
        }
        return (Data(data[afterSize..<end]), next)
    }
}
