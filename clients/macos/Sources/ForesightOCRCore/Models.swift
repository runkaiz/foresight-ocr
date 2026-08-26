import Foundation

public struct ProjectCreation: Codable, Sendable, Equatable {
  public let projectRoot: String
  public let marker: String
  public let database: String

  enum CodingKeys: String, CodingKey {
    case marker, database
    case projectRoot = "project_root"
  }
}

public struct PDFImportResult: Codable, Sendable, Equatable {
  public let projectRoot: String
  public let documentID: String
  public let source: String
  public let checksum: String
  public let copied: Bool
  public let pageCount: Int

  enum CodingKeys: String, CodingKey {
    case source, checksum, copied
    case projectRoot = "project_root"
    case documentID = "document_id"
    case pageCount = "page_count"
  }
}

public struct EngineManifest: Codable, Sendable, Equatable {
  public let protocolVersion: Int
  public let engineHome: String
  public let installerAvailable: Bool
  public let installer: String?
  public let platform: String
  public let engines: [EngineStatus]

  enum CodingKeys: String, CodingKey {
    case installer, platform, engines
    case protocolVersion = "protocol_version"
    case engineHome = "engine_home"
    case installerAvailable = "installer_available"
  }
}

public struct EngineStatus: Codable, Sendable, Equatable, Identifiable {
  public var id: String { name }

  public let name: String
  public let displayName: String
  public let state: String
  public let available: Bool
  public let supported: Bool
  public let detail: String
  public let environment: String
  public let requirements: [String]
  public let installedVersions: [String: String]
  public let managed: Bool

  enum CodingKeys: String, CodingKey {
    case name, state, available, supported, detail, environment, requirements, managed
    case displayName = "display_name"
    case installedVersions = "installed_versions"
  }
}

public struct EngineInstallProgress: Codable, Sendable, Equatable {
  public let type: String
  public let engine: String
  public let stage: String
  public let status: String
}

public enum EngineInstallEvent: Decodable, Sendable, Equatable {
  case progress(EngineInstallProgress)
  case result(EngineStatus)

  private enum EventType: String, Decodable {
    case progress = "engine_install"
    case result = "engine_result"
  }

  private enum CodingKeys: String, CodingKey {
    case type, engine
  }

  public init(from decoder: Decoder) throws {
    let container = try decoder.container(keyedBy: CodingKeys.self)
    switch try container.decode(EventType.self, forKey: .type) {
    case .progress:
      self = .progress(try EngineInstallProgress(from: decoder))
    case .result:
      self = .result(try container.decode(EngineStatus.self, forKey: .engine))
    }
  }
}

public struct ProjectPreparationProgress: Codable, Sendable, Equatable {
  public let type: String
  public let documentID: String
  public let stage: String
  public let label: String
  public let status: String
  public let index: Int
  public let total: Int
  public let detail: String?

  enum CodingKeys: String, CodingKey {
    case type, stage, label, status, index, total, detail
    case documentID = "document_id"
  }
}

public struct RecognitionSummary: Codable, Sendable, Equatable {
  public let requested: Int
  public let read: Int
  public let recognized: Int
  public let reused: Int
  public let errors: [RecognitionFailure]
}

public struct RecognitionFailure: Codable, Sendable, Equatable {
  public let regionUID: String
  public let error: String

  enum CodingKeys: String, CodingKey {
    case error
    case regionUID = "region_uid"
  }
}

public struct ProjectPreparationResult: Codable, Sendable, Equatable {
  public let type: String?
  public let protocolVersion: Int
  public let projectRoot: String
  public let documentID: String
  public let pageCount: Int
  public let backend: String
  public let recognition: RecognitionSummary
  public let ready: Bool

  enum CodingKeys: String, CodingKey {
    case type, backend, recognition, ready
    case protocolVersion = "protocol_version"
    case projectRoot = "project_root"
    case documentID = "document_id"
    case pageCount = "page_count"
  }
}

