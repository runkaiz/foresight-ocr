import ForesightOCRCore
import SwiftUI

@main
struct ForesightOCRApp: App {
  @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
  @StateObject private var model = ReviewAppModel()

  var body: some Scene {
    WindowGroup("Foresight OCR") {
      RootView(model: model)
        .frame(minWidth: 1_180, minHeight: 720)
        .onAppear { appDelegate.model = model }
    }
    .defaultSize(width: 1_512, height: 982)
    .windowResizability(.contentMinSize)
    .commands {
      CommandGroup(replacing: .newItem) {
        Button("新建项目…") { model.beginNewProject() }
          .keyboardShortcut("n", modifiers: .command)
          .disabled(model.workspaceReady || model.hasActiveManagedOperation)
        Button("打开项目…") { model.beginOpeningExistingProject() }
          .keyboardShortcut("o", modifiers: .command)
          .disabled(model.workspaceReady || model.hasActiveManagedOperation)
      }
      CommandGroup(after: .saveItem) {
        Button("导出校对结果…") { model.exportDocument() }
          .keyboardShortcut("e", modifiers: [.command, .shift])
          .disabled(model.bootstrap == nil || model.isExporting)
        Button("导出为文件夹…") { model.exportFolder() }
          .disabled(model.bootstrap == nil || model.isExporting)
      }
      CommandMenu("校对") {
        Button("上一条") { model.navigateEntry(by: -1) }
          .keyboardShortcut(.upArrow, modifiers: [.control])
          .disabled(model.selectedEntry == nil)
        Button("下一条") { model.navigateEntry(by: 1) }
          .keyboardShortcut(.downArrow, modifiers: [.control])
          .disabled(model.selectedEntry == nil)
        Button("上一页") { model.navigatePage(by: -1) }
          .keyboardShortcut(.leftArrow, modifiers: [.control])
          .disabled(model.selectedPage == nil)
        Button("下一页") { model.navigatePage(by: 1) }
          .keyboardShortcut(.rightArrow, modifiers: [.control])
          .disabled(model.selectedPage == nil)
        Divider()
        Button("确认并跳转") { model.confirmAndAdvance() }
          .keyboardShortcut(.return, modifiers: [.command])
          .disabled(model.selectedEntry == nil || model.isSaving)
        Button("无法辨认") { model.markUnreadable() }
          .keyboardShortcut("u", modifiers: [.control])
          .disabled(model.selectedEntry == nil || model.isSaving)
        Button("取消确认") { model.unconfirm() }
          .keyboardShortcut(.return, modifiers: [.command, .shift])
          .disabled(
            model.selectedEntry?.isConfirmed != true || model.isSaving
          )
        Divider()
        Button(model.selectedSheet?.ignored == true ? "恢复此页" : "忽略此页") {
          model.requestPageIgnoreToggle()
        }
        .disabled(model.selectedSheet == nil || model.isMutatingPage)
        Button("重新识别此页…") { model.requestPageReOCR() }
          .disabled(
            model.selectedSheet?.ignored != false || model.isReOCRingPage
          )
        Button("页面格线修复…") { model.openLayoutRepairPanel() }
          .disabled(model.selectedSheet?.ignored != false)
        Button("全书增量识别…") { model.openDocumentOCR() }
        Divider()
        Button("校对学习审计…") { model.openLearning() }
      }
      CommandGroup(after: .toolbar) {
        Toggle("页面导航", isOn: $model.showSidebar)
        Toggle("页面上下文", isOn: $model.showContext)
        Toggle("校对检查器", isOn: $model.showInspector)
      }
    }
  }
}
