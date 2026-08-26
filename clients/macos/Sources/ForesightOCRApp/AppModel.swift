import AppKit
import Combine
import ForesightOCRCore
import Foundation
import UniformTypeIdentifiers

enum OnboardingStep: Equatable {
  case welcome
  case existingProject
  case newProject
  case engine
  case preparation
}

@MainActor
final class ReviewAppModel: ObservableObject {
  @Published var onboardingStep: OnboardingStep = .welcome
  @Published var serviceURL = "http://127.0.0.1:8765"
  @Published private(set) var projectURL: URL?
  @Published private(set) var documentManifest: DocumentManifest?
  @Published var selectedDocumentID: String?
  @Published private(set) var bootstrap: ReviewBootstrap?
  @Published private(set) var workspaceReady = false
  @Published private(set) var spread: PageSpread?
  @Published var selectedPage: Int?
  @Published var selectedEntryID: String?
  @Published private(set) var contextImage: NSImage?
  @Published private(set) var contextImageMetadata: PageImage?
  @Published private(set) var cropImage: NSImage?
  @Published private(set) var cropMetadata: CropImage?
  @Published var imageVariant: ImageVariant = .watermark
  @Published var contextImageVariant: ImageVariant = .watermark
  @Published var draft = CorrectionFields()
  @Published var freeformTranscription = ""
  @Published var note = ""
  @Published var unreadable = false
  @Published var isConnecting = false
  @Published var isDiscoveringProject = false
  @Published var isStartingBackend = false
  @Published var isLoadingPage = false
  @Published var isSaving = false
  @Published var isMutatingPage = false
  @Published var isExporting = false
  @Published var showIgnoreConfirmation = false
  @Published var showPageOCRConfirmation = false
  @Published var showDocumentOCRConfirmation = false
  @Published var showDocumentOCRPanel = false
  @Published var showLearningPanel = false
  @Published var showLearningReport = false
  @Published var showLayoutRepair = false
  @Published var showRecutConfirmation = false
  @Published var showDiscardDraftConfirmation = false
  @Published var isReOCRingPage = false
  @Published var isStartingDocumentOCR = false
  @Published var isRunningLearning = false
  @Published var isLoadingCombPreview = false
  @Published var isApplyingRecut = false
  @Published var isConvertingNumeral = false
  @Published var pageOCREvent: RecognitionEvent?
  @Published private(set) var documentOCRJob: ReviewJob?
  @Published private(set) var learningSnapshot: LearningSnapshot?
  @Published private(set) var learningReportMarkdown = ""
  @Published private(set) var combPreview: CombPreview?
  @Published var combPhase = 0.0
  @Published var combPitch = 0.0
  @Published var combSnap = true
  @Published var combTextLeft = 0.0
  @Published var combTextRight = 0.0
  @Published var combBoundaryOverrides: [Int: Double] = [:]
  @Published var errorMessage: String?
  @Published var statusMessage = "未连接"
  @Published var showInspector = true
  @Published var showContext = true
  @Published var showSidebar = true
  @Published var cropZoom: CGFloat = 1
  @Published var cropZoomCommand = 0
  @Published var cropFitCommand = 0
  @Published var newProjectName = ""
  @Published var newDocumentID = ""
  @Published private(set) var newProjectParentURL: URL?
  @Published private(set) var importPDFURL: URL?
  @Published private(set) var importedPDF: PDFImportResult?
  @Published private(set) var engineManifest: EngineManifest?
  @Published var selectedEngineName: String?
  @Published private(set) var engineInstallProgress: EngineInstallProgress?
  @Published private(set) var preparationProgress: [String: ProjectPreparationProgress] = [:]
  @Published private(set) var preparationResult: ProjectPreparationResult?
  @Published var isCreatingProject = false
  @Published var isLoadingEngineManifest = false
  @Published var isInstallingEngine = false
  @Published var isPreparingProject = false

  private var api: ForesightAPI?
  private let backendProcess = BackendProcessManager()
  private var managedService = false
  private var pageRequestID = UUID()
  private var cropRequestID = UUID()
  private var contextRequestID = UUID()
  private var documentOCRPollingTask: Task<Void, Never>?
  private var baselineDraft = CorrectionFields()
  private var baselineFreeformTranscription = ""
  private var baselineNote = ""
  private var baselineUnreadable = false
  private var pendingNavigation: (() -> Void)?
  private var engineReturnStep: OnboardingStep = .newProject

  var selectedEntry: ReviewEntry? {
    guard let selectedEntryID else { return nil }
    return spread?.pages
      .lazy
      .flatMap(\.entries)
      .first(where: { $0.id == selectedEntryID })
  }

  var selectedSheet: ReviewSheet? {
    guard let selectedPage else { return spread?.pages.first }
    return spread?.pages.first(where: { $0.page == selectedPage })
      ?? spread?.pages.first
  }

  var supportsCropVariants: Bool {
    bootstrap?.capabilities.contains("crop_image_variants") == true
  }

  var supportsPageImageVariants: Bool {
    bootstrap?.capabilities.contains("page_image_watermark") == true
  }

  var progressFraction: Double {
    guard let progress = bootstrap?.progress, progress.entries > 0 else { return 0 }
    return Double(progress.reviewed) / Double(progress.entries)
  }

