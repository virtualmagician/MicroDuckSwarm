// Roster.swift
//
// Physical duck roster: which duck-id lives at which host/port and which
// show-cast role it is currently assigned to. Kept out of the .duckshow
// file on purpose — "shows reference cast roles, never physical ducks"
// (docs/duckshow-format.md).

import Foundation

/// Identifies one physical duck. Stable across shows; roles are assigned
/// per-roster-entry, not baked into the id.
public struct DuckID: Hashable, Sendable, Codable, Comparable, ExpressibleByStringLiteral, CustomStringConvertible {
    public let raw: String

    public init(_ raw: String) {
        self.raw = raw
    }

    public init(stringLiteral value: String) {
        self.raw = value
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        raw = try c.decode(String.self)
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        try c.encode(raw)
    }

    public var description: String { raw }

    public static func < (lhs: DuckID, rhs: DuckID) -> Bool { lhs.raw < rhs.raw }
}

/// One line of the roster file: `[{"id": "duck-01", "host": "192.168.4.21", "port": 47801, "role": "lead"}]`.
public struct RosterEntry: Codable, Sendable, Hashable {
    public var id: DuckID
    public var host: String
    public var port: UInt16
    /// The cast role (see `CastMember.role` in DuckShow.swift) this duck is
    /// currently standing in for.
    public var role: String

    public init(id: DuckID, host: String, port: UInt16 = SwarmLinkInfo.defaultAgentPort, role: String) {
        self.id = id
        self.host = host
        self.port = port
        self.role = role
    }
}

public enum RosterError: Error, Sendable, Equatable, CustomStringConvertible {
    case duplicateDuckID(DuckID)

    public var description: String {
        switch self {
        case .duplicateDuckID(let id): return "duplicate duck id in roster: \(id)"
        }
    }
}

public extension Array where Element == RosterEntry {
    /// Loads a roster file: a plain JSON array of roster entries.
    static func load(contentsOf url: URL) throws -> [RosterEntry] {
        let data = try Data(contentsOf: url)
        let entries = try JSONDecoder().decode([RosterEntry].self, from: data)
        var seen = Set<DuckID>()
        for entry in entries {
            guard seen.insert(entry.id).inserted else {
                throw RosterError.duplicateDuckID(entry.id)
            }
        }
        return entries
    }

    /// Writes the roster back out as a plain JSON array, stable field order.
    func save(to url: URL) throws {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        let data = try encoder.encode(self)
        try data.write(to: url, options: .atomic)
    }

    /// Convenience lookup keyed by duck id.
    func indexedByID() -> [DuckID: RosterEntry] {
        Dictionary(uniqueKeysWithValues: map { ($0.id, $0) })
    }
}
