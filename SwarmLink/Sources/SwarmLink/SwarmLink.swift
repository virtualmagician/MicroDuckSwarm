// SwarmLink.swift
//
// Version and protocol-wide constants for SwarmLink, the Swift 6 master-side
// implementation of docs/swarmlink-protocol.md.

/// Namespace for package-wide constants. Not `enum SwarmLink` to avoid
/// colliding with the module name `SwarmLink` itself.
public enum SwarmLinkInfo {
    /// SwarmLink package version (not the wire-protocol version).
    public static let version = "0.1.0"

    /// Wire-protocol major version, carried as `"v"` in every message.
    /// See docs/swarmlink-protocol.md, top of file.
    public static let protocolVersion = 1

    /// UDP port the master listens on (and originates all per-duck
    /// traffic from). See docs/swarmlink-protocol.md §Ports.
    public static let defaultMasterPort: UInt16 = 47800

    /// UDP port every duck-agent listens on. See docs/swarmlink-protocol.md §Ports.
    public static let defaultAgentPort: UInt16 = 47801

    /// `.duckshow` format major version this package understands.
    /// See docs/duckshow-format.md — parsers reject unknown major versions.
    public static let duckShowFormatMajor = 1

    /// A duck is marked `lost` after this many seconds without telemetry.
    /// See docs/swarmlink-protocol.md §4.
    public static let telemetryLostThresholdSeconds: Double = 5.0

    /// Command retry cadence: up to this many attempts...
    public static let commandMaxAttempts = 5

    /// ...at this interval, until ACKed. See docs/swarmlink-protocol.md §3.
    public static let commandRetryIntervalMs: UInt64 = 100

    /// Rate of the `state` broadcast while armed/playing. See §2.
    public static let stateBroadcastHz: Double = 5.0
}
