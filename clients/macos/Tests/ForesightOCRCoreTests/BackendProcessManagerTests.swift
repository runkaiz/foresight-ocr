import Foundation
import XCTest

@testable import ForesightOCRCore

final class BackendProcessManagerTests: XCTestCase {
  func testTypedSetupCommandsAndStreamingEngineEvents() async throws {
    let root = FileManager.default.temporaryDirectory
      .appending(path: UUID().uuidString, directoryHint: .isDirectory)
    try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: root) }
    let project = root.appending(path: "章氏宗譜", directoryHint: .isDirectory)
    try FileManager.default.createDirectory(at: project, withIntermediateDirectories: true)
    let executable = root.appending(path: "fake-backend")
    try Self.fakeBackend.write(to: executable, atomically: true, encoding: .utf8)
    try FileManager.default.setAttributes(
      [.posixPermissions: 0o755],
      ofItemAtPath: executable.path
    )

    let manager = BackendProcessManager()
    let created = try await manager.createProject(
      directoryURL: project,
      name: "章氏宗譜",
      executableURL: executable
    )
    XCTAssertEqual(
      URL(fileURLWithPath: created.projectRoot).standardizedFileURL,
      project.standardizedFileURL
    )

    let pdf = root.appending(path: "卷一.pdf")
    try Data("%PDF-fake".utf8).write(to: pdf)
    let imported = try await manager.importPDF(
      projectURL: project,
      pdfURL: pdf,
      documentID: "卷一",
      executableURL: executable
    )
    XCTAssertEqual(imported.documentID, "卷一")
    XCTAssertEqual(imported.pageCount, 12)

    let manifest = try await manager.engineManifest(executableURL: executable)
    XCTAssertTrue(manifest.installerAvailable)
    XCTAssertTrue(manifest.engineHome.hasSuffix("Foresight OCR/engines"))

    let collector = LockedCollector<EngineInstallEvent>()
    let installed = try await manager.installEngine(
      name: "ppocr_v5",
      executableURL: executable
    ) {
      collector.append($0)
    }
    XCTAssertEqual(installed.name, "ppocr_v5")
    XCTAssertEqual(collector.values.count, 2)
    guard case .result(let status) = collector.values.last else {
      return XCTFail("expected final engine result before command completion")
    }
    XCTAssertTrue(status.available)
    XCTAssertEqual(status.installedVersions["paddleocr"], "3.7.0")

    let preparationEvents = LockedCollector<ProjectPreparationEvent>()
    let prepared = try await manager.prepareProject(
      projectURL: project,
      documentID: "卷一",
      backend: "ppocr_v5",
      executableURL: executable
    ) {
      preparationEvents.append($0)
    }
    XCTAssertTrue(prepared.ready)
    XCTAssertEqual(prepared.recognition.reused, 3)
    XCTAssertEqual(preparationEvents.values.count, 3)
  }

  private static let fakeBackend = #"""
    #!/bin/zsh
    set -euo pipefail
    if [[ "$1" == "project" && "$2" == "init" ]]; then
      root="$3"
      print -r -- "{\"project_root\":\"$root\",\"marker\":\"$root/foresight-ocr.project.json\",\"database\":\"$root/artifacts/foresight-ocr.db\"}"
    elif [[ "$1" == "project" && "$2" == "import" ]]; then
      print -r -- "{\"project_root\":\"$PWD\",\"document_id\":\"卷一\",\"source\":\"$PWD/source/卷一.pdf\",\"checksum\":\"abc\",\"copied\":true,\"page_count\":12}"
    elif [[ "$1" == "project" && "$2" == "prepare" ]]; then
      print -r -- '{"type":"project_prepare","document_id":"卷一","stage":"preserve_pdf","label":"保留 PDF 并校验","status":"started","index":1,"total":7,"detail":null}'
      print -r -- '{"type":"project_prepare","document_id":"卷一","stage":"preserve_pdf","label":"保留 PDF 并校验","status":"completed","index":1,"total":7,"detail":null}'
      print -r -- "{\"type\":\"project_prepare_result\",\"protocol_version\":1,\"project_root\":\"$PWD\",\"document_id\":\"卷一\",\"page_count\":12,\"backend\":\"ppocr_v5\",\"recognition\":{\"requested\":3,\"read\":3,\"recognized\":0,\"reused\":3,\"errors\":[]},\"ready\":true}"
    elif [[ "$1" == "engine" && "$2" == "status" ]]; then
      print -r -- "{\"protocol_version\":1,\"engine_home\":\"$FORESIGHT_OCR_ENGINE_HOME\",\"installer_available\":true,\"installer\":\"/App/uv\",\"platform\":\"macos-arm64\",\"engines\":[]}"
    elif [[ "$1" == "engine" && "$2" == "install" ]]; then
      print -r -- '{"type":"engine_install","engine":"ppocr_v5","stage":"engine_packages","status":"completed"}'
      print -r -- '{"type":"engine_result","engine":{"name":"ppocr_v5","display_name":"PP-OCRv5","state":"ready","available":true,"supported":true,"detail":"ready","environment":"/tmp/engine","requirements":["paddleocr==3.7.0"],"installed_versions":{"paddleocr":"3.7.0"},"managed":true}}'
    else
      print -u2 -- "unexpected command: $*"
      exit 2
    fi
    """#
}

private final class LockedCollector<Value: Sendable>: @unchecked Sendable {
  private let lock = NSLock()
  private var storage: [Value] = []

  var values: [Value] {
    lock.lock()
    defer { lock.unlock() }
    return storage
  }

  func append(_ value: Value) {
    lock.lock()
    storage.append(value)
    lock.unlock()
  }
}
