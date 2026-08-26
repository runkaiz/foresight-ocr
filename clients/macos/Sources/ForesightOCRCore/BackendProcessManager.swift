@preconcurrency import Foundation

public enum BackendProcessError: LocalizedError, Sendable, Equatable {
  case alreadyRunning
  case launchFailed(String)
  case commandFailed(status: Int32, message: String)
  case readyTimeout
  case processExitedBeforeReady(Int32)
  case incompatibleProtocol(Int)
  case wrongDocument(String)

  public var errorDescription: String? {
    switch self {
    case .alreadyRunning:
      "已有受管理的 OCR 服务正在运行。"
    case .launchFailed(let message):
      "无法启动 OCR 服务：\(message)"
    case .commandFailed(let status, let message):
      "OCR 命令退出（\(status)）：\(message)"
    case .readyTimeout:
      "OCR 服务未在规定时间内完成启动。"
    case .processExitedBeforeReady(let status):
      "OCR 服务在就绪前退出（\(status)）。"
    case .incompatibleProtocol(let version):
      "OCR 服务协议版本不兼容：\(version)。"
    case .wrongDocument(let documentID):
      "OCR 服务打开了其他文档：\(documentID)。"
    }
  }
}

public enum BackendRuntimeEnvironment {
  public static func values(
    applicationSupportURL: URL? = nil,
    base: [String: String] = ProcessInfo.processInfo.environment
  ) throws -> [String: String] {
    let support =
      try applicationSupportURL
      ?? FileManager.default.url(
        for: .applicationSupportDirectory,
        in: .userDomainMask,
        appropriateFor: nil,
        create: true
      )
    let root = support.appending(path: "Foresight OCR", directoryHint: .isDirectory)
    var environment = base
    environment["FORESIGHT_OCR_ENGINE_HOME"] =
      root
      .appending(path: "engines", directoryHint: .isDirectory).path
    environment["UV_PYTHON_INSTALL_DIR"] =
      root
      .appending(path: "python", directoryHint: .isDirectory).path
    environment["UV_CACHE_DIR"] =
      root
      .appending(path: "cache/uv", directoryHint: .isDirectory).path
    environment["HF_HOME"] =
      root
      .appending(path: "models/huggingface", directoryHint: .isDirectory).path
    environment["PADDLE_HOME"] =
      root
      .appending(path: "models/paddle", directoryHint: .isDirectory).path
    environment["UV_NO_PROGRESS"] = "1"
    return environment
  }
}

public struct ReadyRecordParser: Sendable {
  private var buffer = Data()
  private let decoder = JSONDecoder()

  public init() {}

  public mutating func append(_ data: Data) throws -> ServiceReady? {
    buffer.append(data)
    while let newline = buffer.firstIndex(of: 0x0A) {
      let line = Data(buffer[..<newline])
      buffer.removeSubrange(...newline)
      if let ready = try decodeReady(line) { return ready }
    }
    return nil
  }

  public mutating func finish() throws -> ServiceReady? {
    guard !buffer.isEmpty else { return nil }
    defer { buffer.removeAll(keepingCapacity: false) }
    return try decodeReady(buffer)
  }

  private func decodeReady(_ line: Data) throws -> ServiceReady? {
    guard
      !line.isEmpty,
      let object = try? JSONSerialization.jsonObject(with: line) as? [String: Any],
      object["type"] as? String == "ready"
    else {
      return nil
    }
    return try decoder.decode(ServiceReady.self, from: line)
  }
}