public enum ProjectPreparationEvent: Decodable, Sendable, Equatable {
  case progress(ProjectPreparationProgress)
  case result(ProjectPreparationResult)

  private enum EventType: String, Decodable {
    case progress = "project_prepare"
    case result = "project_prepare_result"
  }

  private enum CodingKeys: String, CodingKey {
    case type
  }

  public init(from decoder: Decoder) throws {
    let container = try decoder.container(keyedBy: CodingKeys.self)
    switch try container.decode(EventType.self, forKey: .type) {
    case .progress:
      self = .progress(try ProjectPreparationProgress(from: decoder))
    case .result:
      self = .result(try ProjectPreparationResult(from: decoder))
    }
  }
}

public struct DocumentManifest: Codable, Sendable, Equatable {
  public let protocolVersion: Int
  public let projectRoot: String
  public let documents: [DocumentSummary]

  enum CodingKeys: String, CodingKey {
    case protocolVersion = "protocol_version"
    case projectRoot = "project_root"
    case documents
  }
}

public struct DocumentSummary: Codable, Sendable, Identifiable, Equatable {
  public let id: String
  public let title: String
  public let pageCount: Int
  public let reviewable: Bool
  public let entries: Int
  public let reviewed: Int
  public let tag: String?

  enum CodingKeys: String, CodingKey {
    case id, title, reviewable, entries, reviewed, tag
    case pageCount = "page_count"
  }
}

public struct ServiceReady: Codable, Sendable, Equatable {
  public let type: String
  public let protocolVersion: Int
  public let url: URL
  public let documentID: String

  enum CodingKeys: String, CodingKey {
    case type, url
    case protocolVersion = "protocol_version"
    case documentID = "document_id"
  }
}

public struct ReviewProgress: Codable, Sendable, Equatable {
  public let entries: Int
  public let reviewed: Int

  public init(entries: Int, reviewed: Int) {
    self.entries = entries
    self.reviewed = reviewed
  }
}

public struct PageSummary: Codable, Sendable, Identifiable, Equatable {
  public var id: Int { page }

  public let page: Int
  public let entries: Int
  public let flagged: Int
  public let reviewed: Int
  public let ignored: Bool
}

public struct ReviewBootstrap: Codable, Sendable, Equatable {
  public let protocolVersion: Int?
  public let documentID: String
  public let pages: [Int]
  public let summary: [PageSummary]
  public let progress: ReviewProgress
  public let tag: String?
  public let capabilities: Set<String>

  enum CodingKeys: String, CodingKey {
    case pages, summary, progress, tag, capabilities
    case protocolVersion = "protocol_version"
    case documentID = "document_id"
  }
}

public struct PageSpread: Codable, Sendable, Equatable {
  public let page: Int
  public let pages: [ReviewSheet]
  public let progress: ReviewProgress
}

public struct ReviewSheet: Codable, Sendable, Identifiable, Equatable {
  public var id: Int { page }

  public let page: Int
  public let ignored: Bool
  public let imagePath: String?
  public let width: Int
  public let height: Int
  public let frameStatus: String?
  public let entries: [ReviewEntry]

  enum CodingKeys: String, CodingKey {
    case page, ignored, width, height, entries
    case imagePath = "image"
    case frameStatus = "frame_status"
  }
}

public struct ReviewFinding: Codable, Sendable, Equatable, Identifiable {
  public var id: String { "\(kind):\(expected ?? ""):\(observed ?? "")" }

  public let kind: String
  public let expected: String?
  public let observed: String?
}

public struct ReviewEntry: Codable, Sendable, Equatable, Identifiable {
  public var id: String {
    regionUID ?? "\(pageIndex):\(bandLabel):\(entryIndex):\(role)"
  }

