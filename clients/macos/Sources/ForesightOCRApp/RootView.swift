import ForesightOCRCore
import SwiftUI

struct RootView: View {
  @ObservedObject var model: ReviewAppModel

  var body: some View {
    Group {
      if model.bootstrap != nil, model.workspaceReady {
        ReviewWorkspaceView(model: model)
      } else {
        switch model.onboardingStep {
        case .welcome:
          WelcomeView(model: model)
        case .existingProject:
          ExistingProjectView(model: model)
        case .newProject:
          NewProjectView(model: model)
        case .engine:
          EngineSetupView(model: model)
        case .preparation:
          ProjectPreparationView(model: model)
        }
      }
    }
    .frame(maxWidth: .infinity, maxHeight: .infinity)
    .alert(
      "Foresight OCR",
      isPresented: Binding(
        get: { model.errorMessage != nil },
        set: { if !$0 { model.errorMessage = nil } }
      ),
      actions: {
        Button("好", role: .cancel) { model.errorMessage = nil }
      },
      message: { Text(model.errorMessage ?? "") }
    )
    .confirmationDialog(
      "忽略第 \(model.selectedPage ?? 0) 页？",
      isPresented: $model.showIgnoreConfirmation,
      titleVisibility: .visible
    ) {
      Button("忽略此页", role: .destructive) { model.confirmPageIgnore() }
      Button("取消", role: .cancel) {}
    } message: {
      Text("该页将从进度、OCR 目标和导出中排除，但仍可随时恢复。")
    }
    .confirmationDialog(
      "重新识别第 \(model.selectedPage ?? 0) 页？",
      isPresented: $model.showPageOCRConfirmation,
      titleVisibility: .visible
    ) {
      Button("开始识别") { model.confirmPageReOCR() }
      Button("取消", role: .cancel) {}
    } message: {
      Text("使用全分辨率去水印图像更新机器识别；已有人工校对不会被覆盖。")
    }
    .confirmationDialog(
      "启动全书增量识别？",
      isPresented: $model.showDocumentOCRConfirmation,
      titleVisibility: .visible
    ) {
      Button("开始全书识别") { model.confirmDocumentOCR() }
      Button("取消", role: .cancel) {}
    } message: {
      Text("只处理尚未缓存的区域（force=false），使用全分辨率去水印图像；可能耗时较长。")
    }
    .confirmationDialog(
      "按当前预览重新切分第 \(model.selectedPage ?? 0) 页？",
      isPresented: $model.showRecutConfirmation,
      titleVisibility: .visible
    ) {
      Button("应用并重新识别") { model.confirmApplyRecut() }
      Button("取消", role: .cancel) {}
    } message: {
      Text("将移动页面区域并重新生成切片；稳定区域身份和已有人工校对会按后端规则迁移。")
    }
    .confirmationDialog(
      "放弃尚未保存的校对？",
      isPresented: $model.showDiscardDraftConfirmation,
      titleVisibility: .visible
    ) {
      Button("放弃更改", role: .destructive) { model.discardDraftAndContinue() }
      Button("继续编辑", role: .cancel) { model.keepEditingDraft() }
    } message: {
      Text("切换条目或页面会丢弃当前字段与备注中的未保存更改。")
    }
    .sheet(isPresented: $model.showDocumentOCRPanel) {
      DocumentOCRSheet(model: model)
    }
    .sheet(isPresented: $model.showLearningPanel) {
      LearningSheet(model: model)
    }
    .sheet(isPresented: $model.showLearningReport) {
      LearningReportSheet(markdown: model.learningReportMarkdown)
    }
    .sheet(isPresented: $model.showLayoutRepair) {
      LayoutRepairSheet(model: model)
    }
    .task { model.loadConfiguredProjectIfPresent() }
  }
}

private struct WelcomeView: View {
  @ObservedObject var model: ReviewAppModel