public actor BackendProcessManager {
  private var ownedProcess: Process?
  private var outputCapture: (any ManagedProcessCapture)?

  public init() {}

  public var isRunning: Bool {
    ownedProcess?.isRunning == true
  }

  public func discoverDocuments(
    projectURL: URL,
    executableURL: URL
  ) throws -> DocumentManifest {
    try runJSONCommand(
      executableURL: executableURL,
      arguments: ["documents", "--json"],
      currentDirectoryURL: projectURL,
      invalidJSONMessage: "文档清单不是有效 JSON。"
    )
  }

  public func createProject(
    directoryURL: URL,
    name: String,
    executableURL: URL
  ) throws -> ProjectCreation {
    try runJSONCommand(
      executableURL: executableURL,
      arguments: [
        "project", "init", directoryURL.path(percentEncoded: false),
        "--name", name, "--json",
      ],
      currentDirectoryURL: directoryURL.deletingLastPathComponent(),
      invalidJSONMessage: "项目创建结果不是有效 JSON。"
    )
  }

  public func importPDF(
    projectURL: URL,
    pdfURL: URL,
    documentID: String? = nil,
    executableURL: URL
  ) throws -> PDFImportResult {
    var arguments = [
      "project", "import", pdfURL.path(percentEncoded: false), "--no-report", "--json",
    ]
    if let documentID, !documentID.isEmpty {
      arguments += ["--id", documentID]
    }
    return try runJSONCommand(
      executableURL: executableURL,
      arguments: arguments,
      currentDirectoryURL: projectURL,
      invalidJSONMessage: "PDF 导入结果不是有效 JSON。"
    )
  }

  public func engineManifest(executableURL: URL) throws -> EngineManifest {
    try runJSONCommand(
      executableURL: executableURL,
      arguments: ["engine", "status", "--json"],
      currentDirectoryURL: nil,
      invalidJSONMessage: "OCR 引擎清单不是有效 JSON。"
    )
  }

  @discardableResult
  public func installEngine(
    name: String,
    executableURL: URL,
    onEvent: @escaping @Sendable (EngineInstallEvent) -> Void
  ) async throws -> EngineStatus {
    let terminal = TerminalEventBox<EngineStatus>()
    try await runEventCommand(
      executableURL: executableURL,
      arguments: ["engine", "install", name, "--events"],
      currentDirectoryURL: nil,
      onEvent: { (event: EngineInstallEvent) in
        if case .result(let status) = event { terminal.set(status) }
        onEvent(event)
      }
    )
    guard let result = terminal.value else {
      throw BackendProcessError.commandFailed(
        status: 0,
        message: "OCR 引擎安装完成，但未返回最终状态。"
      )
    }
    return result
  }

  @discardableResult
  public func prepareProject(
    projectURL: URL,
    documentID: String,
    backend: String,
    executableURL: URL,
    onEvent: @escaping @Sendable (ProjectPreparationEvent) -> Void
  ) async throws -> ProjectPreparationResult {
    let terminal = TerminalEventBox<ProjectPreparationResult>()
    try await runEventCommand(
      executableURL: executableURL,
      arguments: [
        "project", "prepare", documentID, "--backend", backend, "--events",
      ],
      currentDirectoryURL: projectURL,
      onEvent: { (event: ProjectPreparationEvent) in
        if case .result(let result) = event { terminal.set(result) }
        onEvent(event)
      }
    )
    guard let result = terminal.value else {
      throw BackendProcessError.commandFailed(
        status: 0,
        message: "项目准备完成，但未返回最终状态。"
      )
    }
    return result
  }

  private func runJSONCommand<Value: Decodable>(
    executableURL: URL,
    arguments: [String],
    currentDirectoryURL: URL?,
    invalidJSONMessage: String
  ) throws -> Value {
    let process = Process()
    let stdout = Pipe()
    let stderr = Pipe()
    process.executableURL = executableURL
    process.arguments = arguments
    process.currentDirectoryURL = currentDirectoryURL
    process.environment = try BackendRuntimeEnvironment.values()
    process.standardOutput = stdout
    process.standardError = stderr
    do {
      try process.run()
    } catch {
      throw BackendProcessError.launchFailed(error.localizedDescription)
    }
    let output = stdout.fileHandleForReading.readDataToEndOfFile()
    process.waitUntilExit()
    let diagnostics = stderr.fileHandleForReading.readDataToEndOfFile()
    guard process.terminationStatus == 0 else {
      throw BackendProcessError.commandFailed(
        status: process.terminationStatus,
        message: String(decoding: diagnostics, as: UTF8.self)
      )
    }
    do {
      return try JSONDecoder().decode(Value.self, from: output)
    } catch {
      throw BackendProcessError.commandFailed(
        status: process.terminationStatus,
        message: invalidJSONMessage
      )
    }
  }

  private func runEventCommand<Event: Decodable & Sendable>(
    executableURL: URL,
    arguments: [String],
    currentDirectoryURL: URL?,
    onEvent: @escaping @Sendable (Event) -> Void
  ) async throws {
    guard ownedProcess == nil else {
      throw BackendProcessError.alreadyRunning
    }
    let process = Process()
    let stdout = Pipe()
    let stderr = Pipe()
    let capture = EventProcessCapture<Event>(onEvent: onEvent)
    process.executableURL = executableURL
    process.arguments = arguments
    process.currentDirectoryURL = currentDirectoryURL
    process.environment = try BackendRuntimeEnvironment.values()
    process.standardOutput = stdout
    process.standardError = stderr
    stdout.fileHandleForReading.readabilityHandler = { handle in
      let data = handle.availableData
      if data.isEmpty {
        capture.finishOutput()
      } else {
        capture.receive(data)
      }
    }
    stderr.fileHandleForReading.readabilityHandler = { handle in
      let data = handle.availableData
      if !data.isEmpty { capture.receiveDiagnostic(data) }
    }
    process.terminationHandler = { terminated in
      capture.processExited(status: terminated.terminationStatus)
    }
    do {
      try process.run()
    } catch {
      stdout.fileHandleForReading.readabilityHandler = nil
      stderr.fileHandleForReading.readabilityHandler = nil
      throw BackendProcessError.launchFailed(error.localizedDescription)
    }
    ownedProcess = process
    outputCapture = capture
    do {
      try await capture.waitForExit()
      stdout.fileHandleForReading.readabilityHandler = nil
      stderr.fileHandleForReading.readabilityHandler = nil
      ownedProcess = nil
      outputCapture = nil
    } catch {
      stop()
      throw error
    }
  }

  public func start(
    projectURL: URL,
    executableURL: URL,
    documentID: String,
    timeout: Duration = .seconds(15)
  ) async throws -> ServiceReady {
    guard ownedProcess == nil else {
      throw BackendProcessError.alreadyRunning
    }

    let process = Process()
    let stdout = Pipe()
    let stderr = Pipe()
    let capture = ProcessOutputCapture()
    process.executableURL = executableURL
    process.arguments = [
      "review",
      documentID,
      "--no-open",
      "--port",
      "0",
      "--ready-json",
    ]
    process.currentDirectoryURL = projectURL
    process.environment = try BackendRuntimeEnvironment.values()
    process.standardOutput = stdout
    process.standardError = stderr

    stdout.fileHandleForReading.readabilityHandler = { handle in
      let data = handle.availableData
      if data.isEmpty {
        capture.finishOutput()
      } else {
        capture.receive(data)
      }
    }
    stderr.fileHandleForReading.readabilityHandler = { handle in
      let data = handle.availableData
      if !data.isEmpty { capture.receiveDiagnostic(data) }
    }
    process.terminationHandler = { terminated in
      capture.processExited(status: terminated.terminationStatus)
    }

    do {
      try process.run()
    } catch {
      stdout.fileHandleForReading.readabilityHandler = nil
      stderr.fileHandleForReading.readabilityHandler = nil
      throw BackendProcessError.launchFailed(error.localizedDescription)
    }
    ownedProcess = process
    outputCapture = capture

    let timeoutTask = Task {
      try? await Task.sleep(for: timeout)
      if !Task.isCancelled { capture.timeout() }
    }
    defer { timeoutTask.cancel() }

    do {
      let ready = try await capture.waitForReady()
      guard ready.protocolVersion == 1 else {
        throw BackendProcessError.incompatibleProtocol(ready.protocolVersion)
      }
      guard ready.documentID == documentID else {
        throw BackendProcessError.wrongDocument(ready.documentID)
      }
      _ = try ForesightAPI(baseURL: ready.url)
      return ready
    } catch {
      stop()
      throw error
    }
  }

  public func logs() -> [String] {
    outputCapture?.logs() ?? []
  }

  public func stop() {
    guard let process = ownedProcess else { return }
    outputCapture?.cancel()
    (process.standardOutput as? Pipe)?.fileHandleForReading.readabilityHandler = nil
    (process.standardError as? Pipe)?.fileHandleForReading.readabilityHandler = nil
    if process.isRunning { process.terminate() }
    ownedProcess = nil
    outputCapture = nil
  }
}

