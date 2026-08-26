import AppKit

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
  weak var model: ReviewAppModel?

  func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
    guard let model, model.hasActiveManagedOperation else { return .terminateNow }
    if model.shouldWarnBeforeTermination {
      let alert = NSAlert()
      alert.alertStyle = .warning
      alert.messageText = "本机处理仍在进行"
      alert.informativeText =
        "现在退出会停止当前任务。已经完成的项目数据会保留，下次可以重试。"
      alert.addButton(withTitle: "停止并退出")
      alert.addButton(withTitle: "继续处理")
      guard alert.runModal() == .alertFirstButtonReturn else {
        return .terminateCancel
      }
    }
    Task {
      await model.shutdown()
      sender.reply(toApplicationShouldTerminate: true)
    }
    return .terminateLater
  }
}
