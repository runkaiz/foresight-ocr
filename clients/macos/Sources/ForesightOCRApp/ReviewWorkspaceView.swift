import AppKit
import ForesightOCRCore
import SwiftUI

struct ReviewWorkspaceView: View {
  @ObservedObject var model: ReviewAppModel

  var body: some View {
    NativeWorkspaceSplitView(model: model)
      .frame(maxWidth: .infinity, maxHeight: .infinity)
      .toolbar { workspaceToolbar }
  }

  @ToolbarContentBuilder
  private var workspaceToolbar: some ToolbarContent {
    ToolbarItemGroup(placement: .navigation) {
      Button {
        model.showSidebar.toggle()
      } label: {
        Label("页面导航", systemImage: "sidebar.leading")
      }
      .help(model.showSidebar ? "隐藏页面导航" : "显示页面导航")
      Button {
        model.navigatePage(by: -1)
      } label: {
        Label("上一页", systemImage: "chevron.left")
      }
      Button {
        model.navigatePage(by: 1)
      } label: {
        Label("下一页", systemImage: "chevron.right")
      }
      Button {
        model.showContext.toggle()
      } label: {
        Label("页面上下文", systemImage: "rectangle.leadinghalf.inset.filled")
      }
      .help(model.showContext ? "隐藏页面上下文" : "显示页面上下文")
    }

    ToolbarItem(placement: .principal) {
      if let bootstrap = model.bootstrap, let page = model.selectedPage {
        Text("\(bootstrap.documentID) · 第 \(page) 页")
          .font(.headline)
          .lineLimit(1)
          .fixedSize(horizontal: true, vertical: false)
          .padding(.horizontal, 10)
      }
    }

    ToolbarItemGroup {
      Button {
        model.setCropZoom(model.cropZoom / 1.2)
      } label: {
        Label("缩小切片", systemImage: "minus.magnifyingglass")
      }
      .disabled(model.cropImage == nil)

      Button("100%") { model.setCropZoom(1) }
        .monospacedDigit()
        .disabled(model.cropImage == nil)

      Button {
        model.setCropZoom(model.cropZoom * 1.2)
      } label: {
        Label("放大切片", systemImage: "plus.magnifyingglass")
      }
      .disabled(model.cropImage == nil)

      Button {
        model.fitCrop()
      } label: {
        Label("适合高度", systemImage: "arrow.up.and.down")
      }
      .disabled(model.cropImage == nil)

      Button {
        model.showInspector.toggle()
      } label: {
        Label("校对检查器", systemImage: "sidebar.right")
      }
      .help(model.showInspector ? "隐藏校对检查器" : "显示校对检查器")

      Menu {
        Button(
          model.selectedSheet?.ignored == true ? "恢复此页" : "忽略此页",
          action: model.requestPageIgnoreToggle
        )
        .disabled(model.selectedSheet == nil || model.isMutatingPage)
        Divider()
        Button("重新识别此页…", action: model.requestPageReOCR)
          .disabled(
            model.selectedSheet?.ignored != false || model.isReOCRingPage
          )
        Button("页面格线修复…", action: model.openLayoutRepairPanel)
          .disabled(model.selectedSheet?.ignored != false)
        Button("全书增量识别…", action: model.openDocumentOCR)
        Button("校对学习审计…", action: model.openLearning)
        Divider()
        Button("导出校对结果…", action: model.exportDocument)
          .disabled(model.isExporting)
        Button("导出为文件夹…", action: model.exportFolder)
          .disabled(model.isExporting)
      } label: {
        Label("文档操作", systemImage: "ellipsis.circle")
      }
    }
  }
}

private struct NativeWorkspaceSplitView: NSViewControllerRepresentable {
  @ObservedObject var model: ReviewAppModel

  func makeCoordinator() -> Coordinator { Coordinator() }

