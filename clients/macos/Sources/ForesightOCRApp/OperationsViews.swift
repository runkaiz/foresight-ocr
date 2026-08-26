import AppKit
import ForesightOCRCore
import SwiftUI

struct DocumentOCRSheet: View {
  @ObservedObject var model: ReviewAppModel
  @Environment(\.dismiss) private var dismiss

  var body: some View {
    VStack(alignment: .leading, spacing: 18) {
      HStack {
        VStack(alignment: .leading, spacing: 4) {
          Text("全书增量识别")
            .font(.title2.weight(.semibold))
          Text("全分辨率去水印图像 · 保留人工校对")
            .foregroundStyle(.secondary)
        }
        Spacer()
        Button("完成") { dismiss() }
          .keyboardShortcut(.cancelAction)
      }

      if let job = model.documentOCRJob {
        GroupBox {
          VStack(alignment: .leading, spacing: 12) {
            HStack {
              Label(statusTitle(job.status), systemImage: statusSymbol(job.status))
              Spacer()
              Text("\(job.percent, format: .number.precision(.fractionLength(1)))%")
                .monospacedDigit()
            }
            ProgressView(value: job.percent, total: 100)
            HStack {
              Text("页面 \(job.currentPagePosition)/\(job.totalPages)")
              Spacer()
              Text("区域 \(job.completedRegions)/\(job.totalRegions)")
            }
            .font(.caption.monospacedDigit())
            .foregroundStyle(.secondary)
            if let error = job.error {
              Text(error)
                .font(.callout)
                .foregroundStyle(.red)
                .textSelection(.enabled)
            }
          }
          .padding(4)
        }
      } else {
        ProgressView("正在读取任务状态…")
          .frame(maxWidth: .infinity, alignment: .leading)
      }

      HStack {
        Text("任务在本机服务中运行；关闭此面板不会取消任务。")
          .font(.caption)
          .foregroundStyle(.secondary)
        Spacer()
        Button("开始增量识别", action: model.requestDocumentOCR)
          .buttonStyle(.borderedProminent)
          .disabled(
            model.isStartingDocumentOCR
              || ["queued", "running"].contains(model.documentOCRJob?.status)
          )
      }
    }
    .padding(22)
    .frame(width: 560)
  }

  private func statusTitle(_ status: String) -> String {
    switch status {
    case "queued": "已排队"
    case "running": "正在识别"
    case "complete": "识别完成"
    case "error": "识别失败"
    default: "尚未运行"
    }
  }

  private func statusSymbol(_ status: String) -> String {
    switch status {
    case "queued": "clock"
    case "running": "waveform"
    case "complete": "checkmark.circle.fill"
    case "error": "exclamationmark.triangle.fill"
    default: "circle.dashed"
    }
  }
}

struct LearningSheet: View {
  @ObservedObject var model: ReviewAppModel
  @Environment(\.dismiss) private var dismiss

  var body: some View {
    VStack(alignment: .leading, spacing: 18) {
      HStack {
        VStack(alignment: .leading, spacing: 4) {
          Text("校对学习审计")
            .font(.title2.weight(.semibold))
          Text("只评测当前人工校对；不会运行 OCR 或启用规则。")
            .foregroundStyle(.secondary)
        }
        Spacer()
        Button("完成") { dismiss() }
          .keyboardShortcut(.cancelAction)
      }

      if let snapshot = model.learningSnapshot {
        if let report = snapshot.report {
          HStack(spacing: 12) {
            LearningMetric(
              title: "三字段全对",
              value: percent(report.exactCoreRate)
            )
            LearningMetric(
              title: "完整样本",
              value: String(report.eligibleEntries)
            )
            LearningMetric(
              title: "待重新分析",
              value: String(snapshot.pendingCorrections)
            )
          }
          if snapshot.comparison?.status == "lower" {
            Label("本次全对率下降，请在采用任何规则前检查差异。", systemImage: "exclamationmark.triangle.fill")
              .foregroundStyle(.orange)
          }
        } else {
          ContentUnavailableView(
            "尚无学习快照",
            systemImage: "chart.bar.doc.horizontal",
            description: Text("运行分析会回放现有人工校对，不会启动识别。")
          )
        }
      } else {
        ProgressView("正在读取审计状态…")
          .frame(maxWidth: .infinity, maxHeight: .infinity)
      }

      HStack {
        Button("查看报告", action: model.openLearningReport)
          .disabled(model.learningSnapshot?.report == nil)
        Spacer()
        Button("重新分析", action: model.runLearning)
          .buttonStyle(.borderedProminent)
          .disabled(model.isRunningLearning)
      }
    }
    .padding(22)
    .frame(width: 620, height: 360)
  }

  private func percent(_ value: Double) -> String {
    value.formatted(.percent.precision(.fractionLength(1)))
  }
}

private struct LearningMetric: View {
  let title: String
  let value: String

  var body: some View {
    VStack(alignment: .leading, spacing: 6) {
      Text(title)
        .font(.caption)
        .foregroundStyle(.secondary)
      Text(value)
        .font(.title2.weight(.semibold).monospacedDigit())
    }
    .frame(maxWidth: .infinity, alignment: .leading)
    .padding(14)
    .background(.quaternary.opacity(0.55), in: RoundedRectangle(cornerRadius: 10))
  }
}

struct LearningReportSheet: View {
  let markdown: String
  @Environment(\.dismiss) private var dismiss