private final class TerminalEventBox<Value: Sendable>: @unchecked Sendable {
  private let lock = NSLock()
  private var storage: Value?

  var value: Value? {
    lock.lock()
    defer { lock.unlock() }
    return storage
  }

  func set(_ value: Value) {
    lock.lock()
    storage = value
    lock.unlock()
  }
}

private protocol ManagedProcessCapture: AnyObject, Sendable {
  func logs() -> [String]
  func cancel()
}

private final class ProcessOutputCapture: ManagedProcessCapture, @unchecked Sendable {
  private let lock = NSLock()
  private var parser = ReadyRecordParser()
  private var result: Result<ServiceReady, Error>?
  private var continuation: CheckedContinuation<ServiceReady, Error>?
  private var recentLogs: [String] = []

  func waitForReady() async throws -> ServiceReady {
    try await withCheckedThrowingContinuation { continuation in
      lock.lock()
      if let result {
        lock.unlock()
        continuation.resume(with: result)
        return
      }
      self.continuation = continuation
      lock.unlock()
    }
  }

  func receive(_ data: Data) {
    lock.lock()
    appendLogData(data)
    do {
      if let ready = try parser.append(data) {
        complete(.success(ready))
      }
    } catch {
      complete(.failure(error))
    }
    lock.unlock()
  }