  var selectedDocument: DocumentSummary? {
    guard let selectedDocumentID else { return nil }
    return documentManifest?.documents.first(where: { $0.id == selectedDocumentID })
  }

  var hasUnsavedDraft: Bool {
    selectedEntry != nil
      && (draft != baselineDraft || note != baselineNote || unreadable != baselineUnreadable
        || freeformTranscription != baselineFreeformTranscription)
  }

  var ownsManagedService: Bool { managedService }

  var hasActiveManagedOperation: Bool {
    managedService || isCreatingProject || isInstallingEngine || isPreparingProject
  }

  var shouldWarnBeforeTermination: Bool {
    if isCreatingProject || isInstallingEngine || isPreparingProject { return true }
    guard let status = documentOCRJob?.status else { return false }
    return ["queued", "running"].contains(status)
  }

  var newProjectURL: URL? {
    guard let newProjectParentURL else { return nil }
    return DesktopOnboardingPolicy.projectDirectory(
      parent: newProjectParentURL,
      name: newProjectName
    )
  }

  var canCreateProject: Bool {
    newProjectURL != nil
      && importPDFURL != nil
      && !newDocumentID.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
      && !isCreatingProject
  }

  var selectedEngine: EngineStatus? {
    guard let selectedEngineName else { return nil }
    return engineManifest?.engines.first(where: { $0.name == selectedEngineName })
  }

  var completedPreparationStageCount: Int {
    preparationProgress.values.count(where: { $0.status == "completed" })
  }

  func shutdown() async {
    documentOCRPollingTask?.cancel()
    managedService = false
    await backendProcess.stop()
  }

  func returnToWelcome() {
    guard !isCreatingProject, !isInstallingEngine, !isPreparingProject else { return }
    onboardingStep = .welcome
  }

  func beginOpeningExistingProject() {
    guard !isCreatingProject, !isInstallingEngine, !isPreparingProject else { return }
    onboardingStep = .existingProject
    chooseProject()
  }

  func beginNewProject() {
    guard !isCreatingProject, !isInstallingEngine, !isPreparingProject else { return }
    onboardingStep = .newProject
    if newProjectParentURL == nil {
      newProjectParentURL =
        FileManager.default.urls(
          for: .documentDirectory,
          in: .userDomainMask
        ).first
    }
  }

  func chooseImportPDF() {
    let panel = NSOpenPanel()
    panel.title = "选择源 PDF"
    panel.message = "请选择要保留到可移植项目中的原始 PDF。"
    panel.prompt = "导入 PDF"
    panel.allowedContentTypes = [.pdf]
    panel.canChooseFiles = true
    panel.canChooseDirectories = false
    panel.allowsMultipleSelection = false
    guard panel.runModal() == .OK, let url = panel.url else { return }
    setImportPDF(url)
  }

  func acceptDroppedPDFs(_ urls: [URL]) -> Bool {
    let pdfs = urls.filter {
      $0.isFileURL && $0.pathExtension.caseInsensitiveCompare("pdf") == .orderedSame
    }
    guard pdfs.count == 1, let pdf = pdfs.first else {
      errorMessage = "请一次拖入一个 PDF 文件。"
      return false
    }
    setImportPDF(pdf)
    return true
  }

  func chooseNewProjectParent() {
    let panel = NSOpenPanel()
    panel.title = "选择项目存储位置"
    panel.message = "Foresight OCR 会在所选位置创建一个以项目命名的新文件夹。"
    panel.prompt = "选择位置"
    panel.canChooseFiles = false
    panel.canChooseDirectories = true
    panel.canCreateDirectories = true
    panel.allowsMultipleSelection = false
    guard panel.runModal() == .OK, let url = panel.url else { return }
    newProjectParentURL = url
  }

  func createProjectAndContinue() {
    Task { await createProjectAndImportNow() }
  }

  func prepareSelectedExistingDocument() {
    guard projectURL != nil, selectedDocumentID != nil else { return }
    Task { await showEngineSetup(returnTo: .existingProject) }
  }

  func backFromEngineSetup() {
    guard !isInstallingEngine, !isPreparingProject else { return }
    onboardingStep = engineReturnStep
  }

  func returnToEngineSetup() {
    guard !isPreparingProject else { return }
    onboardingStep = .engine
  }

  func selectEngine(_ name: String) {
    guard engineManifest?.engines.first(where: { $0.name == name })?.supported == true else {
      return
    }
    selectedEngineName = name
    engineInstallProgress = nil
  }

  func installEngineAndContinue() {
    Task { await installEngineAndContinueNow() }
  }

  func retryPreparation() {
    Task { await prepareProjectNow() }
  }

  func chooseProject() {
    let panel = NSOpenPanel()
    panel.title = "选择 foresight-ocr 项目"
    panel.message = "请选择一个 Foresight OCR 项目文件夹；项目本身不需要 Python 环境。"
    panel.prompt = "选择项目"
    panel.canChooseFiles = false
    panel.canChooseDirectories = true
    panel.allowsMultipleSelection = false
    panel.canCreateDirectories = false
    guard panel.runModal() == .OK, let url = panel.url else { return }
    discoverProject(at: url)
  }