  var body: some View {
    VStack(spacing: 0) {
      HStack {
        Text("校对学习报告")
          .font(.headline)
        Spacer()
        Button("完成") { dismiss() }
          .keyboardShortcut(.cancelAction)
      }
      .padding(14)
      Divider()
      ScrollView {
        Text((try? AttributedString(markdown: markdown)) ?? AttributedString(markdown))
          .frame(maxWidth: .infinity, alignment: .leading)
          .textSelection(.enabled)
          .padding(22)
      }
    }
    .frame(width: 720, height: 620)
  }
}

struct LayoutRepairSheet: View {
  @ObservedObject var model: ReviewAppModel
  @Environment(\.dismiss) private var dismiss

  var body: some View {
    HStack(spacing: 0) {
      Group {
        if let image = model.contextImage, let preview = model.combPreview {
          CombPreviewCanvas(image: image, preview: preview)
        } else {
          ProgressView("正在计算页面格线…")
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
      }
      .frame(minWidth: 560, maxWidth: .infinity, maxHeight: .infinity)
      .background(Color(nsColor: .underPageBackgroundColor))

      Divider()

      VStack(spacing: 0) {
        HStack {
          VStack(alignment: .leading, spacing: 3) {
            Text("页面格线修复")
              .font(.title3.weight(.semibold))
            Text("预览与应用共享同一组参数")
              .font(.caption)
              .foregroundStyle(.secondary)
          }
          Spacer()
          Button("完成") { dismiss() }
            .keyboardShortcut(.cancelAction)
        }
        .padding(16)

        Divider()

        Form {
          Section("格线参数") {
            LabeledContent("相位调整") {
              TextField(
                "相位",
                value: $model.combPhase,
                format: .number.precision(.fractionLength(2))
              )
              .frame(width: 110)
            }
            LabeledContent("列距") {
              TextField(
                "列距",
                value: $model.combPitch,
                format: .number.precision(.fractionLength(2))
              )
              .frame(width: 110)
            }
            LabeledContent("文字左界") {
              TextField("左界", value: $model.combTextLeft, format: .number)
                .frame(width: 110)
            }
            LabeledContent("文字右界") {
              TextField("右界", value: $model.combTextRight, format: .number)
                .frame(width: 110)
            }
            Toggle("吸附到检测沟槽", isOn: $model.combSnap)
          }

          if let preview = model.combPreview {
            Section("边界微调") {
              ForEach(Array(preview.boundaries.enumerated()), id: \.offset) {
                index, boundary in
                HStack {
                  Text("#\(index + 1)")
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(.secondary)
                  TextField(
                    "位置",
                    value: Binding(
                      get: {
                        model.combBoundaryOverrides[index] ?? boundary
                      },
                      set: { model.setBoundaryOverride(index, value: $0) }
                    ),
                    format: .number.precision(.fractionLength(1))
                  )
                  Button {
                    model.setBoundaryOverride(index, value: nil)
                  } label: {
                    Image(systemName: "arrow.counterclockwise")
                  }
                  .buttonStyle(.borderless)
                  .disabled(model.combBoundaryOverrides[index] == nil)
                  .help("恢复自动位置")
                }
              }
            }

            Section("结果") {
              LabeledContent("每代条目", value: String(preview.entriesPerBand))
              LabeledContent("总区域", value: String(preview.entries))
              LabeledContent(
                "列距置信度",
                value: preview.pitchConfidence?.formatted(
                  .percent.precision(.fractionLength(1))
                ) ?? "—"
              )
            }
          }
        }
        .formStyle(.grouped)

        Divider()
        HStack {
          Button("更新预览", action: model.updateCombPreview)
            .disabled(model.isLoadingCombPreview || model.isApplyingRecut)
          Spacer()
          Button("应用并重新识别…", action: model.requestApplyRecut)
            .buttonStyle(.borderedProminent)
            .disabled(model.combPreview == nil || model.isApplyingRecut)
        }
        .padding(14)
      }
      .frame(width: 390)
    }
    .frame(width: 1_120, height: 760)
  }
}

private struct CombPreviewCanvas: View {
  let image: NSImage
  let preview: CombPreview

  var body: some View {
    GeometryReader { proxy in
      let imageSize = pixelSize
      let scale = min(
        (proxy.size.width - 36) / max(1, imageSize.width),
        (proxy.size.height - 36) / max(1, imageSize.height)
      )
      let size = CGSize(width: imageSize.width * scale, height: imageSize.height * scale)
      let origin = CGPoint(
        x: (proxy.size.width - size.width) / 2,
        y: (proxy.size.height - size.height) / 2
      )
      ZStack(alignment: .topLeading) {
        Image(nsImage: image)
          .resizable()
          .interpolation(.high)
          .frame(width: size.width, height: size.height)
          .position(x: origin.x + size.width / 2, y: origin.y + size.height / 2)
          .shadow(radius: 10, y: 3)
        ForEach(Array(preview.boundaries.enumerated()), id: \.offset) {
          index, boundary in
          let x = origin.x + boundary * scale
          Path { path in
            path.move(to: CGPoint(x: x, y: origin.y))
            path.addLine(to: CGPoint(x: x, y: origin.y + size.height))
          }
          .stroke(
            preview.manual.indices.contains(index) && preview.manual[index]
              ? Color.purple : Color.accentColor,
            style: StrokeStyle(lineWidth: 1.5, dash: [6, 3])
          )
        }
      }
    }
    .padding(18)
  }

  private var pixelSize: CGSize {
    var rect = NSRect(origin: .zero, size: image.size)
    if let cgImage = image.cgImage(forProposedRect: &rect, context: nil, hints: nil) {
      return CGSize(width: cgImage.width, height: cgImage.height)
    }
    return image.size
  }
}
