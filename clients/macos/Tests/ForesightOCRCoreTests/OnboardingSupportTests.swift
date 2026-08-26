import Foundation
import XCTest

@testable import ForesightOCRCore

final class OnboardingSupportTests: XCTestCase {
  func testProjectDirectoryAcceptsOnePortableFolderComponent() {
    let parent = URL(fileURLWithPath: "/tmp/Projects", isDirectory: true)
    XCTAssertEqual(
      DesktopOnboardingPolicy.projectDirectory(parent: parent, name: " 章氏宗譜 ")?.path,
      "/tmp/Projects/章氏宗譜"
    )
    XCTAssertNil(
      DesktopOnboardingPolicy.projectDirectory(parent: parent, name: "../Elsewhere")
    )
    XCTAssertNil(
      DesktopOnboardingPolicy.projectDirectory(parent: parent, name: "卷一:副本")
    )
  }

  func testSuggestedEnginePrefersReadyThenAppleSiliconVL() {
    let ppocr = engine(
      name: "ppocr_v5",
      displayName: "PP-OCRv5",
      available: false
    )
    let vl = engine(
      name: "paddleocr_vl",
      displayName: "PaddleOCR-VL",
      available: false
    )
    let manifest = EngineManifest(
      protocolVersion: 1,
      engineHome: "/tmp/engines",
      installerAvailable: true,
      installer: "/App/uv",
      platform: "macos-arm64",
      engines: [ppocr, vl]
    )
    XCTAssertEqual(
      DesktopOnboardingPolicy.suggestedEngine(in: manifest)?.name,
      "paddleocr_vl"
    )

    let readyManifest = EngineManifest(
      protocolVersion: 1,
      engineHome: manifest.engineHome,
      installerAvailable: true,
      installer: manifest.installer,
      platform: manifest.platform,
      engines: [engine(name: "ppocr_v5", displayName: "PP-OCRv5", available: true), vl]
    )
    XCTAssertEqual(
      DesktopOnboardingPolicy.suggestedEngine(in: readyManifest)?.name,
      "ppocr_v5"
    )
  }

  func testPreparationStagesMatchBackendProtocolOrder() {
    XCTAssertEqual(
      DesktopOnboardingPolicy.preparationStages.map(\.id),
      [
        "preserve_pdf", "inspect_pdf", "extract_pages", "normalize_frames",
        "detect_layout", "segment_regions", "initial_ocr",
      ]
    )
  }

  private func engine(
    name: String,
    displayName: String,
    available: Bool
  ) -> EngineStatus {
    EngineStatus(
      name: name,
      displayName: displayName,
      state: available ? "ready" : "not_installed",
      available: available,
      supported: true,
      detail: available ? "ready" : "not installed",
      environment: "/tmp/engines/\(name)",
      requirements: [],
      installedVersions: [:],
      managed: available
    )
  }
}
