// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "SwarmLink",
    platforms: [
        .macOS(.v13)
    ],
    products: [
        .library(name: "SwarmLink", targets: ["SwarmLink"]),
        .executable(name: "swarmctl", targets: ["swarmctl"])
    ],
    dependencies: [],
    targets: [
        .target(
            name: "SwarmLink",
            dependencies: []
        ),
        .executableTarget(
            name: "swarmctl",
            dependencies: ["SwarmLink"]
        ),
        .testTarget(
            name: "SwarmLinkTests",
            dependencies: ["SwarmLink"]
        )
    ]
)