  func makeNSViewController(context: Context) -> NSSplitViewController {
    let controller = NSSplitViewController()
    controller.splitView.isVertical = true
    controller.splitView.dividerStyle = .thin
    controller.splitView.autosaveName = "ForesightOCRReviewWorkspace-v3"

    let sidebar = makeItem(
      rootView: AnyView(PageSidebar(model: model)),
      width: 188,
      minimum: 180,
      maximum: 320,
      preferredFraction: 0.125,
      priority: 252
    )
    sidebar.canCollapse = true

    let contextRail = makeItem(
      rootView: AnyView(SourceContextRail(model: model)),
      width: 285,
      minimum: 200,
      maximum: 520,
      preferredFraction: 0.19,
      priority: 251
    )
    contextRail.canCollapse = true

    let crop = makeItem(
      rootView: AnyView(PrimaryCropWorkspace(model: model)),
      width: 650,
      minimum: 320,
      maximum: 10_000,
      preferredFraction: 0.44,
      priority: 200
    )
    crop.canCollapse = false

    let inspector = makeItem(
      rootView: AnyView(VerificationInspector(model: model)),
      width: 350,
      minimum: 300,
      maximum: 480,
      preferredFraction: 0.23,
      priority: 251
    )
    inspector.canCollapse = true

    [sidebar, contextRail, crop, inspector].forEach(controller.addSplitViewItem)
    sidebar.isCollapsed = !model.showSidebar
    contextRail.isCollapsed = !model.showContext
    inspector.isCollapsed = !model.showInspector
    context.coordinator.attach(to: controller)
    return controller
  }

  func updateNSViewController(_ controller: NSSplitViewController, context: Context) {
    context.coordinator.updateVisibility(
      sidebar: model.showSidebar,
      contextRail: model.showContext,
      inspector: model.showInspector,
      in: controller
    )
  }

  private func makeItem(
    rootView: AnyView,
    width: CGFloat,
    minimum: CGFloat,
    maximum: CGFloat,
    preferredFraction: CGFloat,
    priority: Float
  ) -> NSSplitViewItem {
    let hosting = NSHostingController(rootView: rootView)
    hosting.view.frame.size.width = width
    let item = NSSplitViewItem(viewController: hosting)
    item.minimumThickness = minimum
    item.maximumThickness = maximum
    item.preferredThicknessFraction = preferredFraction
    item.holdingPriority = NSLayoutConstraint.Priority(priority)
    return item
  }

  @MainActor
  final class Coordinator {
    private weak var controller: NSSplitViewController?
    private var rememberedWidths: [Int: CGFloat] = [
      0: 188,
      1: 285,
      3: 350,
    ]
    private var restorationGeneration = 0

    func attach(to controller: NSSplitViewController) {
      self.controller = controller
    }

    func updateVisibility(
      sidebar: Bool,
      contextRail: Bool,
      inspector: Bool,
      in controller: NSSplitViewController
    ) {
      guard controller.splitViewItems.count == 4 else { return }
      attach(to: controller)
      let visibility = [0: sidebar, 1: contextRail, 3: inspector]
      let changes = visibility.filter { index, visible in
        controller.splitViewItems[index].isCollapsed == visible
      }
      guard !changes.isEmpty else { return }

      rememberVisibleWidths(in: controller)
      for (index, visible) in changes {
        let item = controller.splitViewItems[index]
        if visible, let width = rememberedWidths[index] {
          item.viewController.view.frame.size.width = clamped(width, for: item)
        }
        item.isCollapsed = !visible
      }

      restoreVisibleWidths(in: controller)
      restorationGeneration += 1
      let generation = restorationGeneration
      DispatchQueue.main.async { [weak self, weak controller] in
        guard let self, let controller, generation == restorationGeneration else { return }
        restoreVisibleWidths(in: controller)
      }
    }

    private func rememberVisibleWidths(in controller: NSSplitViewController) {
      for index in [0, 1, 3] {
        let item = controller.splitViewItems[index]
        guard !item.isCollapsed else { continue }
        let width = item.viewController.view.frame.width
        if width >= item.minimumThickness {
          rememberedWidths[index] = clamped(width, for: item)
        }
      }
    }