  public let pageIndex: Int
  public let bandIndex: Int
  public let bandLabel: String
  public let entryIndex: Int
  public let cropPath: String?
  public let bbox: [Double]?
  public let cropBBox: [Int]?
  public let role: String
  public let machine: String?
  public let machineBackend: String?
  public let human: String?
  public let unreadable: Bool
  public let note: String?
  public let findings: [ReviewFinding]
  public let ownID: String?
  public let parent: String?
  public let birthOrder: String?
  public let parentOrder: String?
  public let additionalInfo: String?
  public let ownLabel: String?
  public let parentLabel: String?
  public let leftover: String?
  public let expectedOwnID: String?
  public let flagged: Bool
  public let regionUID: String?
  public let state: String
  public let staleReading: Bool
  public let headerKind: String?

  public var currentText: String? { human ?? machine }
  public var isConfirmed: Bool { human != nil || unreadable }

  enum CodingKeys: String, CodingKey {
    case bbox, role, machine, human, unreadable, note, findings
    case parent, leftover, flagged, state
    case pageIndex = "page_index"
    case bandIndex = "band_index"
    case bandLabel = "band_label"
    case entryIndex = "entry_index"
    case cropPath = "crop_path"
    case cropBBox = "crop_bbox"
    case machineBackend = "machine_backend"
    case ownID = "own_id"
    case birthOrder = "birth_order"
    case parentOrder = "parent_order"
    case additionalInfo = "additional_info"
    case ownLabel = "own_label"
    case parentLabel = "parent_label"
    case expectedOwnID = "expected_own_id"
    case regionUID = "region_uid"
    case staleReading = "stale_reading"
    case headerKind = "header_kind"
  }
}

public enum ImageVariant: String, Codable, Sendable, CaseIterable, Identifiable {
  case original
  case watermark

  public var id: String { rawValue }
}

public struct CropImage: Codable, Sendable, Equatable {
  public let regionUID: String
  public let variant: ImageVariant
  public let path: String
  public let width: Int
  public let height: Int
  public let pixelBBox: [Int]

  enum CodingKeys: String, CodingKey {
    case variant, path, width, height
    case regionUID = "region_uid"
    case pixelBBox = "pixel_bbox"
  }
}

public struct PageImage: Codable, Sendable, Equatable {
  public let page: Int
  public let path: String
  public let width: Int
  public let height: Int
  public let variant: ImageVariant
}

public struct PageIgnoreResponse: Codable, Sendable, Equatable {
  public let ok: Bool
  public let pageIndex: Int
  public let ignored: Bool
  public let summary: [PageSummary]
  public let progress: ReviewProgress

  enum CodingKeys: String, CodingKey {
    case ok, ignored, summary, progress
    case pageIndex = "page_index"
  }
}

public struct ReviewJobEnvelope: Codable, Sendable, Equatable {
  public let ok: Bool
  public let job: ReviewJob
}

public struct ReviewJobStart: Codable, Sendable, Equatable {
  public let ok: Bool
  public let started: Bool
  public let job: ReviewJob
}

public struct ReviewJob: Codable, Sendable, Equatable {
  public let id: String?
  public let status: String
  public let stage: String
  public let completedPages: Int
  public let currentPagePosition: Int
  public let totalPages: Int
  public let completedRegions: Int
  public let totalRegions: Int
  public let percent: Double
  public let page: Int?
  public let error: String?
  public let reviewProgress: ReviewProgress?

  enum CodingKeys: String, CodingKey {
    case id, status, stage, percent, page, error
    case completedPages = "completed_pages"
    case currentPagePosition = "current_page_position"
    case totalPages = "total_pages"
    case completedRegions = "completed_regions"
    case totalRegions = "total_regions"
    case reviewProgress = "review_progress"
  }
}

public struct LearningSnapshot: Codable, Sendable, Equatable {
  public let ok: Bool
  public let status: String
  public let documentID: String
  public let analyzedAt: String?
  public let pendingCorrections: Int
  public let report: LearningReport?
  public let reportURL: String
  public let comparison: LearningComparison?

  enum CodingKeys: String, CodingKey {
    case ok, status, report, comparison
    case documentID = "document_id"
    case analyzedAt = "analyzed_at"
    case pendingCorrections = "pending_corrections"
    case reportURL = "report_url"
  }
}

