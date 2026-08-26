import Foundation
import XCTest

@testable import ForesightOCRCore

final class ModelDecodingTests: XCTestCase {
  func testDesktopSetupModelsDecodeBackendContracts() throws {
    let project = try JSONDecoder().decode(
      ProjectCreation.self,
      from: Data(
        #"{"project_root":"/tmp/章氏宗譜","marker":"/tmp/章氏宗譜/foresight-ocr.project.json","database":"/tmp/章氏宗譜/artifacts/foresight-ocr.db"}"#
          .utf8
      )
    )
    XCTAssertEqual(project.projectRoot, "/tmp/章氏宗譜")

    let imported = try JSONDecoder().decode(
      PDFImportResult.self,
      from: Data(
        #"{"project_root":"/tmp/章氏宗譜","document_id":"卷一","source":"/tmp/章氏宗譜/source/卷一.pdf","checksum":"abc","copied":true,"page_count":12}"#
          .utf8
      )
    )
    XCTAssertEqual(imported.documentID, "卷一")
    XCTAssertEqual(imported.pageCount, 12)
    XCTAssertTrue(imported.copied)
  }

  func testManagedEngineAndPreparationEventsDecodeTheirTaggedPayloads() throws {
    let manifestJSON = #"""
      {"protocol_version":1,"engine_home":"/tmp/engines",
      "installer_available":true,"installer":"/App/uv","platform":"macos-arm64",
      "engines":[{"name":"paddleocr_vl","display_name":"PaddleOCR-VL",
      "state":"not_installed","available":false,"supported":true,
      "detail":"not installed","environment":"/tmp/engines/paddleocr-vl",
      "requirements":["mlx-vlm==0.6.13"],"installed_versions":{},"managed":false}]}
      """#
    let manifest = try JSONDecoder().decode(
      EngineManifest.self,
      from: Data(manifestJSON.utf8)
    )
    XCTAssertEqual(manifest.engines.first?.requirements, ["mlx-vlm==0.6.13"])

    let progress = try JSONDecoder().decode(
      EngineInstallEvent.self,
      from: Data(
        #"{"type":"engine_install","engine":"paddleocr_vl","stage":"python_runtime","status":"started"}"#
          .utf8
      )
    )
    XCTAssertEqual(
      progress,
      .progress(
        EngineInstallProgress(
          type: "engine_install",
          engine: "paddleocr_vl",
          stage: "python_runtime",
          status: "started"
        )
      )
    )

    let preparationJSON = #"""
      {"type":"project_prepare_result","protocol_version":1,
      "project_root":"/tmp/project","document_id":"卷一","page_count":12,
      "backend":"paddleocr_vl","recognition":{"requested":20,"read":20,
      "recognized":18,"reused":2,"errors":[]},"ready":true}
      """#
    let preparation = try JSONDecoder().decode(
      ProjectPreparationEvent.self,
      from: Data(preparationJSON.utf8)
    )
    guard case .result(let result) = preparation else {
      return XCTFail("expected preparation result")
    }
    XCTAssertTrue(result.ready)
    XCTAssertEqual(result.recognition.recognized, 18)
  }

  func testManagedRuntimeEnvironmentKeepsEnginesAndModelsInApplicationSupport()
    throws
  {
    let support = URL(fileURLWithPath: "/tmp/Application Support", isDirectory: true)
    let environment = try BackendRuntimeEnvironment.values(
      applicationSupportURL: support,
      base: ["KEEP": "value"]
    )

    XCTAssertEqual(environment["KEEP"], "value")
    XCTAssertEqual(
      environment["FORESIGHT_OCR_ENGINE_HOME"],
      "/tmp/Application Support/Foresight OCR/engines"
    )
    XCTAssertEqual(
      environment["HF_HOME"],
      "/tmp/Application Support/Foresight OCR/models/huggingface"
    )
    XCTAssertEqual(
      environment["PADDLE_HOME"],
      "/tmp/Application Support/Foresight OCR/models/paddle"
    )
  }

  func testBootstrapAndEntryPreserveTraditionalTextAndExplicitBlank() throws {
    let bootstrapJSON = #"""
      {
        "document_id": "丙辰庶富教1",
        "pages": [1],
        "summary": [{"page":1,"entries":1,"flagged":1,"reviewed":1,"ignored":false}],
        "progress": {"entries":1,"reviewed":1},
        "tag": "book-v3",
        "capabilities": ["crop_image_variants", "correction_unconfirm"]
      }
      """#
    let bootstrap = try JSONDecoder().decode(
      ReviewBootstrap.self,
      from: Data(bootstrapJSON.utf8)
    )
    XCTAssertEqual(bootstrap.documentID, "丙辰庶富教1")
    XCTAssertTrue(bootstrap.capabilities.contains("crop_image_variants"))

    let entryJSON = #"""
      {
        "page_index":1,
        "band_index":0,
        "band_label":"庶",
        "entry_index":0,
        "crop_path":"/opaque/crop.png",
        "bbox":[10,20,30,90],
        "crop_bbox":[10,20,30,90],
        "role":"entry",
        "machine":"庶二十五\n譚沈德\n生於道光丙申年九月初三日",
        "machine_backend":"paddleocr_vl",
        "human":"",
        "unreadable":false,
        "note":null,
        "findings":[{"kind":"gap","expected":"庶二十五","observed":"庶二十六"}],
        "own_id":null,
        "parent":null,
        "birth_order":null,
        "parent_order":null,
        "additional_info":null,
        "own_label":"庶",
        "parent_label":"允",
        "leftover":null,
        "expected_own_id":"庶二十五",
        "flagged":true,
        "region_uid":"region-1",
        "state":"verified",
        "stale_reading":false,
        "header_kind":null
      }
      """#
    let entry = try JSONDecoder().decode(
      ReviewEntry.self,
      from: Data(entryJSON.utf8)
    )
    XCTAssertEqual(entry.machine, "庶二十五\n譚沈德\n生於道光丙申年九月初三日")
    XCTAssertEqual(entry.human, "")
    XCTAssertEqual(entry.currentText, "")
    XCTAssertTrue(entry.isConfirmed)
    XCTAssertEqual(entry.id, "region-1")
  }

  func testLoopbackValidationRejectsRemoteAndMissingPort() throws {
    XCTAssertNoThrow(
      try ForesightAPI(baseURL: XCTUnwrap(URL(string: "http://127.0.0.1:8765")))
    )
    XCTAssertThrowsError(
      try ForesightAPI(baseURL: XCTUnwrap(URL(string: "https://127.0.0.1:8765")))
    )
    XCTAssertThrowsError(
      try ForesightAPI(baseURL: XCTUnwrap(URL(string: "http://example.com:8765")))
    )
    XCTAssertThrowsError(
      try ForesightAPI(baseURL: XCTUnwrap(URL(string: "http://localhost")))
    )
  }

  func testOperationalModelsDecodeBackendWireNames() throws {
    let jobJSON = #"""
      {"ok":true,"job":{"id":"job-1","status":"running","stage":"recognizing",
      "completed_pages":2,"current_page_position":3,"total_pages":10,
      "completed_regions":40,"total_regions":200,"percent":28.0,"page":12,
      "error":null,"review_progress":{"entries":3594,"reviewed":222}}}
      """#
    let job = try JSONDecoder().decode(
      ReviewJobEnvelope.self,
      from: Data(jobJSON.utf8)
    ).job
    XCTAssertEqual(job.currentPagePosition, 3)
    XCTAssertEqual(job.completedRegions, 40)
    XCTAssertEqual(job.reviewProgress, ReviewProgress(entries: 3594, reviewed: 222))

    let learningJSON = #"""
      {"ok":true,"status":"ready","document_id":"丙辰庶富教1",
      "analyzed_at":"2026-08-26T08:00:00Z","pending_corrections":2,
      "report_url":"/api/learn-ocr/report","report":{"document_id":"丙辰庶富教1",
      "ocr_tag":"book-v3","reviewed_entries":18,"eligible_entries":12,
      "machine_present":12,"exact_core_entries":9,
      "field_exact":{"own_id":11,"parent":10,"birth_order":12},
      "rates":{"own_id":0.9167,"parent":0.8333,"birth_order":1.0},
      "exact_core_rate":0.75},"comparison":{"previous_exact_core_rate":0.8,
      "delta":-0.05,"status":"lower"}}
      """#
    let learning = try JSONDecoder().decode(
      LearningSnapshot.self,
      from: Data(learningJSON.utf8)
    )
    XCTAssertEqual(learning.report?.eligibleEntries, 12)
    XCTAssertEqual(learning.comparison?.status, "lower")
  }

  func testCombPreviewKeepsNullableHistoricalBandLabel() throws {
    let json = #"""
      {"page":12,"pitch":322.5,"phase_offset":4.0,"phase_adjustment":1.5,
      "base_phase_offset":2.5,"snap":true,"text_left":100.0,"text_right":900.0,
      "boundaries":[900.0,577.5,255.0],"snapped":[true,false,true],
      "manual":[false,true,false],"entries_per_band":2,"entries":6,
      "fitted_pitch":320.0,"corpus_pitch":322.5,"used_corpus_pitch":true,
      "pitch_confidence":0.9,"gutters":[255.0,577.5,900.0],
      "fitted_text_left":98.0,"fitted_text_right":902.0,
      "bands":[{"ordinal":0,"label":null,"top":10.0,"bottom":100.0}]}
      """#
    let preview = try JSONDecoder().decode(CombPreview.self, from: Data(json.utf8))
    XCTAssertEqual(preview.phaseAdjustment, 1.5)
    XCTAssertNil(preview.bands.first?.label)
    XCTAssertEqual(preview.manual, [false, true, false])
  }
}