    private func restoreVisibleWidths(in controller: NSSplitViewController) {
      let splitView = controller.splitView
      guard splitView.subviews.count == 4 else { return }
      splitView.adjustSubviews()
      splitView.layoutSubtreeIfNeeded()

      for index in [0, 1] {
        let item = controller.splitViewItems[index]
        guard !item.isCollapsed, let remembered = rememberedWidths[index] else { continue }
        let width = clamped(remembered, for: item)
        let position = splitView.subviews[index].frame.minX + width
        splitView.setPosition(position, ofDividerAt: index)
        splitView.layoutSubtreeIfNeeded()
      }

      let inspector = controller.splitViewItems[3]
      if !inspector.isCollapsed, let remembered = rememberedWidths[3] {
        let width = clamped(remembered, for: inspector)
        let rightEdge = splitView.subviews[3].frame.maxX
        splitView.setPosition(
          rightEdge - width - splitView.dividerThickness,
          ofDividerAt: 2
        )
      }
      splitView.adjustSubviews()
    }

    private func clamped(_ width: CGFloat, for item: NSSplitViewItem) -> CGFloat {
      min(item.maximumThickness, max(item.minimumThickness, width))
    }
  }
}

private struct PageSidebar: View {
  @ObservedObject var model: ReviewAppModel

  var body: some View {
    VStack(spacing: 0) {
      if let bootstrap = model.bootstrap {
        VStack(alignment: .leading, spacing: 8) {
          HStack {
            Text("校对进度")
              .font(.caption)
              .foregroundStyle(.secondary)
            Spacer()
            Text("\(bootstrap.progress.reviewed)/\(bootstrap.progress.entries)")
              .font(.caption.monospacedDigit())
              .foregroundStyle(.secondary)
          }
          ProgressView(value: model.progressFraction)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)

        Divider()

        ScrollView {
          LazyVStack(spacing: 2) {
            ForEach(bootstrap.summary) { summary in
              Button {
                model.loadPage(summary.page)
              } label: {
                PageSummaryRow(
                  summary: summary,
                  selected: model.selectedPage == summary.page
                )
                .padding(.horizontal, 8)
                .padding(.vertical, 4)
                .background(
                  model.selectedPage == summary.page
                    ? Color.accentColor.opacity(0.16)
                    : Color.clear,
                  in: RoundedRectangle(cornerRadius: 7)
                )
              }
              .buttonStyle(.plain)
            }
          }
          .padding(.horizontal, 6)
          .padding(.vertical, 5)
        }
      }

      Divider()
      HStack(spacing: 8) {
        Circle()
          .fill(model.bootstrap == nil ? Color.secondary : Color.green)
          .frame(width: 7, height: 7)
        Text(model.statusMessage)
          .font(.caption)
          .foregroundStyle(.secondary)
          .lineLimit(1)
        Spacer()
        Button {
          model.disconnect()
        } label: {
          Image(systemName: "eject")
        }
        .buttonStyle(.borderless)
        .help("断开服务")
      }
      .padding(10)
    }
    .navigationTitle(model.bootstrap?.documentID ?? "页面")
  }
}

private struct PageSummaryRow: View {
  let summary: PageSummary
  let selected: Bool

  var body: some View {
    HStack(spacing: 9) {
      Image(systemName: statusSymbol)
        .foregroundStyle(statusColor)
        .frame(width: 16)
        .accessibilityHidden(true)
      VStack(alignment: .leading, spacing: 2) {
        Text("第 \(summary.page) 页")
          .foregroundStyle(.primary)
        Text(detail)
          .font(.caption2)
          .foregroundStyle(.secondary)
      }
      Spacer()
      if summary.flagged > 0 {
        Text("\(summary.flagged)")
          .font(.caption2.monospacedDigit())
          .padding(.horizontal, 6)
          .padding(.vertical, 2)
          .background(.orange.opacity(0.16), in: Capsule())
          .accessibilityLabel("\(summary.flagged) 条争议")
      }
    }
    .padding(.vertical, 3)
    .contentShape(Rectangle())
    .accessibilityElement(children: .combine)
    .accessibilityAddTraits(selected ? .isSelected : [])
  }

