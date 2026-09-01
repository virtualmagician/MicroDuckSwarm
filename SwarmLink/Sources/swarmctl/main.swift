// swarmctl — reference CLI over SwarmLink's SwarmMaster.
//
// Usage:
//   swarmctl --roster roster.json load show.duckshow.json
//   swarmctl --roster roster.json play show.duckshow.json [--lead-ms 300]
//   swarmctl --roster roster.json run  show.duckshow.json [--lead-ms 300]
//   swarmctl --roster roster.json seek <show_time>
//   swarmctl --roster roster.json stop
//   swarmctl --roster roster.json panic
//   swarmctl --roster roster.json status [--seconds 2]
//   swarmctl --help
//
// Zero third-party deps: argument parsing is hand-rolled. Each invocation
// is one short-lived master session: it dials the roster, performs one
// action, waits for every duck's ACK/NACK/timeout, prints the per-duck
// outcome and exits non-zero if any duck did not ACK. `play`/`run` keep
// the session alive (load → play → monitor telemetry) because a `play`
// needs the show loaded in the *same* process; `seek`/`stop`/`panic`/
// `status` only need the roster, so they work against ducks started by
// another master (e.g. the SwarmMaster embedded in StageWizard, which is
// how a real show is run — see docs/architecture.md).

import Foundation
import SwarmLink

func printUsage() {
    print("""
    swarmctl \(SwarmLinkInfo.version) — SwarmLink reference master CLI

    USAGE:
      swarmctl --roster <roster.json> [--master-port <n>] <command> [args]

    COMMANDS:
      load <show.duckshow.json>              verify + pre-load the show on every roster duck
      play <show.duckshow.json> [--lead-ms]  load, play, then monitor telemetry until Ctrl+C (panic)
      run  <show.duckshow.json> [--lead-ms]  load, play, monitor until the show ends, stop, exit 0
      seek <show_time_seconds>               seek every duck (NACKed by ducks that are not playing)
      stop                                   graceful stop (zero locomotion, robot.stop, → LOADED)
      panic                                  emergency stop from any state (→ IDLE)
      status [--seconds <n>]                 listen for telemetry for n seconds and print a table

    OPTIONS:
      --roster <path>     roster JSON: [{"id","host","port","role"}, ...]   (required)
      --master-port <n>   local UDP port to bind (default \(SwarmLinkInfo.defaultMasterPort)); use a unique
                          port when several masters/tests share one machine
      --lead-ms <n>       play scheduling lead time (default 300)
      --seconds <n>       how long `status` listens (default 1.5)

    Every command waits for each duck's ACK (retried \(SwarmLinkInfo.commandMaxAttempts)× at \(SwarmLinkInfo.commandRetryIntervalMs) ms) and
    exits 1 if any duck NACKed, timed out, or no duck is connected; 130 if
    interrupted. A show is loaded per process: `play`/`run` load it themselves.
    """)
}

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(("error: " + message + "\n").data(using: .utf8)!)
    exit(1)
}

// MARK: - Output helpers

func describe(_ status: CommandStatus) -> String {
    switch status {
    case .ok: return "ACK"
    case .nacked(let error): return "NACK (\(error))"
    case .timeout: return "TIMEOUT"
    case .connectionFailed(let reason): return "FAILED (\(reason))"
    case .superseded: return "SUPERSEDED"
    }
}

/// Prints one line per duck and returns whether every duck ACKed. An empty
/// outcome set is a failure too: nothing was sent to anyone.
@discardableResult
func report(_ action: String, _ outcomes: [DuckID: CommandStatus]) -> Bool {
    if outcomes.isEmpty {
        print("[\(action)] no ducks connected — nothing was sent (empty roster?)")
        return false
    }
    for duckID in outcomes.keys.sorted() {
        print("[\(action)] \(duckID): \(describe(outcomes[duckID]!))")
    }
    return outcomes.values.allSatisfy { $0 == .ok }
}

/// `nil` means "not yet synced" (docs/swarmlink-protocol.md §4) — never
/// printed as 0.
func formatMs(_ value: Double?) -> String {
    guard let value else { return "—" }
    return String(format: "%.1f", value)
}

