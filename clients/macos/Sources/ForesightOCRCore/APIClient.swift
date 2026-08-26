import Foundation

public enum ForesightAPIError: LocalizedError, Sendable, Equatable {
  case invalidBaseURL
  case nonLoopbackService
  case invalidResponse
  case service(status: Int, message: String)

  public var errorDescription: String? {
    switch self {
    case .invalidBaseURL:
      "服务地址无效。"
    case .nonLoopbackService:
      "只允许连接本机回环地址。"
    case .invalidResponse:
      "服务返回了无法识别的响应。"
    case .service(let status, let message):
      "服务错误 \(status)：\(message)"
    }
  }
}

public actor ForesightAPI {
  public nonisolated let baseURL: URL

  private let session: URLSession
  private let decoder = JSONDecoder()
  private let encoder = JSONEncoder()

  public init(baseURL: URL, session: URLSession = .shared) throws {
    guard
      let components = URLComponents(url: baseURL, resolvingAgainstBaseURL: false),
      components.scheme == "http",
      let host = components.host?.lowercased(),
      ["127.0.0.1", "localhost", "::1"].contains(host),
      components.port != nil
    else {
      if baseURL.scheme == "http" {
        throw ForesightAPIError.nonLoopbackService
      }
      throw ForesightAPIError.invalidBaseURL
    }
    var normalized = components
    normalized.path = ""
    normalized.query = nil
    normalized.fragment = nil
    guard let normalizedURL = normalized.url else {
      throw ForesightAPIError.invalidBaseURL
    }
    self.baseURL = normalizedURL
    self.session = session
  }

  public func bootstrap() async throws -> ReviewBootstrap {
    try await get("/api/pages")
  }

  public func page(_ page: Int, spread: Int = 1) async throws -> PageSpread {
    try await get(
      "/api/page",
      query: [
        URLQueryItem(name: "page", value: String(page)),
        URLQueryItem(name: "spread", value: String(min(3, max(1, spread)))),
      ]
    )
  }

  public func cropImage(
    regionUID: String,
    variant: ImageVariant
  ) async throws -> CropImage {
    try await get(
      "/api/crop-image",
      query: [
        URLQueryItem(name: "region_uid", value: regionUID),
        URLQueryItem(name: "variant", value: variant.rawValue),
      ]
    )
  }

  public func pageImage(
    page: Int,
    variant: ImageVariant
  ) async throws -> PageImage {
    try await get(
      "/api/page-image",
      query: [
        URLQueryItem(name: "page", value: String(page)),
        URLQueryItem(name: "variant", value: variant.rawValue),
      ]
    )
  }

  public func image(pathToken: String) async throws -> Data {
    let request = try makeRequest(
      path: "/img",
      query: [URLQueryItem(name: "path", value: pathToken)]
    )
    return try await responseData(for: request)
  }

  public func saveCorrection(
    entry: ReviewEntry,
    fields: CorrectionFields,
    transcription: String?,
    unreadable: Bool,
    note: String?
  ) async throws -> CorrectionResponse {
    try await post(
      "/api/correction",
      body: CorrectionMutation(
        pageIndex: entry.pageIndex,
        bandLabel: entry.bandLabel,
        entryIndex: entry.entryIndex,
        role: entry.role,
        fields: entry.role == "entry" ? fields : nil,
        transcription: entry.role == "entry" ? nil : transcription,
        unreadable: unreadable,
        note: note,
        action: nil
      )
    )
  }

  public func unconfirm(_ entry: ReviewEntry) async throws -> CorrectionResponse {
    try await post(
      "/api/correction",
      body: CorrectionMutation(
        pageIndex: entry.pageIndex,
        bandLabel: entry.bandLabel,
        entryIndex: entry.entryIndex,
        role: entry.role,
        fields: nil,
        transcription: nil,
        unreadable: nil,
        note: nil,
        action: "unconfirm"
      )
    )
  }

  public func setPageIgnored(
    page: Int,
    ignored: Bool
  ) async throws -> PageIgnoreResponse {
    try await post(
      "/api/page-ignore",
      body: PageIgnoreMutation(pageIndex: page, ignored: ignored)
    )
  }

  public func exportZIP() async throws -> Data {
    let request = try makeRequest(path: "/api/export.zip")
    return try await responseData(for: request)
  }

  public func exportFiles() async throws -> [ExportFile] {
    let response: ExportBundle = try await post("/api/export", body: EmptyMutation())
    return response.files
  }

  public func numeral(label: String, digits: String) async throws -> String {
    let response: NumeralResponse = try await get(
      "/api/numeral",
      query: [
        URLQueryItem(name: "label", value: label),
        URLQueryItem(name: "n", value: digits),
      ]
    )
    return response.text
  }

  public func documentOCRStatus() async throws -> ReviewJob {
    let response: ReviewJobEnvelope = try await get("/api/reocr-all")
    return response.job
  }

  public func startDocumentOCR(force: Bool = false) async throws -> ReviewJobStart {
    try await post(
      "/api/reocr-all",
      body: DocumentOCRMutation(
        backend: "paddleocr_vl",
        variant: ImageVariant.watermark.rawValue,
        force: force
      )
    )
  }

  public func learningStatus() async throws -> LearningSnapshot {
    try await get("/api/learn-ocr")
  }

  public func runLearningAnalysis() async throws -> LearningSnapshot {
    try await post("/api/learn-ocr", body: EmptyMutation())
  }

  public func learningReportMarkdown() async throws -> String {
    let request = try makeRequest(path: "/api/learn-ocr/report")
    let data = try await responseData(for: request)
    guard let text = String(data: data, encoding: .utf8) else {
      throw ForesightAPIError.invalidResponse
    }
    return text
  }

  public func reOCRPage(
    _ page: Int,
    onEvent: @escaping @Sendable (RecognitionEvent) -> Void
  ) async throws {
    var request = try makeRequest(path: "/api/reocr")
    request.httpMethod = "POST"
    request.setValue("application/json", forHTTPHeaderField: "Content-Type")
    request.httpBody = try encoder.encode(
      PageReOCRMutation(
        page: page,
        backend: "paddleocr_vl",
        variant: ImageVariant.watermark.rawValue,
        stream: true
      )
    )

    let (bytes, response) = try await session.bytes(for: request)
    guard let http = response as? HTTPURLResponse else {
      throw ForesightAPIError.invalidResponse
    }
    guard (200..<300).contains(http.statusCode) else {
      var body = Data()
      for try await byte in bytes { body.append(byte) }
      let message =
        (try? decoder.decode(ErrorEnvelope.self, from: body).error)
        ?? HTTPURLResponse.localizedString(forStatusCode: http.statusCode)
      throw ForesightAPIError.service(status: http.statusCode, message: message)
    }

    var streamDecoder = NDJSONDecoder()
    var chunk = Data()
    chunk.reserveCapacity(4_096)
    for try await byte in bytes {
      chunk.append(byte)
      if byte == 0x0A || chunk.count >= 4_096 {
        for event in try streamDecoder.append(chunk) {
          if event.type == "error" {
            throw ForesightAPIError.service(
              status: 500,
              message: event.error ?? "页面重新识别失败。"
            )
          }
          onEvent(event)
        }
        chunk.removeAll(keepingCapacity: true)
      }
    }
    if !chunk.isEmpty {
      for event in try streamDecoder.append(chunk) { onEvent(event) }
    }
    for event in try streamDecoder.finish() { onEvent(event) }
  }

  public func combPreview(
    page: Int,
    phase: Double,
    pitch: Double?,
    snap: Bool,
    textLeft: Double?,
    textRight: Double?,
    boundaryOverrides: [Int: Double]
  ) async throws -> CombPreview {
    let manual = Dictionary(
      uniqueKeysWithValues: boundaryOverrides.map {
        (String($0.key), $0.value)
      })
    let manualData = try encoder.encode(manual)
    guard let manualJSON = String(data: manualData, encoding: .utf8) else {
      throw ForesightAPIError.invalidResponse
    }
    var query = [
      URLQueryItem(name: "page", value: String(page)),
      URLQueryItem(name: "phase", value: String(phase)),
      URLQueryItem(name: "snap", value: snap ? "1" : "0"),
      URLQueryItem(name: "manual", value: manualJSON),
    ]
    if let pitch { query.append(URLQueryItem(name: "pitch", value: String(pitch))) }
    if let textLeft {
      query.append(URLQueryItem(name: "left", value: String(textLeft)))
    }
    if let textRight {
      query.append(URLQueryItem(name: "right", value: String(textRight)))
    }
    return try await get("/api/comb", query: query)
  }

  public func recutPage(
    page: Int,
    phase: Double,
    pitch: Double?,
    snap: Bool,
    textLeft: Double?,
    textRight: Double?,
    boundaryOverrides: [Int: Double],
    onEvent: @escaping @Sendable (RecognitionEvent) -> Void
  ) async throws {
    let manual = Dictionary(
      uniqueKeysWithValues: boundaryOverrides.map {
        (String($0.key), $0.value)
      })
    var request = try makeRequest(path: "/api/recut")
    request.httpMethod = "POST"
    request.setValue("application/json", forHTTPHeaderField: "Content-Type")
    request.httpBody = try encoder.encode(
      RecutMutation(
        page: page,
        phaseOffset: phase,
        pitch: pitch,
        snap: snap,
        textLeft: textLeft,
        textRight: textRight,
        boundaryOverrides: manual,
        reocr: true,
        backend: "paddleocr_vl",
        stream: true
      )
    )

    let (bytes, response) = try await session.bytes(for: request)
    guard let http = response as? HTTPURLResponse else {
      throw ForesightAPIError.invalidResponse
    }
    guard (200..<300).contains(http.statusCode) else {
      var body = Data()
      for try await byte in bytes { body.append(byte) }
      let message =
        (try? decoder.decode(ErrorEnvelope.self, from: body).error)
        ?? HTTPURLResponse.localizedString(forStatusCode: http.statusCode)
      throw ForesightAPIError.service(status: http.statusCode, message: message)
    }

    var streamDecoder = NDJSONDecoder()
    var chunk = Data()
    chunk.reserveCapacity(4_096)
    for try await byte in bytes {
      chunk.append(byte)
      if byte == 0x0A || chunk.count >= 4_096 {
        for event in try streamDecoder.append(chunk) {
          if event.type == "error" {
            throw ForesightAPIError.service(
              status: 500,
              message: event.error ?? "页面重新切分失败。"
            )
          }
          onEvent(event)
        }
        chunk.removeAll(keepingCapacity: true)
      }
    }
    if !chunk.isEmpty {
      for event in try streamDecoder.append(chunk) { onEvent(event) }
    }
    for event in try streamDecoder.finish() { onEvent(event) }
  }

  private func get<Response: Decodable & Sendable>(
    _ path: String,
    query: [URLQueryItem] = []
  ) async throws -> Response {
    let request = try makeRequest(path: path, query: query)
    let data = try await responseData(for: request)
    do {
      return try decoder.decode(Response.self, from: data)
    } catch {
      throw ForesightAPIError.invalidResponse
    }
  }

  private func post<Body: Encodable & Sendable, Response: Decodable & Sendable>(
    _ path: String,
    body: Body
  ) async throws -> Response {
    var request = try makeRequest(path: path)
    request.httpMethod = "POST"
    request.setValue("application/json", forHTTPHeaderField: "Content-Type")
    request.httpBody = try encoder.encode(body)
    let data = try await responseData(for: request)
    do {
      return try decoder.decode(Response.self, from: data)
    } catch {
      throw ForesightAPIError.invalidResponse
    }
  }

  private func makeRequest(
    path: String,
    query: [URLQueryItem] = []
  ) throws -> URLRequest {
    guard
      var components = URLComponents(
        url: baseURL,
        resolvingAgainstBaseURL: false
      )
    else {
      throw ForesightAPIError.invalidBaseURL
    }
    components.path = path
    components.queryItems = query.isEmpty ? nil : query
    guard let url = components.url else {
      throw ForesightAPIError.invalidBaseURL
    }
    var request = URLRequest(url: url)
    request.cachePolicy = .reloadIgnoringLocalCacheData
    request.timeoutInterval = 60
    return request
  }

  private func responseData(for request: URLRequest) async throws -> Data {
    let (data, response) = try await session.data(for: request)
    guard let http = response as? HTTPURLResponse else {
      throw ForesightAPIError.invalidResponse
    }
    guard (200..<300).contains(http.statusCode) else {
      let message =
        (try? decoder.decode(ErrorEnvelope.self, from: data).error)
        ?? HTTPURLResponse.localizedString(forStatusCode: http.statusCode)
      throw ForesightAPIError.service(status: http.statusCode, message: message)
    }
    return data
  }
}

