// Print the CoreGraphics window id of an app's frontmost on-screen window.
//
// Usage: window_id <owner-name>
//
// Exists so screenshots do not need an Accessibility grant. Reading a window's position
// through System Events is a UI-scripting operation and requires Accessibility; asking
// CoreGraphics for the window list does not, and `screencapture -l <id>` then captures
// exactly that window regardless of what is in front of it. Only Screen Recording is
// needed, and that is also what screencapture itself requires.

import CoreGraphics
import Foundation

let arguments = CommandLine.arguments
guard arguments.count > 1 else {
    FileHandle.standardError.write(Data("usage: window_id <owner-name>\n".utf8))
    exit(2)
}
let owner = arguments[1]

guard
    let windows = CGWindowListCopyWindowInfo(
        [.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID
    ) as? [[String: Any]]
else {
    FileHandle.standardError.write(Data("could not read the window list\n".utf8))
    exit(1)
}

// Layer 0 is the normal window layer. Filtering it out excludes menu bars, tooltips and
// the like, which would otherwise be picked ahead of the real window. Ordering from
// CGWindowListCopyWindowInfo is front-to-back, so the first match is the frontmost.
let candidates = windows.filter { window in
    (window[kCGWindowOwnerName as String] as? String) == owner
        && (window[kCGWindowLayer as String] as? Int) == 0
}

// Skip degenerate windows: an app can own a 1x1 offscreen helper window, and capturing
// that yields a valid-looking but useless image.
let usable = candidates.first { window in
    guard let bounds = window[kCGWindowBounds as String] as? [String: Any],
          let width = bounds["Width"] as? Double,
          let height = bounds["Height"] as? Double
    else { return false }
    return width > 100 && height > 100
}

guard let window = usable, let id = window[kCGWindowNumber as String] as? Int else {
    FileHandle.standardError.write(Data("no on-screen window owned by \(owner)\n".utf8))
    exit(1)
}
print(id)
