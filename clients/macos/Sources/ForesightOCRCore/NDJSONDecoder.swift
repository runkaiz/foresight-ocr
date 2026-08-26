import Foundation

public struct JSONLineDecoder<Value: Decodable & Sendable>: Sendable {
  private var buffer = Data()
  private let decoder = JSONDecoder()

  public init() {}

  public mutating func append(_ data: Data) throws -> [Value] {
    buffer.append(data)
    var values: [Value] = []
    while let newline = buffer.firstIndex(of: 0x0A) {
      let line = buffer[..<newline]
      buffer.removeSubrange(...newline)
      if line.isEmpty { continue }
      values.append(try decoder.decode(Value.self, from: line))
    }
    return values
  }

  public mutating func finish() throws -> [Value] {
    guard !buffer.isEmpty else { return [] }
    defer { buffer.removeAll(keepingCapacity: false) }
    return [try decoder.decode(Value.self, from: buffer)]
  }
}

public struct RecognitionEvent: Codable, Sendable, Equatable {
  public let type: String
  public let stage: String?
  public let completed: Int?
  public let total: Int?
  public let page: Int?
  public let regionUID: String?
  public let ok: Bool?
  public let error: String?

  enum CodingKeys: String, CodingKey {
    case type, stage, completed, total, page, ok, error
    case regionUID = "region_uid"
  }
}

public struct NDJSONDecoder: Sendable {
  private var buffer = Data()
  private let decoder = JSONDecoder()

  public init() {}

  public mutating func append(_ data: Data) throws -> [RecognitionEvent] {
    buffer.append(data)
    var events: [RecognitionEvent] = []
    while let newline = buffer.firstIndex(of: 0x0A) {
      let line = buffer[..<newline]
      buffer.removeSubrange(...newline)
      if line.isEmpty { continue }
      events.append(try decoder.decode(RecognitionEvent.self, from: line))
    }
    return events
  }

  public mutating func finish() throws -> [RecognitionEvent] {
    guard !buffer.isEmpty else { return [] }
    defer { buffer.removeAll(keepingCapacity: false) }
    return [try decoder.decode(RecognitionEvent.self, from: buffer)]
  }
}