func formatTelemetryTable(_ telemetry: [DuckID: DuckTelemetry], roster: Set<DuckID>, lost: Set<DuckID>) -> String {
    let ducks = roster.union(telemetry.keys).sorted()
    if ducks.isEmpty {
        return "(no ducks on the roster)"
    }
    var lines = ["DUCK\tSTATE\tSHOW_TIME\tOFFSET_MS\tRTT_MS\tPOLICIES_OK\tLOST\tLAST_ERROR"]
    for duckID in ducks {
        guard let t = telemetry[duckID] else {
            let flag = lost.contains(duckID) ? "LOST" : "no"
            lines.append("\(duckID.raw)\t-\t-\t-\t-\t-\t\(flag)\t(no telemetry received)")
            continue
        }
        lines.append([
            duckID.raw,
            t.state.rawValue,
            String(format: "%.2f", t.showTime),
            formatMs(t.clockOffsetMs),
            formatMs(t.clockRttMs),
            t.policiesOk ? "yes" : "no",
            (t.lost || lost.contains(duckID)) ? "LOST" : "no",
            t.lastError ?? "-"
        ].joined(separator: "\t"))
    }
    return lines.joined(separator: "\n")
}

func describe(_ event: TelemetryEvent) -> String {
    switch event {
    case .updated(let t):
        let err = t.lastError.map { " last_error=\($0)" } ?? ""
        return "[telemetry] \(t.duck) state=\(t.state.rawValue) show_time=\(String(format: "%.2f", t.showTime)) offset_ms=\(formatMs(t.clockOffsetMs)) rtt_ms=\(formatMs(t.clockRttMs))\(err)"
    case .lost(let duck):
        return "[telemetry] \(duck) LOST (no telemetry for \(SwarmLinkInfo.telemetryLostThresholdSeconds) s)"
    }
}

// MARK: - SIGINT → AsyncStream

/// Turns SIGINT into an `AsyncStream<Void>` so `play`/`run` can race the
/// operator's Ctrl+C against telemetry and the show's end and send `panic`
/// before exiting (panic: works from any state, never NACKed).
final class InterruptWatcher: @unchecked Sendable {
    let interrupts: AsyncStream<Void>
    private let source: DispatchSourceSignal

    init() {
        let (stream, continuation) = AsyncStream<Void>.makeStream()
        interrupts = stream
        signal(SIGINT, SIG_IGN)
        source = DispatchSource.makeSignalSource(signal: SIGINT, queue: DispatchQueue(label: "swarmctl.sigint"))
        source.setEventHandler { continuation.yield() }
        source.resume()
    }
}

/// Prints telemetry until `deadlineNs` elapses (nil = forever) or SIGINT.
/// Returns true if interrupted.
func monitor(_ master: SwarmMaster, deadlineNs: UInt64?, interrupts: AsyncStream<Void>) async -> Bool {
    await withTaskGroup(of: Bool.self) { group in
        group.addTask {
            for await event in await master.telemetryEvents() {
                print(describe(event))
            }
            return false
        }
        group.addTask {
            for await _ in interrupts { return true }
            return false
        }
        if let deadlineNs {
            group.addTask {
                try? await Task.sleep(nanoseconds: deadlineNs)
                return false
            }
        }
        let interrupted = await group.next() ?? false
        group.cancelAll()
        return interrupted
    }
}

// MARK: - Main