public struct LearningReport: Codable, Sendable, Equatable {
  public let documentID: String
  public let ocrTag: String?
  public let reviewedEntries: Int
  public let eligibleEntries: Int
  public let machinePresent: Int
  public let exactCoreEntries: Int
  public let fieldExact: [String: Int]
  public let rates: [String: Double]
  public let exactCoreRate: Double

  enum CodingKeys: String, CodingKey {
    case rates
    case documentID = "document_id"
    case ocrTag = "ocr_tag"
    case reviewedEntries = "reviewed_entries"
    case eligibleEntries = "eligible_entries"
    case machinePresent = "machine_present"
    case exactCoreEntries = "exact_core_entries"
    case fieldExact = "field_exact"
    case exactCoreRate = "exact_core_rate"
  }
}

public struct LearningComparison: Codable, Sendable, Equatable {
  public let previousExactCoreRate: Double?
  public let delta: Double?
  public let status: String

  enum CodingKeys: String, CodingKey {
    case delta, status
    case previousExactCoreRate = "previous_exact_core_rate"
  }
}

public struct NumeralResponse: Codable, Sendable, Equatable {
  public let text: String
}

public struct CombPreview: Codable, Sendable, Equatable {
  public let page: Int
  public let pitch: Double
  public let phaseOffset: Double
  public let phaseAdjustment: Double
  public let basePhaseOffset: Double
  public let snap: Bool
  public let textLeft: Double
  public let textRight: Double
  public let boundaries: [Double]
  public let snapped: [Bool]
  public let manual: [Bool]
  public let entriesPerBand: Int
  public let entries: Int
  public let fittedPitch: Double
  public let corpusPitch: Double?
  public let usedCorpusPitch: Bool
  public let pitchConfidence: Double?
  public let gutters: [Double]
  public let fittedTextLeft: Double
  public let fittedTextRight: Double
  public let bands: [CombBand]

  enum CodingKeys: String, CodingKey {
    case page, pitch, snap, boundaries, snapped, manual, entries, gutters, bands
    case phaseOffset = "phase_offset"
    case phaseAdjustment = "phase_adjustment"
    case basePhaseOffset = "base_phase_offset"
    case textLeft = "text_left"
    case textRight = "text_right"
    case entriesPerBand = "entries_per_band"
    case fittedPitch = "fitted_pitch"
    case corpusPitch = "corpus_pitch"
    case usedCorpusPitch = "used_corpus_pitch"
    case pitchConfidence = "pitch_confidence"
    case fittedTextLeft = "fitted_text_left"
    case fittedTextRight = "fitted_text_right"
  }
}

public struct CombBand: Codable, Sendable, Equatable, Identifiable {
  public var id: Int { ordinal }
  public let ordinal: Int
  public let label: String?
  public let top: Double
  public let bottom: Double
}

public struct ExportBundle: Codable, Sendable, Equatable {
  public let ok: Bool
  public let files: [ExportFile]
}

public struct ExportFile: Codable, Sendable, Equatable, Identifiable {
  public var id: String { name }
  public let name: String
  public let content: String
}

public struct CorrectionFields: Codable, Sendable, Equatable {
  public var ownID: String
  public var parent: String
  public var birthOrder: String
  public var additionalInfo: String

  public init(
    ownID: String = "",
    parent: String = "",
    birthOrder: String = "",
    additionalInfo: String = ""
  ) {
    self.ownID = ownID
    self.parent = parent
    self.birthOrder = birthOrder
    self.additionalInfo = additionalInfo
  }

  enum CodingKeys: String, CodingKey {
    case parent
    case ownID = "own_id"
    case birthOrder = "birth_order"
    case additionalInfo = "additional_info"
  }
}

public struct CorrectionResponse: Codable, Sendable, Equatable {
  public let ok: Bool
  public let transcription: String?
  public let confirmed: Bool?
  public let removed: Bool?
  public let progress: ReviewProgress
}

public struct ErrorEnvelope: Codable, Sendable, Equatable {
  public let error: String
}
