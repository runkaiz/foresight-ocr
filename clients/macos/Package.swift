// swift-tools-version: 6.4

import PackageDescription

let package = Package(
  name: "ForesightOCRMac",
  defaultLocalization: "zh-Hans",
  platforms: [.macOS(.v27)],
  products: [
    .library(name: "ForesightOCRCore", targets: ["ForesightOCRCore"]),
    .executable(name: "ForesightOCR", targets: ["ForesightOCRApp"]),
  ],
  targets: [
    .target(name: "ForesightOCRCore"),
    .executableTarget(
      name: "ForesightOCRApp",
      dependencies: ["ForesightOCRCore"]
    ),
    .testTarget(
      name: "ForesightOCRCoreTests",
      dependencies: ["ForesightOCRCore"]
    ),
  ]
)