  func loadConfiguredProjectIfPresent() {
    guard
      projectURL == nil,
      !isDiscoveringProject,
      let path = ProcessInfo.processInfo.environment["FORESIGHT_OCR_PROJECT"],
      !path.isEmpty
    else { return }
    discoverProject(at: URL(fileURLWithPath: path, isDirectory: true))
  }

  func discoverProject(at url: URL) {
    Task { await discoverProjectNow(at: url) }
  }

  func openSelectedDocument() {
    Task { await startSelectedDocumentNow() }
  }

  func connect() {
    Task { await connectNow() }
  }

  func disconnect() {
    let shouldStopManagedService = managedService
    managedService = false
    api = nil
    bootstrap = nil
    workspaceReady = false
    spread = nil
    selectedPage = nil
    selectedEntryID = nil
    contextImage = nil
    contextImageMetadata = nil
    cropImage = nil
    statusMessage = "未连接"
    onboardingStep = .existingProject
    if shouldStopManagedService {
      Task { await backendProcess.stop() }
    }
  }

  private func setImportPDF(_ url: URL) {
    let priorStem = importPDFURL?.deletingPathExtension().lastPathComponent
    let stem = url.deletingPathExtension().lastPathComponent
    importPDFURL = url
    if newProjectName.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
      || newProjectName == priorStem
    {
      newProjectName = stem
    }
    if newDocumentID.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
      || newDocumentID == priorStem
    {
      newDocumentID = stem
    }
  }

  private func createProjectAndImportNow() async {
    guard
      canCreateProject,
      let destination = newProjectURL,
      let pdfURL = importPDFURL
    else { return }
    isCreatingProject = true
    errorMessage = nil
    statusMessage = "正在创建项目…"
    defer { isCreatingProject = false }
    do {
      let executable = try backendExecutable()
      _ = try await backendProcess.createProject(
        directoryURL: destination,
        name: newProjectName.trimmingCharacters(in: .whitespacesAndNewlines),
        executableURL: executable
      )
      let imported = try await backendProcess.importPDF(
        projectURL: destination,
        pdfURL: pdfURL,
        documentID: newDocumentID.trimmingCharacters(in: .whitespacesAndNewlines),
        executableURL: executable
      )
      projectURL = destination
      importedPDF = imported
      selectedDocumentID = imported.documentID
      documentManifest = nil
      statusMessage = "已保留 (imported.pageCount) 页源文件"
      await showEngineSetup(returnTo: .newProject)
    } catch {
      statusMessage = "项目创建失败"
      errorMessage = error.localizedDescription
    }
  }

  private func showEngineSetup(returnTo: OnboardingStep) async {
    guard let projectURL, let selectedDocumentID, !selectedDocumentID.isEmpty else {
      return
    }
    isLoadingEngineManifest = true
    errorMessage = nil
    statusMessage = "正在读取 OCR 引擎…"
    defer { isLoadingEngineManifest = false }
    do {
      let executable = try backendExecutable(for: projectURL)
      let manifest = try await backendProcess.engineManifest(executableURL: executable)
      guard manifest.protocolVersion == 1 else {
        throw BackendProcessError.incompatibleProtocol(manifest.protocolVersion)
      }
      engineManifest = manifest
      if let current = selectedEngineName,
        manifest.engines.contains(where: { $0.name == current && $0.supported })
      {
        selectedEngineName = current
      } else {
        selectedEngineName = DesktopOnboardingPolicy.suggestedEngine(in: manifest)?.name
      }
      engineInstallProgress = nil
      engineReturnStep = returnTo
      onboardingStep = .engine
      statusMessage = "请选择 OCR 引擎"
    } catch {
      statusMessage = "无法读取 OCR 引擎"
      errorMessage = error.localizedDescription
    }
  }

  private func installEngineAndContinueNow() async {
    guard
      !isInstallingEngine,
      !isPreparingProject,
      let projectURL,
      let selectedEngineName,
      let selectedEngine,
      selectedEngine.supported
    else { return }

    if !selectedEngine.available {
      guard engineManifest?.installerAvailable == true else {
        errorMessage = "应用内置的 OCR 引擎安装器不可用。请重新安装 Foresight OCR。"
        return
      }
      isInstallingEngine = true
      errorMessage = nil
      engineInstallProgress = nil
      statusMessage = "正在安装 (selectedEngine.displayName)…"
      do {
        let executable = try backendExecutable(for: projectURL)
        let installed = try await backendProcess.installEngine(
          name: selectedEngineName,
          executableURL: executable
        ) { [weak self] event in
          Task { @MainActor [weak self] in
            guard let self else { return }
            switch event {
            case .progress(let progress):
              self.engineInstallProgress = progress
            case .result:
              break
            }
          }
        }
        let refreshed = try await backendProcess.engineManifest(
          executableURL: executable
        )
        engineManifest = refreshed
        self.selectedEngineName = installed.name
        statusMessage = "(installed.displayName) 已就绪"
      } catch {
        isInstallingEngine = false
        statusMessage = "OCR 引擎安装失败"
        errorMessage = error.localizedDescription
        return
      }
      isInstallingEngine = false
    }

    await prepareProjectNow()
  }