  private var detail: String {
    if summary.ignored { return "已忽略 · 可恢复" }
    return "\(summary.reviewed)/\(summary.entries) 已校对"
  }

  private var statusSymbol: String {
    if summary.ignored { return "minus.circle" }
    if summary.entries > 0, summary.reviewed >= summary.entries {
      return "checkmark.circle.fill"
    }
    if summary.flagged > 0 { return "exclamationmark.circle" }
    return "circle"
  }

  private var statusColor: Color {
    if summary.ignored { return .secondary }
    if summary.entries > 0, summary.reviewed >= summary.entries { return .green }
    if summary.flagged > 0 { return .orange }
    return .secondary
  }
}

private struct SourceContextRail: View {
  @ObservedObject var model: ReviewAppModel

  var body: some View {
    VStack(spacing: 0) {
      HStack {
        Text("页面上下文")
          .font(.caption.weight(.semibold))
          .foregroundStyle(.secondary)
        Spacer()
        if model.isLoadingPage || model.isReOCRingPage {
          ProgressView().controlSize(.small)
        }
        Picker("页面图像", selection: $model.contextImageVariant) {
          Text("原图").tag(ImageVariant.original)
          Text("去水印").tag(ImageVariant.watermark)
        }
        .pickerStyle(.menu)
        .labelsHidden()
        .frame(width: 92)
        .disabled(
          !model.supportsPageImageVariants || model.isLoadingPage
            || model.selectedSheet?.ignored == true
        )
        .onChange(of: model.contextImageVariant) { _, variant in
          model.setContextImageVariant(variant)
        }
      }
      .padding(.horizontal, 12)
      .padding(.vertical, 9)
      Divider()
      if model.selectedSheet?.ignored != true {
        EntryStateLegend()
          .padding(.horizontal, 12)
          .padding(.vertical, 6)
          .background(Color(nsColor: .windowBackgroundColor))
        Divider()
      }

      if model.selectedSheet?.ignored == true {
        IgnoredPageUnavailableView(
          description: "恢复此页后才能编辑、重识别或重新切分。",
          action: model.requestPageIgnoreToggle,
          actionDisabled: model.isMutatingPage
        )
      } else if let sheet = model.selectedSheet, let image = model.contextImage {
        ContextPageCanvas(
          image: image,
          sheet: sheet,
          sourceSize: CGSize(
            width: model.contextImageMetadata?.width ?? sheet.width,
            height: model.contextImageMetadata?.height ?? sheet.height
          ),
          selectedEntryID: model.selectedEntryID,
          onSelect: model.select
        )
        .padding(14)
      } else {
        ContentUnavailableView(
          "没有页面图像",
          systemImage: "doc",
          description: Text("服务尚未返回当前页图像。")
        )
      }
    }
    .background(Color(nsColor: .underPageBackgroundColor))
  }
}

private struct PrimaryCropWorkspace: View {
  @ObservedObject var model: ReviewAppModel

  var body: some View {
    VStack(spacing: 0) {
      HStack(spacing: 10) {
        VStack(alignment: .leading, spacing: 2) {
          Text("当前切片")
            .font(.headline)
          if let entry = model.selectedEntry {
            Text("第 \(entry.pageIndex) 页 · \(entry.bandLabel) · \(entry.entryIndex + 1)")
              .font(.caption)
              .foregroundStyle(.secondary)
          }
        }
        Spacer()
        Picker("图像", selection: $model.imageVariant) {
          Text("原图").tag(ImageVariant.original)
          Text("去水印").tag(ImageVariant.watermark)
        }
        .pickerStyle(.segmented)
        .labelsHidden()
        .frame(width: 150)
        .disabled(!model.supportsCropVariants || model.selectedEntry == nil)
        .onChange(of: model.imageVariant) { _, variant in
          model.setImageVariant(variant)
        }
      }
      .padding(.horizontal, 14)
      .padding(.vertical, 9)
      .background(.bar)

      Divider()

      if model.selectedSheet?.ignored == true {
        IgnoredPageUnavailableView(
          description: "本页不参与识别、校对进度或导出。",
          action: model.requestPageIgnoreToggle,
          actionDisabled: model.isMutatingPage
        )
      } else if let image = model.cropImage {
        ZoomableImageView(
          image: image,
          zoom: model.cropZoom,
          zoomCommand: model.cropZoomCommand,
          fitCommand: model.cropFitCommand
        )
      } else {
        ContentUnavailableView(
          "选择一个切片",
          systemImage: "viewfinder.rectangular",
          description: Text("从页面上下文或页内条目中选择需要校对的区域。")
        )
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color(nsColor: .underPageBackgroundColor))
      }
    }
  }
}