  var body: some View {
    VStack(spacing: 0) {
      Spacer()
      VStack(spacing: 18) {
        Image(systemName: "doc.text.viewfinder")
          .font(.system(size: 62, weight: .ultraLight))
          .foregroundStyle(.secondary)
          .accessibilityHidden(true)
        VStack(spacing: 6) {
          Text("Foresight OCR")
            .font(.largeTitle.weight(.semibold))
          Text("原生宗谱校对工作站")
            .font(.title3)
            .foregroundStyle(.secondary)
        }
        Text("导入 PDF、安装识别引擎并开始校对，不需要打开终端。")
          .font(.body)
          .foregroundStyle(.secondary)
          .multilineTextAlignment(.center)

        HStack(spacing: 10) {
          Button(action: model.beginNewProject) {
            Label("新建项目…", systemImage: "plus")
              .frame(minWidth: 126)
          }
          .buttonStyle(.borderedProminent)
          Button(action: model.beginOpeningExistingProject) {
            Label("打开现有项目…", systemImage: "folder")
              .frame(minWidth: 150)
          }
          .buttonStyle(.bordered)
        }
        .controlSize(.large)
      }
      Spacer()
      Divider()
      HStack(spacing: 24) {
        Label("项目数据可移植", systemImage: "externaldrive")
        Label("运行环境由应用管理", systemImage: "cpu")
        Label("源文件与人工校对分开保存", systemImage: "checkmark.shield")
      }
      .font(.caption)
      .foregroundStyle(.secondary)
      .padding(.vertical, 12)
    }
    .frame(maxWidth: .infinity, maxHeight: .infinity)
    .background(.background)
  }
}

private struct ExistingProjectView: View {
  @ObservedObject var model: ReviewAppModel
  @State private var showsAttachMode = false

  var body: some View {
    VStack(spacing: 0) {
      SetupHeader(
        title: "打开现有项目",
        subtitle: "项目中的人工校对会由网页与原生客户端共同读取",
        backAction: model.returnToWelcome
      )
      Divider()
      ScrollView(.vertical, showsIndicators: true) {
        VStack(alignment: .leading, spacing: 22) {
          GroupBox("项目") {
            VStack(alignment: .leading, spacing: 14) {
              HStack(spacing: 12) {
                Image(systemName: "folder")
                  .font(.title2)
                  .foregroundStyle(.secondary)
                  .frame(width: 28)
                VStack(alignment: .leading, spacing: 2) {
                  Text(model.projectURL?.lastPathComponent ?? "尚未选择项目")
                    .font(.headline)
                  Text(
                    model.projectURL?.path(percentEncoded: false)
                      ?? "选择一个 Foresight OCR 项目文件夹。"
                  )
                  .font(.caption)
                  .foregroundStyle(.secondary)
                  .lineLimit(1)
                  .truncationMode(.middle)
                }
                Spacer()
                Button(model.projectURL == nil ? "选择…" : "更改…") {
                  model.chooseProject()
                }
                .disabled(model.isDiscoveringProject || model.isStartingBackend)
              }

              if model.isDiscoveringProject {
                ProgressView("正在读取项目…")
                  .controlSize(.small)
              } else if let manifest = model.documentManifest {
                Divider()
                if manifest.documents.isEmpty {
                  ContentUnavailableView(
                    "项目中没有文档",
                    systemImage: "doc.badge.ellipsis",
                    description: Text("请返回并从 PDF 新建项目。")
                  )
                  .frame(maxWidth: .infinity)
                } else {
                  Picker("文档", selection: $model.selectedDocumentID) {
                    ForEach(manifest.documents) { document in
                      Text(document.title).tag(Optional(document.id))
                    }
                  }
                  .pickerStyle(.menu)

                  if let document = model.selectedDocument {
                    HStack(spacing: 16) {
                      Label("\(document.pageCount) 页", systemImage: "doc.on.doc")
                      Label(
                        "\(document.reviewed)/\(document.entries) 已校对",
                        systemImage: "checkmark.circle"
                      )
                      if let tag = document.tag {
                        Text(tag)
                          .font(.caption.monospaced())
                      }
                    }
                    .font(.caption)
                    .foregroundStyle(.secondary)

                    Button {
                      if document.reviewable {
                        model.openSelectedDocument()
                      } else {
                        model.prepareSelectedExistingDocument()
                      }
                    } label: {
                      Text(document.reviewable ? "打开校对工作台" : "准备此文档…")
                    }
                    .buttonStyle(.borderedProminent)
                    .controlSize(.large)
                    .frame(maxWidth: .infinity, alignment: .trailing)
                    .disabled(
                      model.isStartingBackend || model.isLoadingEngineManifest
                    )
                  }
                }
              }
            }
            .padding(6)
          }

          DisclosureGroup("开发与恢复：连接已有服务", isExpanded: $showsAttachMode) {
            VStack(alignment: .leading, spacing: 10) {
              TextField("http://127.0.0.1:8765", text: $model.serviceURL)
                .textFieldStyle(.roundedBorder)
                .onSubmit(model.connect)
              HStack {
                Text("仅允许本机回环地址与显式端口。")
                  .font(.caption)
                  .foregroundStyle(.secondary)
                Spacer()
                Button("连接", action: model.connect)
                  .disabled(model.isConnecting || model.serviceURL.isEmpty)
              }
            }
            .padding(.top, 8)
          }
        }
        .frame(maxWidth: 620)
        .padding(32)
        .frame(maxWidth: .infinity)
      }
    }
    .background(.background)
  }
}

