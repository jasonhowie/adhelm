// swift-tools-version: 6.0
import PackageDescription

let package = Package(
  name: "AdHelmMacOSApprovalHost",
  platforms: [.macOS(.v13)],
  products: [
    .library(name: "ApprovalHostCore", targets: ["ApprovalHostCore"]),
    .library(name: "ApprovalHostMacOS", targets: ["ApprovalHostMacOS"]),
    .executable(name: "adhelm-macos-approval-host", targets: ["ApprovalHostApp"]),
    .executable(name: "adhelm-macos-installed-approval-host", targets: ["ApprovalHostInstalledApp"]),
    .executable(name: "adhelm-macos-approval-client", targets: ["ApprovalHostInstalledClient"]),
    .executable(
      name: "adhelm-macos-meta-campaign-pause-approval-client",
      targets: ["ApprovalHostInstalledMetaCampaignPauseClient"]
    ),
    .executable(
      name: "adhelm-macos-approval-build-inspector", targets: ["ApprovalHostBuildInspector"]),
  ],
  targets: [
    .target(name: "ApprovalHostCore"),
    .target(
      name: "ApprovalHostMacOS",
      dependencies: ["ApprovalHostCore"],
      linkerSettings: [
        .linkedFramework("AppKit"),
        .linkedFramework("LocalAuthentication"),
        .linkedFramework("Security"),
        .linkedFramework("CoreGraphics"),
      ]
    ),
    .executableTarget(name: "ApprovalHostApp", dependencies: ["ApprovalHostMacOS"]),
    .executableTarget(name: "ApprovalHostInstalledApp", dependencies: ["ApprovalHostMacOS"]),
    .executableTarget(name: "ApprovalHostInstalledClient", dependencies: ["ApprovalHostMacOS"]),
    .executableTarget(
      name: "ApprovalHostInstalledMetaCampaignPauseClient",
      dependencies: ["ApprovalHostMacOS"]
    ),
    .executableTarget(name: "ApprovalHostBuildInspector", dependencies: ["ApprovalHostCore"]),
    .testTarget(
      name: "ApprovalHostCoreTests",
      dependencies: ["ApprovalHostCore"],
      resources: [.copy("Fixtures")]
    ),
    .testTarget(
      name: "ApprovalHostMacOSTests",
      dependencies: ["ApprovalHostMacOS"],
      resources: [.copy("Fixtures")]
    ),
  ]
)
