import AppKit

/// A transparent strip across the top of the window that drags it.
///
/// ## Why this is AppKit and not CSS
///
/// The first attempt was a CSS pseudo-element with `-webkit-app-region: drag`. That does
/// nothing here for two independent reasons, either of which alone would have been fatal:
///
///  1. `-webkit-app-region` is an **Electron** extension. WKWebView does not implement it, so
///     the property was inert.
///  2. The same rule set `pointer-events: none` — added to stop the strip swallowing clicks —
///     which would have prevented it acting as a drag region even in a browser that did
///     support the property.
///
/// The result was a window with no draggable area at all: `fullSizeContentView` means the web
/// content covers the title bar, and a WKWebView consumes the mouse events that
/// `isMovableByWindowBackground` would otherwise use.
///
/// The working mechanism is `mouseDownCanMoveWindow`. AppKit asks the view under the cursor,
/// and if it says yes the window moves — no event handling, no gesture recognisers, and it
/// composes correctly with a double-click to zoom.
final class TitlebarDragView: NSView {
    /// This is the whole implementation. AppKit consults it per mouse-down on the hit view.
    override var mouseDownCanMoveWindow: Bool { true }

    /// Transparent, and deliberately not `isOpaque = false` + a drawn fill: the strip must show
    /// whatever the page paints behind it, so it cannot become a visible bar.
    override func draw(_ dirtyRect: NSRect) {}

    /// Double-click to zoom, which is standard title-bar behaviour and is what a user tries
    /// immediately after discovering the strip can be dragged.
    override func mouseUp(with event: NSEvent) {
        guard event.clickCount == 2 else {
            super.mouseUp(with: event)
            return
        }
        // Respect the system preference, rather than assuming double-click means zoom.
        let action = UserDefaults.standard.string(forKey: "AppleActionOnDoubleClick")
        switch action {
        case "Minimize":
            window?.miniaturize(nil)
        case "None":
            break
        default:
            window?.zoom(nil)
        }
    }
}