private struct NewProjectView: View {
  @ObservedObject var model: ReviewAppModel

  var body: some View {
    VStack(spacing: 0) {
      SetupHeader(
        title: "新建项目",
        subtitle: "第 1 步（共 3 步）：导入源文件",
        backAction: model.returnToWelcome
      )
      Divider()
      Form {
        Section("源文档") {
          LabeledContent("PDF") {
            HStack {
              Text(model.importPDFURL?.lastPathComponent ?? "尚未选择")
                .foregroundStyle(model.importPDFURL == nil ? .secondary : .primary)
                .lineLimit(1)
                .truncationMode(.middle)
              Button("选择…", action: model.chooseImportPDF)
            }
          }
          .accessibilityValue(model.importPDFURL?.lastPathComponent ?? "尚未选择")
          LabeledContent("文档标识") {
            TextField("例如：武林沈氏家譜_卷三", text: $model.newDocumentID)
              .frame(minWidth: 320)
          }
          Label("也可以将一个 PDF 拖到此窗口。原始文件会按校验和保留。", systemImage: "arrow.down.doc")
            .font(.caption)
            .foregroundStyle(.secondary)
            .dropDestination(for: URL.self) { urls, _ in
              return model.acceptDroppedPDFs(urls)
            }
        }

        Section("项目") {
          LabeledContent("项目名称") {
            TextField("项目名称", text: $model.newProjectName)
              .frame(minWidth: 320)
          }
          LabeledContent("存储位置") {
            HStack {
              Text(
                model.newProjectParentURL?.path(percentEncoded: false)
                  ?? "尚未选择"
              )
              .foregroundStyle(.secondary)
              .lineLimit(1)
              .truncationMode(.middle)
              Button("更改…", action: model.chooseNewProjectParent)
            }
          }
          .accessibilityValue(
            model.newProjectParentURL?.path(percentEncoded: false) ?? "尚未选择"
          )
          LabeledContent("项目文件夹") {
            Text(model.newProjectURL?.path(percentEncoded: false) ?? "请输入有效的项目名称")
              .font(.caption.monospaced())
              .foregroundStyle(.secondary)
              .lineLimit(2)
              .textSelection(.enabled)
          }
          .accessibilityValue(
            model.newProjectURL?.path(percentEncoded: false) ?? "请输入有效的项目名称"
          )
        }

        Section {
          Label(
            "项目只保存 PDF、配置、派生文件与校对数据库；OCR 运行环境由应用共享管理。",
            systemImage: "checkmark.shield"
          )
          .foregroundStyle(.secondary)
        }
      }
      .formStyle(.grouped)
      .frame(maxWidth: 780)
      .frame(maxWidth: .infinity, maxHeight: .infinity)

      Divider()
      HStack {
        Button("取消", action: model.returnToWelcome)
          .keyboardShortcut(.cancelAction)
        Spacer()
        Button(action: model.createProjectAndContinue) {
          if model.isCreatingProject {
            ProgressView()
              .controlSize(.small)
          } else {
            Text("下一步：选择 OCR 引擎")
          }
        }
        .buttonStyle(.borderedProminent)
        .disabled(!model.canCreateProject)
      }
      .padding(16)
    }
    .background(.background)
  }
}

