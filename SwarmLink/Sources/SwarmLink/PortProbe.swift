// PortProbe.swift
//
// Preflight for `swarmctl serve` (docs/osc-facade.md: exit 3 if the OSC or
// master port cannot be bound). Lives in the library so it is unit-tested;
// the executable target is out of XCTest's reach.

import Foundation

public enum UDPPortProbe {
    /// Can a plain UDP socket bind `port` right now? `SwarmMaster` dials
    /// with `allowLocalEndpointReuse`, which would silently *share* a port
    /// another master already holds (datagrams then split between the
    /// two) — so the probe binds *without* reuse, before either listener
    /// is created, and a taken port becomes a clean exit instead. Port 0
    /// asks the kernel for any port and is always free. If no socket can
    /// be created at all the answer is `true`: the real bind decides.
    public static func isFree(_ port: UInt16) -> Bool {
        let fd = socket(AF_INET, SOCK_DGRAM, 0)
        guard fd >= 0 else { return true } // cannot probe; let the real bind decide
        defer { close(fd) }
        var address = sockaddr_in()
        address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        address.sin_family = sa_family_t(AF_INET)
        address.sin_port = port.bigEndian
        address.sin_addr.s_addr = INADDR_ANY
        let result = withUnsafePointer(to: &address) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) { sockaddrPointer in
                bind(fd, sockaddrPointer, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        return result == 0
    }
}