@main
struct SwarmCtl {
    static func main() async {
        var args = Array(CommandLine.arguments.dropFirst())

        if args.isEmpty || args.contains("--help") || args.contains("-h") {
            printUsage()
            exit(0)
        }

        var rosterPath: String?
        var masterPort: UInt16 = SwarmLinkInfo.defaultMasterPort
        var leadMs: Int64 = 300
        var statusSeconds: Double = 1.5
        var positionals: [String] = []

        var i = 0
        while i < args.count {
            switch args[i] {
            case "--roster":
                guard i + 1 < args.count else { fail("--roster requires a path") }
                rosterPath = args[i + 1]
                i += 2
            case "--master-port":
                guard i + 1 < args.count, let value = UInt16(args[i + 1]) else { fail("--master-port requires a port number (0-65535)") }
                masterPort = value
                i += 2
            case "--lead-ms":
                guard i + 1 < args.count, let value = Int64(args[i + 1]) else { fail("--lead-ms requires an integer") }
                leadMs = value
                i += 2
            case "--seconds":
                guard i + 1 < args.count, let value = Double(args[i + 1]) else { fail("--seconds requires a number") }
                statusSeconds = value
                i += 2
            default:
                positionals.append(args[i])
                i += 1
            }
        }
        args = positionals

        guard let command = args.first else {
            printUsage()
            exit(1)
        }

        guard let rosterPath else {
            fail("--roster <roster.json> is required")
        }
        let rosterURL = URL(fileURLWithPath: rosterPath)

        let master = SwarmMaster(masterPort: masterPort)

        /// `seek`/`stop`/`panic`/`status` need connections but no show.
        func connect() async {
            do {
                try await master.connect(roster: rosterURL)
            } catch {
                fail("could not read roster: \(error)")
            }
        }

        /// `load`/`play`/`run`: parse + load; returns the show (for its
        /// duration) or exits 1 with the per-duck report printed.
        func loadOrExit(_ showPath: String) async -> Show {
            let showURL = URL(fileURLWithPath: showPath)
            let show: Show
            do {
                show = try Show.load(contentsOf: showURL)
            } catch {
                fail("could not load show: \(error)")
            }
            do {
                let outcomes = try await master.load(show: showURL, roster: rosterURL)
                let ok = report("load", outcomes.mapValues(\.status))
                if !ok { exit(1) }
            } catch {
                fail("load failed: \(error)")
            }
            return show
        }

        switch command {
        case "load":
            guard args.count >= 2 else { fail("load requires a show path") }
            _ = await loadOrExit(args[1])
            exit(0)

        case "play", "run":
            guard args.count >= 2 else { fail("\(command) requires a show path") }
            let watcher = InterruptWatcher()
            let show = await loadOrExit(args[1])
            let outcomes: [DuckID: CommandStatus]
            do {
                outcomes = try await master.play(at: leadMs * 1_000_000)
            } catch {
                fail("play failed: \(error)")
            }
            guard report("play", outcomes) else {
                print("[play] not every duck ACKed — sending panic so nobody performs alone")
                report("panic", await master.panic())
                exit(1)
            }
            let deadlineNs: UInt64?
            if command == "run" {
                let seconds = Double(leadMs) / 1000.0 + max(0, show.meta.duration) + 1.0
                print("[run] armed; playing for ~\(String(format: "%.1f", seconds))s then exiting (Ctrl+C = panic).")
                deadlineNs = UInt64(seconds * 1_000_000_000)
            } else {
                print("[play] armed; starting in ~\(leadMs) ms. Monitoring telemetry (Ctrl+C = panic + exit).")
                deadlineNs = nil
            }
            let interrupted = await monitor(master, deadlineNs: deadlineNs, interrupts: watcher.interrupts)
            if interrupted {
                print("[\(command)] interrupted — sending panic")
                let ok = report("panic", await master.panic())
                exit(ok ? 130 : 1)
            }
            // The agents ended the show themselves at meta.duration; reset
            // the master's transport to match.
            let ok = report("stop", await master.stop())
            exit(ok ? 0 : 1)

        case "seek":
            guard args.count >= 2, let showTime = Double(args[1]) else { fail("seek requires a show_time in seconds") }
            await connect()
            let ok = report("seek", await master.seek(to: showTime))
            exit(ok ? 0 : 1)

        case "stop":
            await connect()
            let ok = report("stop", await master.stop())
            exit(ok ? 0 : 1)

        case "panic":
            await connect()
            let ok = report("panic", await master.panic())
            exit(ok ? 0 : 1)

        case "status":
            await connect()
            // A fresh process has no history, so give agents a moment to
            // report in before printing whatever arrived.
            try? await Task.sleep(nanoseconds: UInt64(max(0, statusSeconds) * 1_000_000_000))
            let snapshot = await master.telemetry
            let lost = await master.lostDucks
            let roster = await master.connectedDucks
            print(formatTelemetryTable(snapshot, roster: roster, lost: lost))
            exit(lost.isEmpty ? 0 : 1)

        default:
            fail("unknown command '\(command)'")
        }
    }
}
