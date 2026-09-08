// swift-tools-version:6.0
import PackageDescription

let package = Package(
    name: "FinanceApp",
    platforms: [.macOS(.v14)],
    targets: [
        // UI-free logic lives here so it stays testable. Verifying macOS UI
        // headlessly is largely impossible (plan §7), so everything that can be
        // decided without a window should be decidable without one.
        .target(name: "FinanceCore"),
        .executableTarget(name: "FinanceApp", dependencies: ["FinanceCore"]),
        .testTarget(name: "FinanceCoreTests", dependencies: ["FinanceCore"]),
    ]
)