private struct VerificationInspector: View {
  @ObservedObject var model: ReviewAppModel

  var body: some View {
    VStack(spacing: 0) {
      if let entry = model.selectedEntry {
        Form {
          Section {
            HStack {
              Label(statusTitle(entry), systemImage: statusSymbol(entry))
                .foregroundStyle(statusColor(entry))
              Spacer()
              Text(entry.machineBackend ?? "OCR")
                .font(.caption)
                .foregroundStyle(.secondary)
            }
            if entry.flagged {
              Text(discrepancyText(entry))
                .font(.callout)
                .foregroundStyle(.secondary)
            }
          }

          Section("校对字段") {
            if entry.role == "entry" {
              HStack {
                TextField("本人编号", text: $model.draft.ownID)
                Button("转为谱号") { model.convertOwnIDFromDigits() }
                  .controlSize(.small)
                  .disabled(
                    model.isConvertingNumeral
                      || model.draft.ownID.isEmpty
                      || !model.draft.ownID.allSatisfy(\.isNumber)
                  )
                  .help("把阿拉伯数字按当前世代转换为谱面编号")
              }
              TextField("父辈／姓名", text: $model.draft.parent)
              TextField("排行", text: $model.draft.birthOrder)
              VStack(alignment: .leading, spacing: 6) {
                Text("附加信息")
                  .font(.caption)
                  .foregroundStyle(.secondary)
                TextEditor(text: $model.draft.additionalInfo)
                  .frame(minHeight: 92)
                  .font(.body)
              }
            } else {
              VStack(alignment: .leading, spacing: 6) {
                Text(headerLabel(entry))
                  .font(.caption)
                  .foregroundStyle(.secondary)
                TextEditor(text: $model.freeformTranscription)
                  .frame(minHeight: 150)
                  .font(.body)
              }
            }
            TextField("校对备注", text: $model.note, axis: .vertical)
          }
          .disabled(model.isSaving)

          Section("原始 OCR 证据") {
            Text(entry.machine ?? "没有机器识别结果")
              .font(.system(.body, design: .monospaced))
              .foregroundStyle(entry.machine == nil ? .secondary : .primary)
              .textSelection(.enabled)
            if entry.staleReading {
              Label("此结果来自切片调整前的像素", systemImage: "clock.arrow.circlepath")
                .font(.caption)
                .foregroundStyle(.orange)
            }
          }
        }
        .formStyle(.grouped)

        Divider()
        VStack(spacing: 8) {
          Button(action: model.confirmAndAdvance) {
            if model.isSaving {
              ProgressView().controlSize(.small)
            } else {
              Text("确认并跳转")
                .frame(maxWidth: .infinity)
            }
          }
          .buttonStyle(.borderedProminent)
          .keyboardShortcut(.return, modifiers: [.command])
          .disabled(model.isSaving)

          HStack {
            Button("无法辨认", action: model.markUnreadable)
              .disabled(model.isSaving)
            Button("取消确认", action: model.unconfirm)
              .disabled(!entry.isConfirmed || model.isSaving)
          }
        }
        .padding(12)
      } else if model.selectedSheet?.ignored == true {
        IgnoredPageUnavailableView(
          description: "本页不参与校对；恢复后可选择条目。"
        )
      } else {
        ContentUnavailableView(
          "没有选中条目",
          systemImage: "sidebar.right",
          description: Text("选择切片后可在此校对。")
        )
      }
    }
    .navigationTitle("校对")
  }