  private func prepareProjectNow() async {
    guard
      !isInstallingEngine,
      !isPreparingProject,
      let projectURL,
      let documentID = selectedDocumentID,
      let backend = selectedEngineName
    else { return }
    isPreparingProject = true
    preparationProgress = [:]
    preparationResult = nil
    errorMessage = nil
    onboardingStep = .preparation
    statusMessage = "正在准备项目…"
    do {
      let executable = try backendExecutable(for: projectURL)
      let result = try await backendProcess.prepareProject(
        projectURL: projectURL,
        documentID: documentID,
        backend: backend,
        executableURL: executable
      ) { [weak self] event in
        Task { @MainActor [weak self] in
          guard let self else { return }
          switch event {
          case .progress(let progress):
            self.preparationProgress[progress.stage] = progress
            self.statusMessage = progress.label
          case .result(let result):
            self.preparationResult = result
          }
        }
      }
      preparationResult = result
      let manifest = try await backendProcess.discoverDocuments(
        projectURL: projectURL,
        executableURL: executable
      )
      documentManifest = manifest
      selectedDocumentID = result.documentID
      statusMessage = "项目准备完成"
    } catch {
      statusMessage = "项目准备失败"
      errorMessage = error.localizedDescription
    }
    isPreparingProject = false
  }

  func loadPage(_ page: Int, preferredEntryID: String? = nil) {
    guard page != selectedPage else { return }
    performAfterDraftCheck { [weak self] in
      guard let self else { return }
      Task { await self.loadPageNow(page, preferredEntryID: preferredEntryID) }
    }
  }

  func select(_ entry: ReviewEntry) {
    guard selectedSheet?.ignored != true else { return }
    guard entry.id != selectedEntryID else { return }
    performAfterDraftCheck { [weak self] in
      guard let self else { return }
      self.applySelection(entry)
      Task { await self.loadCrop(for: entry) }
    }
  }

  private func applySelection(_ entry: ReviewEntry) {
    selectedEntryID = entry.id
    draft = CorrectionFields(
      ownID: entry.ownID ?? "",
      parent: entry.parent ?? "",
      birthOrder: entry.birthOrder ?? "",
      additionalInfo: entry.additionalInfo ?? ""
    )
    note = entry.note ?? ""
    unreadable = entry.unreadable
    freeformTranscription = entry.human ?? entry.machine ?? ""
    baselineDraft = draft
    baselineFreeformTranscription = freeformTranscription
    baselineNote = note
    baselineUnreadable = unreadable
  }

  func discardDraftAndContinue() {
    showDiscardDraftConfirmation = false
    let action = pendingNavigation
    pendingNavigation = nil
    action?()
  }

  func keepEditingDraft() {
    showDiscardDraftConfirmation = false
    pendingNavigation = nil
  }

  func setImageVariant(_ variant: ImageVariant) {
    imageVariant = variant
    guard let selectedEntry else { return }
    Task { await loadCrop(for: selectedEntry) }
  }

  func setContextImageVariant(_ variant: ImageVariant) {
    contextImageVariant = variant
    guard let selectedPage else { return }
    Task { await loadContextImage(for: selectedPage, fallbackPath: nil) }
  }

  func requestPageIgnoreToggle() {
    guard let sheet = selectedSheet else { return }
    guard requireSavedDraft() else { return }
    if sheet.ignored {
      Task { await setCurrentPageIgnored(false) }
    } else {
      showIgnoreConfirmation = true
    }
  }

  func confirmPageIgnore() {
    showIgnoreConfirmation = false
    Task { await setCurrentPageIgnored(true) }
  }

  func requestPageReOCR() {
    guard requireSavedDraft() else { return }
    guard selectedSheet?.ignored == false, selectedPage != nil else { return }
    showPageOCRConfirmation = true
  }

  func confirmPageReOCR() {
    showPageOCRConfirmation = false
    Task { await reOCRCurrentPage() }
  }

  func openDocumentOCR() {
    showDocumentOCRPanel = true
    Task { await refreshDocumentOCRStatus(startPolling: true) }
  }

  func requestDocumentOCR() {
    showDocumentOCRConfirmation = true
  }

  func confirmDocumentOCR() {
    showDocumentOCRConfirmation = false
    Task { await startDocumentOCR() }
  }

  func openLearning() {
    showLearningPanel = true
    Task { await refreshLearning() }
  }

  func runLearning() {
    Task { await runLearningNow() }
  }

  func openLearningReport() {
    Task { await loadLearningReport() }
  }

  func openLayoutRepairPanel() {
    guard requireSavedDraft() else { return }
    guard selectedSheet?.ignored == false else { return }
    combPreview = nil
    combBoundaryOverrides = [:]
    showLayoutRepair = true
    Task { await refreshCombPreview(initial: true) }
  }

  func updateCombPreview() {
    Task { await refreshCombPreview(initial: false) }
  }

  func setBoundaryOverride(_ index: Int, value: Double?) {
    if let value {
      combBoundaryOverrides[index] = value
    } else {
      combBoundaryOverrides.removeValue(forKey: index)
    }
  }

