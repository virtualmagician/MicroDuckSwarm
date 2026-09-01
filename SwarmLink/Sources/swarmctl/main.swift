// swarmctl — reference CLI over SwarmLink's SwarmMaster.
//
// Usage:
//   swarmctl --roster roster.json load show.duckshow.json
//   swarmctl --roster roster.json play [--lead-ms 300]
//   swarmctl --roster roster.json seek <show_time>
//   swarmctl --roster roster.json stop
//   swarmctl --roster roster.json panic
//   swarmctl --roster roster.json status [--seconds 2]
//   swarmctl --help
//
// Zero third-party deps: argument parsing is hand-rolled. Each invocation
// is a short-lived process — it starts a fresh SwarmMaster, performs one
// action, and exits, which is fine for load-in smoke tests but is not how
// this engine is meant to run for a real show (that's the embedded
// SwarmMaster actor living inside StageWizard for the whole performance;
// see docs/architecture.md).

import Foundation
import SwarmLink

func printUsage() {
    print("""
    swarmctl \(SwarmLinkInfo.version) — SwarmLink reference master CLI

    USAGE:
      swarmctl --roster <roster.json> load <show.duckshow.json>
      swarmctl --roster <roster.json> play [--lead-ms <n>]
      swarmctl --roster <roster.json> seek <show_time_seconds>
      swarmctl --roster <roster.json> stop
      swarmctl --roster <roster.json> panic
      swarmctl --roster <roster.json> status [--seconds <n>]
      swarmctl --help

    Each invocation is a fresh, short-lived master session: `load` verifies
    the show + roster and pre-arms every duck's connection, but that state
    does not persist to a later `play` in a *different* process invocation.
    For a real show, SwarmMaster is meant to be embedded and held alive by
    the caller (see docs/architecture.md) — this CLI is a protocol-level
    smoke-test tool.
    """)
}

struct CLIError: Error, CustomStringConvertible {
    let message: String
    var description: String { message }
}

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(("error: " + message + "\n").data(using: .utf8)!)
    exit(1)
}

func formatTelemetryTable(_ telemetry: [DuckID: DuckTelemetry]) -> String {
    if telemetry.isEmpty {
        return "(no telemetry received)"
    }
    var lines = ["DUCK\tSTATE\tSHOW_TIME\tOFFSET_MS\tRTT_MS\tPOLICIES_OK\tLOST\tLAST_ERROR"]
    for duckID in telemetry.keys.sorted() {
        guard let t = telemetry[duckID] else { continue }
        lines.append([
            duckID.raw,
            t.state.rawValue,
            String(format: "%.2f", t.showTime),
            String(format: "%.1f", t.clockOffsetMs),
            String(format: "%.1f", t.clockRttMs),
            t.policiesOk ? "yes" : "no",
            t.lost ? "LOST" : "no",
            t.lastError ?? "-"
        ].joined(separator: "\t"))
    }
    return lines.joined(separator: "\n")
}

@main
struct SwarmCtl {
    static func main() async {
        var args = Array(CommandLine.arguments.dropFirst())

        if args.isEmpty || args.contains("--help") || args.contains("-h") {
            printUsage()
            exit(0)
        }

        var rosterPath: String?
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

        let master = SwarmMaster()

        switch command {
        case "load":
            guard args.count >= 2 else { fail("load requires a show path") }
            let showURL = URL(fileURLWithPath: args[1])
            do {
                let outcomes = try await master.load(show: showURL, roster: rosterURL)
                for duckID in outcomes.keys.sorted() {
                    print("\(duckID): \(outcomes[duckID]!.status)")
                }
                let failed = outcomes.values.contains { !$0.isOK }
                exit(failed ? 1 : 0)
            } catch {
                fail("load failed: \(error)")
            }

        case "play":
            await master.play(at: leadMs * 1_000_000)
            print("play scheduled (\(leadMs) ms lead)")

        case "seek":
            guard args.count >= 2, let showTime = Double(args[1]) else { fail("seek requires a show_time in seconds") }
            await master.seek(to: showTime)
            print("seek to \(showTime)s sent")

        case "stop":
            await master.stop()
            print("stop sent")

        case "panic":
            await master.panic()
            print("panic sent")

        case "status":
            // A fresh process has no history, so give agents a moment to
            // report in before printing whatever arrived.
            try? await Task.sleep(nanoseconds: UInt64(statusSeconds * 1_000_000_000))
            let snapshot = await master.telemetry
            print(formatTelemetryTable(snapshot))

        default:
            fail("unknown command '\(command)'")
        }
    }
}