  func receiveDiagnostic(_ data: Data) {
    lock.lock()
    appendLogData(data)
    lock.unlock()
  }

  func finishOutput() {
    lock.lock()
    do {
      if let ready = try parser.finish() {
        complete(.success(ready))
      }
    } catch {
      complete(.failure(error))
    }
    lock.unlock()
  }

  func processExited(status: Int32) {
    lock.lock()
    complete(.failure(BackendProcessError.processExitedBeforeReady(status)))
    lock.unlock()
  }

  func timeout() {
    lock.lock()
    complete(.failure(BackendProcessError.readyTimeout))
    lock.unlock()
  }

  func cancel() {
    lock.lock()
    complete(.failure(BackendProcessError.launchFailed("任务已取消。")))
    lock.unlock()
  }

  func logs() -> [String] {
    lock.lock()
    defer { lock.unlock() }
    return recentLogs
  }

  private func complete(_ newResult: Result<ServiceReady, Error>) {
    guard result == nil else { return }
    result = newResult
    let pending = continuation
    continuation = nil
    pending?.resume(with: newResult)
  }

  private func appendLogData(_ data: Data) {
    let text = String(decoding: data, as: UTF8.self)
    recentLogs.append(contentsOf: text.split(separator: "\n").map(String.init))
    if recentLogs.count > 200 {
      recentLogs.removeFirst(recentLogs.count - 200)
    }
  }
}

private final class EventProcessCapture<Event: Decodable & Sendable>:
  ManagedProcessCapture, @unchecked Sendable
{
  private let lock = NSLock()
  private var parser = JSONLineDecoder<Event>()
  private let onEvent: @Sendable (Event) -> Void
  private var result: Result<Void, Error>?
  private var continuation: CheckedContinuation<Void, Error>?
  private var recentLogs: [String] = []
  private var processStatus: Int32?
  private var outputFinished = false

  init(onEvent: @escaping @Sendable (Event) -> Void) {
    self.onEvent = onEvent
  }

  func waitForExit() async throws {
    try await withCheckedThrowingContinuation { continuation in
      lock.lock()
      if let result {
        lock.unlock()
        continuation.resume(with: result)
        return
      }
      self.continuation = continuation
      lock.unlock()
    }
  }

  func receive(_ data: Data) {
    lock.lock()
    appendLogData(data)
    let values: [Event]
    do {
      values = try parser.append(data)
    } catch {
      complete(.failure(error))
      lock.unlock()
      return
    }
    lock.unlock()
    values.forEach(onEvent)
  }

  func receiveDiagnostic(_ data: Data) {
    lock.lock()
    appendLogData(data)
    lock.unlock()
  }

  func finishOutput() {
    lock.lock()
    let values: [Event]
    do {
      values = try parser.finish()
    } catch {
      values = []
      complete(.failure(error))
    }
    lock.unlock()
    values.forEach(onEvent)
    lock.lock()
    outputFinished = true
    completeIfFinished()
    lock.unlock()
  }

  func processExited(status: Int32) {
    lock.lock()
    processStatus = status
    completeIfFinished()
    lock.unlock()
  }

  func cancel() {
    lock.lock()
    complete(.failure(BackendProcessError.launchFailed("任务已取消。")))
    lock.unlock()
  }

  func logs() -> [String] {
    lock.lock()
    defer { lock.unlock() }
    return recentLogs
  }

  private func completeIfFinished() {
    guard outputFinished, let processStatus else { return }
    if processStatus == 0 {
      complete(.success(()))
    } else {
      complete(
        .failure(
          BackendProcessError.commandFailed(
            status: processStatus,
            message: recentLogs.suffix(20).joined(separator: "\n")
          )
        )
      )
    }
  }

  private func complete(_ newResult: Result<Void, Error>) {
    guard result == nil else { return }
    result = newResult
    let pending = continuation
    continuation = nil
    pending?.resume(with: newResult)
  }

  private func appendLogData(_ data: Data) {
    let text = String(decoding: data, as: UTF8.self)
    recentLogs.append(contentsOf: text.split(separator: "\n").map(String.init))
    if recentLogs.count > 200 {
      recentLogs.removeFirst(recentLogs.count - 200)
    }
  }
}