  func requestApplyRecut() {
    showRecutConfirmation = true
  }

  func confirmApplyRecut() {
    showRecutConfirmation = false
    Task { await applyRecut() }
  }

  func exportDocument() {
    guard api != nil, !isExporting else { return }
    let panel = NSSavePanel()
    panel.title = "导出校对结果"
    panel.prompt = "导出"
    panel.nameFieldStringValue = "\(bootstrap?.documentID ?? "foresight-ocr")_export.zip"
    panel.allowedContentTypes = [.zip]
    guard panel.runModal() == .OK, let destination = panel.url else { return }
    Task { await exportDocumentNow(to: destination) }
  }

  func exportFolder() {
    guard api != nil, !isExporting else { return }
    let panel = NSOpenPanel()
    panel.title = "选择导出位置"
    panel.prompt = "选择文件夹"
    panel.canChooseFiles = false
    panel.canChooseDirectories = true
    panel.canCreateDirectories = true
    panel.allowsMultipleSelection = false
    guard panel.runModal() == .OK, let parent = panel.url else { return }
    Task { await exportFolderNow(parent: parent) }
  }

  func fitCrop() {
    cropFitCommand += 1
  }

  func setCropZoom(_ zoom: CGFloat) {
    cropZoom = min(8, max(0.05, zoom))
    cropZoomCommand += 1
  }

  func navigatePage(by offset: Int) {
    guard !hasMarkedText else { return }
    guard let current = selectedPage, let pages = bootstrap?.pages,
      let index = pages.firstIndex(of: current)
    else { return }
    let target = index + offset
    guard pages.indices.contains(target) else { return }
    loadPage(pages[target])
  }

  func navigateEntry(by offset: Int) {
    guard !hasMarkedText else { return }
    let entries = spread?.pages.flatMap(\.entries).filter { $0.role == "entry" } ?? []
    guard let selectedEntryID,
      let index = entries.firstIndex(where: { $0.id == selectedEntryID })
    else {
      if let first = entries.first { select(first) }
      return
    }
    let target = index + offset
    guard entries.indices.contains(target) else { return }
    select(entries[target])
  }

  func convertOwnIDFromDigits() {
    Task { await convertOwnIDFromDigitsNow() }
  }

  func confirmAndAdvance() {
    guard !hasMarkedText else { return }
    Task { await save(unreadable: false, advance: true) }
  }

  func markUnreadable() {
    guard !hasMarkedText else { return }
    Task { await save(unreadable: true, advance: true) }
  }

  func unconfirm() {
    guard !hasMarkedText else { return }
    Task { await unconfirmNow() }
  }

  private func connectNow() async {
    guard !isConnecting else { return }
    isConnecting = true
    workspaceReady = false
    errorMessage = nil
    statusMessage = "正在连接…"
    defer { isConnecting = false }
    do {
      guard let url = URL(string: serviceURL) else {
        throw ForesightAPIError.invalidBaseURL
      }
      try await openService(at: url)
    } catch {
      errorMessage = error.localizedDescription
      statusMessage = "连接失败"
    }
  }

  private func discoverProjectNow(at url: URL) async {
    guard !isDiscoveringProject, !isStartingBackend else { return }
    isDiscoveringProject = true
    errorMessage = nil
    statusMessage = "正在读取项目…"
    defer { isDiscoveringProject = false }
    do {
      let executable = try backendExecutable(for: url)
      let manifest = try await backendProcess.discoverDocuments(
        projectURL: url,
        executableURL: executable
      )
      guard manifest.protocolVersion == 1 else {
        throw BackendProcessError.incompatibleProtocol(manifest.protocolVersion)
      }
      projectURL = url
      documentManifest = manifest
      let configuredDocument = ProcessInfo.processInfo
        .environment["FORESIGHT_OCR_DOCUMENT"]
      selectedDocumentID =
        manifest.documents.first(where: {
          $0.reviewable && $0.id == configuredDocument
        })?.id ?? manifest.documents.first(where: \.reviewable)?.id
        ?? manifest.documents.first?.id
      onboardingStep = .existingProject
      statusMessage = "已读取 \(manifest.documents.count) 个文档"
    } catch {
      projectURL = nil
      documentManifest = nil
      selectedDocumentID = nil
      statusMessage = "项目读取失败"
      errorMessage = error.localizedDescription
    }
  }

  private func startSelectedDocumentNow() async {
    guard
      !isStartingBackend,
      let projectURL,
      let selectedDocumentID
    else { return }
    isStartingBackend = true
    workspaceReady = false
    errorMessage = nil
    statusMessage = "正在启动本机服务…"
    defer { isStartingBackend = false }
    do {
      let executable = try backendExecutable(for: projectURL)
      let ready = try await backendProcess.start(
        projectURL: projectURL,
        executableURL: executable,
        documentID: selectedDocumentID
      )
      serviceURL = ready.url.absoluteString
      managedService = true
      try await openService(at: ready.url)
    } catch {
      managedService = false
      statusMessage = "启动失败"
      errorMessage = error.localizedDescription
    }
  }

