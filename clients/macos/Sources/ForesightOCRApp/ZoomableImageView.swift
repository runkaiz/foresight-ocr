import AppKit
import SwiftUI

struct ZoomableImageView: NSViewRepresentable {
  let image: NSImage
  let zoom: CGFloat
  let zoomCommand: Int
  let fitCommand: Int

  func makeCoordinator() -> Coordinator { Coordinator() }

  func makeNSView(context: Context) -> NSScrollView {
    let scrollView = CropScrollView()
    scrollView.contentView = CenteringClipView()
    scrollView.drawsBackground = true
    scrollView.backgroundColor = NSColor(calibratedWhite: 0.035, alpha: 1)
    scrollView.hasHorizontalScroller = true
    scrollView.hasVerticalScroller = true
    scrollView.allowsMagnification = true
    scrollView.minMagnification = 0.05
    scrollView.maxMagnification = 8
    scrollView.autohidesScrollers = true

    let imageView = NSImageView()
    imageView.imageAlignment = .alignCenter
    imageView.imageFrameStyle = .none
    imageView.imageScaling = .scaleNone
    scrollView.documentView = imageView
    context.coordinator.imageView = imageView
    scrollView.onViewportResize = { [weak scrollView, weak coordinator = context.coordinator] in
      guard coordinator?.isFitHeightActive == true,
        let scrollView,
        let imageView = coordinator?.imageView
      else {
        return
      }
      fitHeight(scrollView, imageSize: imageView.frame.size)
    }
    scrollView.onManualMagnify = { [weak coordinator = context.coordinator] in
      coordinator?.isFitHeightActive = false
    }
    return scrollView
  }

  func updateNSView(_ scrollView: NSScrollView, context: Context) {
    guard let imageView = context.coordinator.imageView else { return }
    let identity = ObjectIdentifier(image)
    if context.coordinator.imageIdentity != identity {
      context.coordinator.imageIdentity = identity
      imageView.image = image
      let size = pixelSize(of: image)
      imageView.frame = CGRect(origin: .zero, size: size)
      scrollView.documentView?.frame = CGRect(origin: .zero, size: size)
      context.coordinator.lastFitCommand = fitCommand
      context.coordinator.lastZoomCommand = zoomCommand
      context.coordinator.isFitHeightActive = true
      DispatchQueue.main.async { fitHeight(scrollView, imageSize: size) }
      return
    }

    if context.coordinator.lastFitCommand != fitCommand {
      context.coordinator.lastFitCommand = fitCommand
      context.coordinator.isFitHeightActive = true
      fitHeight(scrollView, imageSize: imageView.frame.size)
    }
    if context.coordinator.lastZoomCommand != zoomCommand {
      context.coordinator.lastZoomCommand = zoomCommand
      context.coordinator.isFitHeightActive = false
      scrollView.setMagnification(
        min(scrollView.maxMagnification, max(scrollView.minMagnification, zoom)),
        centeredAt: NSPoint(
          x: imageView.frame.midX,
          y: imageView.frame.midY
        )
      )
    }
  }

  private func pixelSize(of image: NSImage) -> NSSize {
    var rect = NSRect(origin: .zero, size: image.size)
    if let cgImage = image.cgImage(forProposedRect: &rect, context: nil, hints: nil) {
      return NSSize(width: cgImage.width, height: cgImage.height)
    }
    return image.size
  }

  private func fitHeight(_ scrollView: NSScrollView, imageSize: NSSize) {
    guard imageSize.width > 0, imageSize.height > 0 else { return }
    let viewport = scrollView.contentSize
    guard viewport.width > 0, viewport.height > 0 else { return }
    let availableHeight = max(1, viewport.height - 24)
    let scale = min(
      scrollView.maxMagnification,
      max(scrollView.minMagnification, availableHeight / imageSize.height)
    )
    scrollView.setMagnification(
      scale,
      centeredAt: NSPoint(x: imageSize.width / 2, y: imageSize.height / 2)
    )
    scrollView.layoutSubtreeIfNeeded()

    let visibleWidth = viewport.width / scale
    let visibleHeight = viewport.height / scale
    scrollView.contentView.scroll(
      to: NSPoint(
        x: max(0, (imageSize.width - visibleWidth) / 2),
        y: max(0, (imageSize.height - visibleHeight) / 2)
      )
    )
    (scrollView.contentView as? CenteringClipView)?.recenterDocument()
    scrollView.reflectScrolledClipView(scrollView.contentView)
  }

  final class Coordinator {
    weak var imageView: NSImageView?
    var imageIdentity: ObjectIdentifier?
    var lastZoomCommand = -1
    var lastFitCommand = -1
    var isFitHeightActive = true
  }
}

private final class CropScrollView: NSScrollView {
  var onViewportResize: (() -> Void)?
  var onManualMagnify: (() -> Void)?

  override func layout() {
    super.layout()
    (contentView as? CenteringClipView)?.recenterDocument()
  }

  override func viewDidEndLiveResize() {
    super.viewDidEndLiveResize()
    onViewportResize?()
  }

  override func magnify(with event: NSEvent) {
    onManualMagnify?()
    super.magnify(with: event)
  }
}

private final class CenteringClipView: NSClipView {
  func recenterDocument() {
    let centeredBounds = constrainBoundsRect(bounds)
    guard centeredBounds.origin != bounds.origin else { return }
    setBoundsOrigin(centeredBounds.origin)
  }

  override func constrainBoundsRect(_ proposedBounds: NSRect) -> NSRect {
    var bounds = super.constrainBoundsRect(proposedBounds)
    guard let documentView else { return bounds }
    if documentView.frame.width < bounds.width {
      bounds.origin.x = -(bounds.width - documentView.frame.width) / 2
    }
    if documentView.frame.height < bounds.height {
      bounds.origin.y = -(bounds.height - documentView.frame.height) / 2
    }
    return bounds
  }
}
