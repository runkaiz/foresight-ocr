import Foundation

public struct PreparationStageDefinition: Sendable, Equatable, Identifiable {
  public let id: String
  public let label: String

  public init(id: String, label: String) {
    self.id = id
    self.label = label
  }
}

public enum DesktopOnboardingPolicy {
  public static let preparationStages = [
    PreparationStageDefinition(id: "preserve_pdf", label: "保留 PDF 并校验"),
    PreparationStageDefinition(id: "inspect_pdf", label: "检查 PDF 结构"),
    PreparationStageDefinition(id: "extract_pages", label: "提取原始页面图像"),
    PreparationStageDefinition(id: "normalize_frames", label: "标准化页面边框"),
    PreparationStageDefinition(id: "detect_layout", label: "检测版面"),
    PreparationStageDefinition(id: "segment_regions", label: "切分条目区域"),
    PreparationStageDefinition(id: "initial_ocr", label: "初始 OCR"),
  ]

  public static func projectDirectory(parent: URL, name: String) -> URL? {
    let trimmed = name.trimmingCharacters(in: .whitespacesAndNewlines)
    guard
      !trimmed.isEmpty,
      trimmed != ".",
      trimmed != "..",
      !trimmed.contains("/"),
      !trimmed.contains(":"),
      !trimmed.contains("\0")
    else {
      return nil
    }
    return parent.appending(path: trimmed, directoryHint: .isDirectory)
  }

  public static func suggestedEngine(in manifest: EngineManifest) -> EngineStatus? {
    let supported = manifest.engines.filter(\.supported)
    return supported.first(where: \.available)
      ?? supported.first(where: { $0.name == "paddleocr_vl" })
      ?? supported.first(where: { $0.name == "ppocr_v5" })
      ?? supported.first
  }
}