  private func statusTitle(_ entry: ReviewEntry) -> String {
    if entry.unreadable { return "无法辨认" }
    if entry.flagged { return "存在争议" }
    if entry.isConfirmed { return "已确认" }
    return "待确认"
  }

  private func statusSymbol(_ entry: ReviewEntry) -> String {
    if entry.unreadable { return "questionmark.diamond" }
    if entry.flagged { return "exclamationmark.triangle.fill" }
    if entry.isConfirmed { return "checkmark.circle.fill" }
    return "circle.dashed"
  }

  private func statusColor(_ entry: ReviewEntry) -> Color {
    if entry.flagged { return .orange }
    if entry.isConfirmed { return .green }
    return .secondary
  }

  private func discrepancyText(_ entry: ReviewEntry) -> String {
    if let expected = entry.expectedOwnID {
      return "序列校验预期本人编号为「\(expected)」。请以扫描原文为准。"
    }
    return "OCR 与结构校验不一致。请核对扫描原文。"
  }

  private func headerLabel(_ entry: ReviewEntry) -> String {
    switch entry.headerKind {
    case "generation": "世代标题原文"
    case "continuation": "续页标题原文"
    case "annotation": "页面注记原文"
    default: "非人物条目原文"
    }
  }
}

private struct IgnoredPageUnavailableView: View {
  let description: String
  var action: (() -> Void)? = nil
  var actionDisabled = false

  var body: some View {
    ContentUnavailableView {
      Label("此页已忽略", systemImage: "minus.circle")
    } description: {
      Text(description)
    } actions: {
      if let action {
        Button("恢复此页", action: action)
          .buttonStyle(.borderedProminent)
          .disabled(actionDisabled)
      }
    }
    .frame(maxWidth: .infinity, maxHeight: .infinity)
  }
}

private enum EntryReviewVisualState: CaseIterable {
  case unreviewed
  case reviewed
  case unreadable
  case flagged

  var label: String {
    switch self {
    case .unreviewed: "待校"
    case .reviewed: "已校"
    case .unreadable: "无法辨认"
    case .flagged: "争议"
    }
  }

  var color: Color {
    switch self {
    case .unreviewed: .blue
    case .reviewed: .green
    case .unreadable: .orange
    case .flagged: .red
    }
  }

  var fillColor: Color {
    switch self {
    case .unreviewed: .clear
    case .reviewed: .green.opacity(0.18)
    case .unreadable: .orange.opacity(0.2)
    case .flagged: .red.opacity(0.14)
    }
  }

  var systemImage: String? {
    switch self {
    case .unreviewed: nil
    case .reviewed: "checkmark"
    case .unreadable: "questionmark"
    case .flagged: "exclamationmark"
    }
  }

  static func resolve(_ entry: ReviewEntry) -> Self {
    if entry.flagged { return .flagged }
    if entry.unreadable { return .unreadable }
    if entry.isConfirmed { return .reviewed }
    return .unreviewed
  }
}

private struct EntryStateLegend: View {
  var body: some View {
    HStack(spacing: 8) {
      ForEach(EntryReviewVisualState.allCases, id: \.self) { state in
        HStack(spacing: 3) {
          ZStack {
            Rectangle()
              .fill(state.fillColor)
            Rectangle()
              .stroke(
                state.color,
                style: StrokeStyle(
                  lineWidth: state == .flagged ? 1.5 : 1,
                  dash: state == .unreadable ? [3, 2] : []
                )
              )
            if let symbol = state.systemImage {
              Image(systemName: symbol)
                .font(.system(size: 6, weight: .bold))
                .foregroundStyle(state.color)
            }
          }
          .frame(width: 13, height: 9)

          Text(state.label)
        }
      }
    }
    .font(.caption2)
    .foregroundStyle(.secondary)
    .accessibilityElement(children: .combine)
    .accessibilityLabel("条目状态：待校、已校、无法辨认、争议")
  }
}

private struct ContextPageCanvas: View {
  let image: NSImage
  let sheet: ReviewSheet
  let sourceSize: CGSize
  let selectedEntryID: String?
  let onSelect: (ReviewEntry) -> Void