  private func openService(at url: URL) async throws {
    let client = try ForesightAPI(baseURL: url)
    let loadedBootstrap = try await client.bootstrap()
    if let version = loadedBootstrap.protocolVersion, version != 1 {
      throw BackendProcessError.incompatibleProtocol(version)
    }
    let firstPage =
      loadedBootstrap.summary.first(where: {
        !$0.ignored && $0.entries > $0.reviewed
      })?.page ?? loadedBootstrap.summary.first(where: { !$0.ignored })?.page
      ?? loadedBootstrap.pages.first
    api = client
    selectedPage = firstPage
    bootstrap = loadedBootstrap
    statusMessage = "已连接 · \(loadedBootstrap.documentID)"
    if let firstPage {
      await loadPageNow(firstPage)
    }
    workspaceReady = true
  }

  private func backendExecutable(for projectURL: URL? = nil) throws -> URL {
    var candidates: [URL] = []
    if let configured = ProcessInfo.processInfo.environment["FORESIGHT_OCR_EXECUTABLE"],
      !configured.isEmpty
    {
      candidates.append(URL(fileURLWithPath: configured))
    }
    if let resources = Bundle.main.resourceURL {
      candidates.append(resources.appending(path: "Backend/bin/foresight-ocr"))
    }
    // Keep the legacy project environment as a development fallback. Installed
    // apps always prefer the backend/runtime signed inside their own bundle.
    if let projectURL {
      candidates.append(projectURL.appending(path: ".venv/bin/foresight-ocr"))
    }
    if let executable = candidates.first(where: {
      FileManager.default.isExecutableFile(atPath: $0.path)
    }) {
      return executable
    }
    throw BackendProcessError.launchFailed(
      "应用内置的 OCR 后端缺失或不可执行。请重新安装 Foresight OCR。"
    )
  }

  private func loadPageNow(
    _ page: Int,
    preferredEntryID: String? = nil
  ) async {
    guard let api else { return }
    let requestID = UUID()
    pageRequestID = requestID
    isLoadingPage = true
    errorMessage = nil
    do {
      let loaded = try await api.page(page, spread: 1)
      guard pageRequestID == requestID else { return }
      spread = loaded
      selectedPage = page

      await loadContextImage(
        for: page,
        fallbackPath: loaded.pages.first?.imagePath
      )
      guard pageRequestID == requestID else { return }

      let entries = loaded.pages.flatMap(\.entries)
      let preferred = preferredEntryID.flatMap { id in
        entries.first(where: { $0.id == id })
      }
      let next =
        preferred
        ?? entries.first(where: { $0.flagged && $0.role == "entry" })
        ?? entries.first
      if loaded.pages.first?.ignored == true {
        selectedEntryID = nil
        cropImage = nil
        documentOCRPollingTask?.cancel()
        documentOCRPollingTask = nil
        documentOCRJob = nil
        pageOCREvent = nil
        cropMetadata = nil
      } else if let next {
        applySelection(next)
        await loadCrop(for: next)
      } else {
        selectedEntryID = nil
        cropImage = nil
      }
      statusMessage = "第 \(page) 页"
    } catch {
      guard pageRequestID == requestID else { return }
      errorMessage = error.localizedDescription
    }
    if pageRequestID == requestID { isLoadingPage = false }
  }

  private func loadCrop(for entry: ReviewEntry) async {
    guard let api else { return }
    let requestID = UUID()
    cropRequestID = requestID
    do {
      let path: String
      if supportsCropVariants, let regionUID = entry.regionUID {
        let metadata = try await api.cropImage(
          regionUID: regionUID,
          variant: imageVariant
        )
        guard cropRequestID == requestID, selectedEntryID == entry.id else {
          return
        }
        cropMetadata = metadata
        path = metadata.path
      } else if let cropPath = entry.cropPath {
        cropMetadata = nil
        path = cropPath
      } else {
        cropMetadata = nil
        cropImage = nil
        return
      }
      let data = try await api.image(pathToken: path)
      guard cropRequestID == requestID, selectedEntryID == entry.id else {
        return
      }
      cropImage = NSImage(data: data)
      cropFitCommand += 1
    } catch {
      guard cropRequestID == requestID else { return }
      cropImage = nil
      errorMessage = error.localizedDescription
    }
  }

  private func loadContextImage(for page: Int, fallbackPath: String?) async {
    guard let api else { return }
    let requestID = UUID()
    contextRequestID = requestID
    do {
      let path: String
      if supportsPageImageVariants {
        let metadata = try await api.pageImage(
          page: page,
          variant: contextImageVariant
        )
        guard contextRequestID == requestID, selectedPage == page else { return }
        contextImageMetadata = metadata
        path = metadata.path
      } else if let fallbackPath {
        contextImageMetadata = nil
        path = fallbackPath
      } else {
        contextImage = nil
        contextImageMetadata = nil
        return
      }
      let data = try await api.image(pathToken: path)
      guard contextRequestID == requestID, selectedPage == page else { return }
      contextImage = NSImage(data: data)
    } catch {
      guard contextRequestID == requestID else { return }
      errorMessage = error.localizedDescription
    }
  }

