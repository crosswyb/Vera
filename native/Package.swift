// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "ZSJHAudio",
    platforms: [.macOS("26.0")],
    products: [
        .library(name: "AudioCore", targets: ["AudioCore"]),
        .executable(name: "AudioHelper", targets: ["AudioHelper"]),
        .executable(name: "DesktopHost", targets: ["DesktopHost"]),
    ],
    targets: [
        .target(name: "AudioCore"),
        .executableTarget(name: "AudioHelper", dependencies: ["AudioCore"]),
        .executableTarget(name: "DesktopHost", dependencies: ["AudioCore"]),
        .testTarget(name: "AudioCoreTests", dependencies: ["AudioCore"]),
    ],
    swiftLanguageModes: [.v5]
)