private struct CorrectionMutation: Encodable, Sendable {
  let pageIndex: Int
  let bandLabel: String
  let entryIndex: Int
  let role: String
  let fields: CorrectionFields?
  let transcription: String?
  let unreadable: Bool?
  let note: String?
  let action: String?

  enum CodingKeys: String, CodingKey {
    case role, fields, transcription, unreadable, note, action
    case pageIndex = "page_index"
    case bandLabel = "band_label"
    case entryIndex = "entry_index"
  }
}

private struct PageIgnoreMutation: Encodable, Sendable {
  let pageIndex: Int
  let ignored: Bool

  enum CodingKeys: String, CodingKey {
    case ignored
    case pageIndex = "page_index"
  }
}

private struct EmptyMutation: Encodable, Sendable {}

private struct DocumentOCRMutation: Encodable, Sendable {
  let backend: String
  let variant: String
  let force: Bool
}

private struct PageReOCRMutation: Encodable, Sendable {
  let page: Int
  let backend: String
  let variant: String
  let stream: Bool
}

private struct RecutMutation: Encodable, Sendable {
  let page: Int
  let phaseOffset: Double
  let pitch: Double?
  let snap: Bool
  let textLeft: Double?
  let textRight: Double?
  let boundaryOverrides: [String: Double]
  let reocr: Bool
  let backend: String
  let stream: Bool

  enum CodingKeys: String, CodingKey {
    case page, pitch, snap, reocr, backend, stream
    case phaseOffset = "phase_offset"
    case textLeft = "text_left"
    case textRight = "text_right"
    case boundaryOverrides = "boundary_overrides"
  }
}