  private func setCurrentPageIgnored(_ ignored: Bool) async {
    guard let api, let page = selectedPage, !isMutatingPage else { return }
    isMutatingPage = true
    errorMessage = nil
    defer { isMutatingPage = false }
    do {
      _ = try await api.setPageIgnored(page: page, ignored: ignored)
      await refreshBootstrap()
      await loadPageNow(page)
      statusMessage = ignored ? "第 \(page) 页已忽略" : "第 \(page) 页已恢复"
    } catch {
      errorMessage = error.localizedDescription
    }
  }

  private func exportDocumentNow(to destination: URL) async {
    guard let api else { return }
    isExporting = true
    errorMessage = nil
    statusMessage = "正在导出…"
    defer { isExporting = false }
    do {
      let data = try await api.exportZIP()
      try data.write(to: destination, options: .atomic)
      statusMessage = "已导出 \(destination.lastPathComponent)"
    } catch {
      statusMessage = "导出失败"
      errorMessage = error.localizedDescription
    }
  }

  private func exportFolderNow(parent: URL) async {
    guard let api else { return }
    isExporting = true
    errorMessage = nil
    statusMessage = "正在导出文件夹…"
    defer { isExporting = false }
    var staging: URL?
    do {
      let files = try await api.exportFiles()
      guard
        files.allSatisfy({
          $0.name == URL(fileURLWithPath: $0.name).lastPathComponent
        })
      else {
        throw ForesightAPIError.invalidResponse
      }
      let directoryName = "\(bootstrap?.documentID ?? "foresight-ocr")_export"
      let destination = parent.appending(path: directoryName, directoryHint: .isDirectory)
      guard !FileManager.default.fileExists(atPath: destination.path) else {
        throw CocoaError(.fileWriteFileExists)
      }
      let temporary = parent.appending(
        path: ".foresight-ocr-export-\(UUID().uuidString)",
        directoryHint: .isDirectory
      )
      staging = temporary
      try FileManager.default.createDirectory(
        at: temporary,
        withIntermediateDirectories: false
      )
      for file in files {
        let output = temporary.appending(path: file.name)
        try Data(file.content.utf8).write(to: output, options: .atomic)
      }
      try FileManager.default.moveItem(at: temporary, to: destination)
      staging = nil
      statusMessage = "已导出文件夹 \(directoryName)"
    } catch {
      if let staging { try? FileManager.default.removeItem(at: staging) }
      statusMessage = "文件夹导出失败"
      errorMessage = error.localizedDescription
    }
  }

  private func reOCRCurrentPage() async {
    guard let api, let page = selectedPage, !isReOCRingPage else { return }
    isReOCRingPage = true
    pageOCREvent = nil
    errorMessage = nil
    statusMessage = "正在重新识别第 \(page) 页…"
    defer { isReOCRingPage = false }
    do {
      try await api.reOCRPage(page) { event in
        Task { @MainActor in
          self.pageOCREvent = event
          if event.type == "progress" {
            self.statusMessage = event.stage ?? "正在识别…"
          }
        }
      }
      await refreshBootstrap()
      await loadPageNow(page, preferredEntryID: selectedEntryID)
      statusMessage = "第 \(page) 页重新识别完成"
    } catch {
      statusMessage = "页面重新识别失败"
      errorMessage = error.localizedDescription
    }
  }

  private func refreshDocumentOCRStatus(startPolling: Bool) async {
    guard let api else { return }
    do {
      let job = try await api.documentOCRStatus()
      documentOCRJob = job
      if startPolling, ["queued", "running"].contains(job.status) {
        beginDocumentOCRPolling()
      }
    } catch {
      errorMessage = error.localizedDescription
    }
  }

  private func startDocumentOCR() async {
    guard let api, !isStartingDocumentOCR else { return }
    isStartingDocumentOCR = true
    errorMessage = nil
    defer { isStartingDocumentOCR = false }
    do {
      let response = try await api.startDocumentOCR(force: false)
      documentOCRJob = response.job
      statusMessage = response.started ? "全书识别已排队" : "全书识别已在运行"
      beginDocumentOCRPolling()
    } catch {
      errorMessage = error.localizedDescription
    }
  }

  private func beginDocumentOCRPolling() {
    documentOCRPollingTask?.cancel()
    documentOCRPollingTask = Task { [weak self] in
      guard let self else { return }
      while !Task.isCancelled {
        await self.refreshDocumentOCRStatus(startPolling: false)
        guard let status = self.documentOCRJob?.status,
          ["queued", "running"].contains(status)
        else {
          if self.documentOCRJob?.status == "complete" {
            await self.refreshBootstrap()
            if let page = self.selectedPage {
              await self.loadPageNow(page, preferredEntryID: self.selectedEntryID)
            }
          }
          return
        }
        try? await Task.sleep(for: .seconds(1))
      }
    }
  }

  private func refreshLearning() async {
    guard let api else { return }
    do {
      learningSnapshot = try await api.learningStatus()
    } catch {
      errorMessage = error.localizedDescription
    }
  }

  private func runLearningNow() async {
    guard let api, !isRunningLearning else { return }
    isRunningLearning = true
    errorMessage = nil
    defer { isRunningLearning = false }
    do {
      learningSnapshot = try await api.runLearningAnalysis()
      statusMessage = "校对学习分析完成（未运行 OCR）"
    } catch {
      errorMessage = error.localizedDescription
    }
  }

