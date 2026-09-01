// MasterClock.swift
//
// Master's monotonic clock and show-time epoch bookkeeping. All protocol
// times are nanosecond integers on the sender's monotonic clock
// (docs/swarmlink-protocol.md §4 rule); this is that clock for the master.

import Dispatch

/// A play/seek epoch: the master-monotonic instant at which a known
/// show-time was (or will be) true. `showTime(atMasterTimeNs:)` extrapolates
/// linearly from there — the same math an agent does locally once it has
/// the `at_master_time` / `from_show_time` pair from a `play` or `seek` cmd.
public struct PlayEpoch: Sendable, Equatable {
    /// Master-monotonic nanoseconds at the epoch (`at_master_time`).
    public var masterTimeNs: Int64
    /// Show-time in seconds at the epoch (`from_show_time` / `show_time`).
    public var showTimeAtEpoch: Double

    public init(masterTimeNs: Int64, showTimeAtEpoch: Double) {
        self.masterTimeNs = masterTimeNs
        self.showTimeAtEpoch = showTimeAtEpoch
    }

    public func showTime(atMasterTimeNs now: Int64) -> Double {
        showTimeAtEpoch + Double(now - masterTimeNs) / 1_000_000_000.0
    }
}

/// Monotonic-ns clock plus play-epoch management for the master.
///
/// A value type: `SwarmMaster` (an actor) owns one as mutable state, which
/// keeps this type trivially `Sendable` and easy to unit test in isolation.
public struct MasterClock: Sendable, Equatable {
    public private(set) var epoch: PlayEpoch?

    public init(epoch: PlayEpoch? = nil) {
        self.epoch = epoch
    }

    /// Current monotonic time in nanoseconds. Not wall-clock — never
    /// trusted per protocol rule 4; only ever compared to other values from
    /// this same clock source.
    public static func nowNanoseconds() -> Int64 {
        Int64(bitPattern: DispatchTime.now().uptimeNanoseconds)
    }

    /// Establishes a new play epoch: show-time `fromShowTime` is true at
    /// master time `atMasterTimeNs` (which may be in the future — that's
    /// the scheduled start the `play` cmd's `at_master_time` carries).
    public mutating func play(at atMasterTimeNs: Int64, fromShowTime: Double = 0.0) {
        epoch = PlayEpoch(masterTimeNs: atMasterTimeNs, showTimeAtEpoch: fromShowTime)
    }

    /// Re-anchors the epoch to a new show-time at a given master time
    /// (immediate seek — `atMasterTimeNs` is normally "now").
    public mutating func seek(to showTime: Double, atMasterTimeNs: Int64) {
        epoch = PlayEpoch(masterTimeNs: atMasterTimeNs, showTimeAtEpoch: showTime)
    }

    /// Clears the epoch (stopped/panicked — no show-time is being tracked).
    public mutating func stop() {
        epoch = nil
    }

    /// Show-time at `now` (defaults to the current instant), or `nil` if no
    /// epoch has been established (stopped).
    public func showTime(now: Int64 = MasterClock.nowNanoseconds()) -> Double? {
        epoch?.showTime(atMasterTimeNs: now)
    }
}