  var body: some View {
    GeometryReader { proxy in
      let fitted = aspectFit(
        source: sourceSize,
        destination: proxy.size
      )
      ZStack(alignment: .topLeading) {
        Image(nsImage: image)
          .resizable()
          .interpolation(.high)
          .frame(width: fitted.size.width, height: fitted.size.height)
          .position(x: fitted.midX, y: fitted.midY)
          .shadow(color: .black.opacity(0.24), radius: 10, y: 3)

        ForEach(sheet.entries) { entry in
          if let rect = overlayRect(entry.bbox, fitted: fitted) {
            let state = EntryReviewVisualState.resolve(entry)
            let isSelected = entry.id == selectedEntryID
            Button {
              onSelect(entry)
            } label: {
              ZStack(alignment: .topLeading) {
                Rectangle()
                  .fill(state.fillColor)
                if isSelected {
                  Rectangle()
                    .fill(Color.accentColor.opacity(0.12))
                }
                Rectangle()
                  .strokeBorder(
                    overlayStrokeColor(
                      for: state,
                      bandIndex: entry.bandIndex,
                      selected: isSelected
                    ),
                    style: StrokeStyle(
                      lineWidth: isSelected || state == .flagged ? 2.5 : 1.25,
                      dash: overlayDash(for: entry, state: state, selected: isSelected)
                    )
                  )
                if min(rect.width, rect.height) >= 12,
                  let symbol = state.systemImage
                {
                  Image(systemName: symbol)
                    .font(.system(size: 7, weight: .bold))
                    .foregroundStyle(state.color)
                    .frame(width: 11, height: 11)
                    .background(.black.opacity(0.68), in: Circle())
                    .offset(x: -3, y: -3)
                    .allowsHitTesting(false)
                }
              }
              .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .frame(width: rect.width, height: rect.height)
            .contentShape(Rectangle())
            .position(x: rect.midX, y: rect.midY)
            .accessibilityLabel(
              "\(entry.bandLabel)第 \(entry.entryIndex + 1) 条，\(state.label)"
            )
            .accessibilityAddTraits(
              isSelected ? .isSelected : []
            )
          }
        }
      }
    }
  }

  private func aspectFit(source: CGSize, destination: CGSize) -> CGRect {
    guard source.width > 0, source.height > 0 else { return .zero }
    let scale = min(destination.width / source.width, destination.height / source.height)
    let size = CGSize(width: source.width * scale, height: source.height * scale)
    return CGRect(
      x: (destination.width - size.width) / 2,
      y: (destination.height - size.height) / 2,
      width: size.width,
      height: size.height
    )
  }

  private func overlayRect(_ values: [Double]?, fitted: CGRect) -> CGRect? {
    guard let values, values.count == 4, sourceSize.width > 0,
      sourceSize.height > 0
    else {
      return nil
    }
    let scaleX = fitted.width / sourceSize.width
    let scaleY = fitted.height / sourceSize.height
    return CGRect(
      x: fitted.minX + values[0] * scaleX,
      y: fitted.minY + values[1] * scaleY,
      width: max(1, (values[2] - values[0]) * scaleX),
      height: max(1, (values[3] - values[1]) * scaleY)
    )
  }

  private func bandColor(_ index: Int) -> Color {
    switch index % 4 {
    case 0: .green
    case 1: .blue
    case 2: .orange
    default: .purple
    }
  }

  private func overlayStrokeColor(
    for state: EntryReviewVisualState,
    bandIndex: Int,
    selected: Bool
  ) -> Color {
    if selected { return .white }
    switch state {
    case .flagged: return .red
    case .unreadable: return .orange
    case .unreviewed, .reviewed: return bandColor(bandIndex)
    }
  }

  private func overlayDash(
    for entry: ReviewEntry,
    state: EntryReviewVisualState,
    selected: Bool
  ) -> [CGFloat] {
    if selected || state == .flagged { return [] }
    if state == .unreadable { return [3, 2] }
    return entry.role == "entry" ? [] : [5, 3]
  }
}