private struct EngineSetupView: View {
  @ObservedObject var model: ReviewAppModel

  var body: some View {
    VStack(spacing: 0) {
      SetupHeader(
        title: "OCR 引擎",
        subtitle: "第 2 步（共 3 步）：选择应用管理的识别环境",
        backAction: model.backFromEngineSetup,
        backDisabled: model.isInstallingEngine
      )
      Divider()

      if let manifest = model.engineManifest {
        HSplitView {
          List(manifest.engines) { engine in
            Button {
              model.selectEngine(engine.name)
            } label: {
              EngineRow(
                engine: engine,
                selected: engine.name == model.selectedEngineName
              )
              .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .listRowBackground(
              engine.name == model.selectedEngineName
                ? Color.accentColor.opacity(0.12)
                : Color.clear
            )
          }
          .frame(minWidth: 300, idealWidth: 340)

          VStack(alignment: .leading, spacing: 20) {
            if let engine = model.selectedEngine {
              VStack(alignment: .leading, spacing: 6) {
                Text(engine.displayName)
                  .font(.title2.weight(.semibold))
                Text(engineDescription(engine))
                  .foregroundStyle(.secondary)
              }

              Grid(alignment: .leadingFirstTextBaseline, horizontalSpacing: 24, verticalSpacing: 10)
              {
                GridRow {
                  Text("状态").foregroundStyle(.secondary)
                  Text(engineStatusTitle(engine))
                }
                GridRow {
                  Text("运行环境").foregroundStyle(.secondary)
                  Text(engine.environment)
                    .font(.caption.monospaced())
                    .textSelection(.enabled)
                }
                GridRow {
                  Text("安装清单").foregroundStyle(.secondary)
                  Text(engine.requirements.joined(separator: "\n"))
                    .font(.caption.monospaced())
                    .textSelection(.enabled)
                }
              }

              if model.isInstallingEngine {
                VStack(alignment: .leading, spacing: 8) {
                  ProgressView()
                  Text(engineInstallTitle(model.engineInstallProgress))
                    .font(.caption)
                    .foregroundStyle(.secondary)
                }
              }
              Spacer()
              Label(
                "安装器不会修改系统 Python；环境与模型位于应用支持目录。",
                systemImage: "lock.shield"
              )
              .font(.caption)
              .foregroundStyle(.secondary)
            } else {
              ContentUnavailableView(
                "没有兼容的 OCR 引擎",
                systemImage: "cpu",
                description: Text("此 Mac 不满足清单中的平台要求。")
              )
            }
          }
          .padding(28)
          .frame(minWidth: 520, maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        }
      } else {
        ProgressView("正在读取本机引擎清单…")
          .frame(maxWidth: .infinity, maxHeight: .infinity)
      }

      Divider()
      HStack {
        Text(model.engineManifest?.engineHome ?? "")
          .font(.caption.monospaced())
          .foregroundStyle(.secondary)
          .lineLimit(1)
          .truncationMode(.middle)
        Spacer()
        Button(action: model.installEngineAndContinue) {
          Text(engineActionTitle)
        }
        .buttonStyle(.borderedProminent)
        .disabled(
          model.selectedEngine?.supported != true
            || model.isInstallingEngine
            || model.isPreparingProject
        )
      }
      .padding(16)
    }
    .background(.background)
  }

  private var engineActionTitle: String {
    if model.isInstallingEngine { return "正在安装…" }
    return model.selectedEngine?.available == true
      ? "继续准备项目"
      : "安装并继续"
  }

  private func engineDescription(_ engine: EngineStatus) -> String {
    switch engine.name {
    case "paddleocr_vl":
      "通过 MLX 运行的视觉语言识别引擎，仅支持 Apple Silicon。"
    case "ppocr_v5":
      "通用文字识别引擎，适合常规印刷与扫描文档。"
    default:
      engine.detail
    }
  }

  private func engineStatusTitle(_ engine: EngineStatus) -> String {
    if !engine.supported { return "此 Mac 不支持" }
    if engine.available { return "已安装 · \(engine.detail)" }
    return "尚未安装"
  }

  private func engineInstallTitle(_ progress: EngineInstallProgress?) -> String {
    guard let progress else { return "正在启动安装器…" }
    let stage =
      switch progress.stage {
      case "python_runtime": "准备 Python 运行时"
      case "engine_packages": "安装引擎软件包"
      case "engine_probe": "验证识别引擎"
      default: progress.stage
      }
    return progress.status == "completed" ? "\(stage)已完成" : stage
  }
}

private struct ProjectPreparationView: View {
  @ObservedObject var model: ReviewAppModel

  var body: some View {
    VStack(spacing: 0) {
      SetupHeader(
        title: "准备项目",
        subtitle: "第 3 步（共 3 步）：生成可校对资产",
        backAction: model.returnToEngineSetup,
        backDisabled: model.isPreparingProject
      )
      Divider()

      VStack(alignment: .leading, spacing: 18) {
        VStack(alignment: .leading, spacing: 7) {
          HStack {
            Text(model.preparationResult?.ready == true ? "项目已就绪" : "正在处理文档")
              .font(.title2.weight(.semibold))
            Spacer()
            Text(
              "\(model.completedPreparationStageCount)/\(DesktopOnboardingPolicy.preparationStages.count)"
            )
            .font(.body.monospacedDigit())
            .foregroundStyle(.secondary)
          }
          ProgressView(
            value: Double(model.completedPreparationStageCount),
            total: Double(DesktopOnboardingPolicy.preparationStages.count)
          )
          Text(model.projectURL?.path(percentEncoded: false) ?? "")
            .font(.caption.monospaced())
            .foregroundStyle(.secondary)
            .lineLimit(1)
            .truncationMode(.middle)
        }

        List(DesktopOnboardingPolicy.preparationStages) { stage in
          PreparationStageRow(
            stage: stage,
            progress: model.preparationProgress[stage.id],
            ready: model.preparationResult?.ready == true
          )
        }
        .listStyle(.inset)

        Label(
          "已完成的处理结果保存在项目中；失败后可以安全重试。",
          systemImage: "info.circle"
        )
        .font(.caption)
        .foregroundStyle(.secondary)
      }
      .frame(maxWidth: 760, maxHeight: .infinity)
      .padding(28)
      .frame(maxWidth: .infinity, maxHeight: .infinity)

      Divider()
      HStack {
        if let recognition = model.preparationResult?.recognition {
          Text(
            "识别 \(recognition.recognized) · 复用 \(recognition.reused) · 错误 \(recognition.errors.count)"
          )
          .font(.caption.monospacedDigit())
          .foregroundStyle(.secondary)
        }
        Spacer()
        if model.preparationResult?.ready == true {
          Button("进入校对工作台", action: model.openSelectedDocument)
            .buttonStyle(.borderedProminent)
            .disabled(model.isStartingBackend)
        } else if !model.isPreparingProject {
          Button("重试准备", action: model.retryPreparation)
            .buttonStyle(.borderedProminent)
        } else {
          ProgressView()
            .controlSize(.small)
        }
      }
      .padding(16)
    }
    .background(.background)
  }
}

private struct SetupHeader: View {
  let title: String
  let subtitle: String
  let backAction: () -> Void
  var backDisabled = false

  var body: some View {
    HStack(spacing: 12) {
      Button(action: backAction) {
        Image(systemName: "chevron.left")
      }
      .buttonStyle(.borderless)
      .keyboardShortcut(.cancelAction)
      .disabled(backDisabled)
      VStack(alignment: .leading, spacing: 1) {
        Text(title).font(.headline)
        Text(subtitle).font(.caption).foregroundStyle(.secondary)
      }
      Spacer()
    }
    .padding(.horizontal, 16)
    .frame(height: 54)
    .background(.bar)
  }
}

private struct EngineRow: View {
  let engine: EngineStatus
  let selected: Bool

  var body: some View {
    HStack(spacing: 10) {
      Image(systemName: selected ? "checkmark.circle.fill" : "circle")
        .foregroundStyle(selected ? Color.accentColor : Color.secondary)
      VStack(alignment: .leading, spacing: 3) {
        HStack {
          Text(engine.displayName)
            .font(.body.weight(.medium))
          if !engine.supported {
            Text("不支持")
              .font(.caption2)
              .foregroundStyle(.secondary)
          }
        }
        Text(engine.available ? "已安装" : engine.supported ? "尚未安装" : engine.detail)
          .font(.caption)
          .foregroundStyle(engine.available ? .green : .secondary)
      }
      Spacer()
    }
    .padding(.vertical, 5)
    .opacity(engine.supported ? 1 : 0.55)
  }
}

private struct PreparationStageRow: View {
  let stage: PreparationStageDefinition
  let progress: ProjectPreparationProgress?
  let ready: Bool

  var body: some View {
    HStack(spacing: 12) {
      stageIcon
        .frame(width: 20)
      VStack(alignment: .leading, spacing: 3) {
        Text("\(stageNumber)  \(stage.label)")
          .font(.body.weight(.medium))
        Text(detail)
          .font(.caption)
          .foregroundStyle(progress?.status == "failed" ? .red : .secondary)
          .lineLimit(2)
      }
      Spacer()
      Text(statusTitle)
        .font(.caption)
        .foregroundStyle(statusColor)
    }
    .padding(.vertical, 5)
  }

  @ViewBuilder
  private var stageIcon: some View {
    switch effectiveStatus {
    case "completed":
      Image(systemName: "checkmark.circle.fill").foregroundStyle(.green)
    case "started":
      ProgressView().controlSize(.small)
    case "failed":
      Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.red)
    default:
      Image(systemName: "circle").foregroundStyle(.tertiary)
    }
  }

  private var stageNumber: Int {
    DesktopOnboardingPolicy.preparationStages.firstIndex(of: stage).map { $0 + 1 } ?? 0
  }

  private var effectiveStatus: String {
    if ready { return "completed" }
    return progress?.status ?? "pending"
  }

  private var detail: String {
    if let detail = progress?.detail, !detail.isEmpty { return detail }
    return switch effectiveStatus {
    case "completed": "已完成"
    case "started": "处理中…"
    case "failed": "后端未提供更多错误详情"
    default: "等待处理"
    }
  }

  private var statusTitle: String {
    switch effectiveStatus {
    case "completed": "完成"
    case "started": "处理中"
    case "failed": "失败"
    default: "等待"
    }
  }

  private var statusColor: Color {
    switch effectiveStatus {
    case "completed": .green
    case "started": .accentColor
    case "failed": .red
    default: .secondary
    }
  }
}