  private func loadLearningReport() async {
    guard let api else { return }
    do {
      learningReportMarkdown = try await api.learningReportMarkdown()
      showLearningReport = true
    } catch {
      errorMessage = error.localizedDescription
    }
  }

  private func refreshCombPreview(initial: Bool) async {
    guard let api, let page = selectedPage, !isLoadingCombPreview else { return }
    isLoadingCombPreview = true
    errorMessage = nil
    defer { isLoadingCombPreview = false }
    do {
      let preview = try await api.combPreview(
        page: page,
        phase: initial ? 0 : combPhase,
        pitch: initial ? nil : combPitch,
        snap: initial ? true : combSnap,
        textLeft: initial ? nil : combTextLeft,
        textRight: initial ? nil : combTextRight,
        boundaryOverrides: initial ? [:] : combBoundaryOverrides
      )
      combPreview = preview
      if initial {
        combPhase = preview.phaseAdjustment
        combPitch = preview.pitch
        combSnap = preview.snap
        combTextLeft = preview.textLeft
        combTextRight = preview.textRight
      }
    } catch {
      errorMessage = error.localizedDescription
    }
  }

  private func applyRecut() async {
    guard let api, let page = selectedPage, !isApplyingRecut else { return }
    isApplyingRecut = true
    errorMessage = nil
    statusMessage = "正在重新切分第 \(page) 页…"
    defer { isApplyingRecut = false }
    do {
      try await api.recutPage(
        page: page,
        phase: combPhase,
        pitch: combPitch,
        snap: combSnap,
        textLeft: combTextLeft,
        textRight: combTextRight,
        boundaryOverrides: combBoundaryOverrides
      ) { event in
        Task { @MainActor in
          self.pageOCREvent = event
          if event.type == "progress" {
            self.statusMessage = event.stage ?? "正在重新切分…"
          }
        }
      }
      await refreshBootstrap()
      await loadPageNow(page, preferredEntryID: selectedEntryID)
      await refreshCombPreview(initial: true)
      statusMessage = "第 \(page) 页已按预览重新切分"
    } catch {
      statusMessage = "页面重新切分失败"
      errorMessage = error.localizedDescription
    }
  }

  private func convertOwnIDFromDigitsNow() async {
    guard let api, let entry = selectedEntry, !isConvertingNumeral else { return }
    let digits = draft.ownID.trimmingCharacters(in: .whitespacesAndNewlines)
    guard !digits.isEmpty, digits.allSatisfy(\.isNumber) else { return }
    isConvertingNumeral = true
    errorMessage = nil
    defer { isConvertingNumeral = false }
    do {
      draft.ownID = try await api.numeral(label: entry.bandLabel, digits: digits)
    } catch {
      errorMessage = error.localizedDescription
    }
  }

  private func save(unreadable newUnreadable: Bool, advance: Bool) async {
    guard let api, let entry = selectedEntry, !isSaving else { return }
    isSaving = true
    errorMessage = nil
    let nextID = advance ? entryAfter(entry)?.id : entry.id
    do {
      _ = try await api.saveCorrection(
        entry: entry,
        fields: draft,
        transcription: freeformTranscription,
        unreadable: newUnreadable,
        note: note.isEmpty ? nil : note
      )
      statusMessage = newUnreadable ? "已标记无法辨认" : "校对已保存"
      await refreshBootstrap()
      await loadPageNow(entry.pageIndex, preferredEntryID: nextID ?? entry.id)
    } catch {
      errorMessage = error.localizedDescription
    }
    isSaving = false
  }

  private func unconfirmNow() async {
    guard let api, let entry = selectedEntry, !isSaving else { return }
    isSaving = true
    errorMessage = nil
    do {
      _ = try await api.unconfirm(entry)
      statusMessage = "已恢复机器识别"
      await refreshBootstrap()
      await loadPageNow(entry.pageIndex, preferredEntryID: entry.id)
    } catch {
      errorMessage = error.localizedDescription
    }
    isSaving = false
  }

  private func refreshBootstrap() async {
    guard let api else { return }
    if let refreshed = try? await api.bootstrap() { bootstrap = refreshed }
  }

  private func entryAfter(_ entry: ReviewEntry) -> ReviewEntry? {
    let entries = spread?.pages.flatMap(\.entries).filter { $0.role == "entry" } ?? []
    guard let index = entries.firstIndex(where: { $0.id == entry.id }) else {
      return nil
    }
    let nextIndex = entries.index(after: index)
    return nextIndex < entries.endIndex ? entries[nextIndex] : nil
  }

  private func performAfterDraftCheck(_ action: @escaping () -> Void) {
    guard hasUnsavedDraft else {
      action()
      return
    }
    pendingNavigation = action
    showDiscardDraftConfirmation = true
  }

  private func requireSavedDraft() -> Bool {
    guard hasUnsavedDraft else { return true }
    errorMessage = "请先保存或放弃当前校对，再执行页面操作。"
    return false
  }

  private var hasMarkedText: Bool {
    (NSApp.keyWindow?.firstResponder as? NSTextInputClient)?.hasMarkedText() == true
  }
}
