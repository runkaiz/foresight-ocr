import Foundation
import XCTest

@testable import ForesightOCRCore

final class StreamDecoderTests: XCTestCase {
  func testGenericJSONLineDecoderPreservesSetupEventsAcrossUTF8Boundaries() throws {
    let payload = """
      {"type":"project_prepare","document_id":"宗譜","stage":"inspect_pdf","label":"检查 PDF 结构","status":"completed","index":2,"total":7,"detail":null}
      {"type":"project_prepare","document_id":"宗譜","stage":"extract_pages","label":"提取原始页面图像","status":"started","index":3,"total":7,"detail":null}
      """ + "\n"
    let bytes = Data(payload.utf8)

    for split in 1..<bytes.count {
      var decoder = JSONLineDecoder<ProjectPreparationEvent>()
      var events = try decoder.append(Data(bytes[..<split]))
      events += try decoder.append(Data(bytes[split...]))
      events += try decoder.finish()
      XCTAssertEqual(events.count, 2, "split at \(split)")
      guard case .progress(let first) = events[0] else {
        return XCTFail("expected progress event")
      }
      XCTAssertEqual(first.documentID, "宗譜")
      XCTAssertEqual(first.label, "检查 PDF 结构")
    }
  }

  func testNDJSONDecodesAcrossEveryUTF8ByteBoundary() throws {
    let payload = """
      {"type":"progress","stage":"识别宗譜","completed":1,"total":2,"region_uid":"區域一"}
      {"type":"result","ok":true}
      """ + "\n"
    let bytes = Data(payload.utf8)

    for split in 1..<bytes.count {
      var decoder = NDJSONDecoder()
      var events = try decoder.append(bytes[..<split])
      events += try decoder.append(bytes[split...])
      events += try decoder.finish()
      XCTAssertEqual(events.count, 2, "split at \(split)")
      XCTAssertEqual(events[0].stage, "识别宗譜")
      XCTAssertEqual(events[0].regionUID, "區域一")
      XCTAssertEqual(events[1].ok, true)
    }
  }

  func testReadyRecordIgnoresDiagnosticsAndDecodesAcrossEveryBoundary() throws {
    let payload = """
      loading profile…
      {"type":"ready","protocol_version":1,"url":"http://127.0.0.1:49152","document_id":"丙辰庶富教1"}
      review server started
      """ + "\n"
    let bytes = Data(payload.utf8)

    for split in 1..<bytes.count {
      var parser = ReadyRecordParser()
      let first = try parser.append(bytes[..<split])
      let second = try parser.append(bytes[split...])
      let finished = try parser.finish()
      let ready = first ?? second ?? finished
      XCTAssertEqual(ready?.protocolVersion, 1, "split at \(split)")
      XCTAssertEqual(ready?.url.absoluteString, "http://127.0.0.1:49152")
      XCTAssertEqual(ready?.documentID, "丙辰庶富教1")
    }
  }
}
