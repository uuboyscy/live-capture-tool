// swift-tools-version: 6.0

import PackageDescription

let package = Package(
    name: "LiveTranscriptCore",
    platforms: [
        .iOS(.v17),
        .macOS(.v13),
    ],
    products: [
        .library(name: "LiveTranscriptCore", targets: ["LiveTranscriptCore"]),
    ],
    targets: [
        .target(name: "LiveTranscriptCore"),
        .testTarget(
            name: "LiveTranscriptCoreTests",
            dependencies: ["LiveTranscriptCore"]
        ),
    ]
)
