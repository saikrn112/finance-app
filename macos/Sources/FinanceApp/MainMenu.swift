import AppKit

/// The minimum menu bar an AppKit app needs to be usable.
///
/// Without a main menu there is no ⌘Q, so the only way to quit is to kill the
/// process -- which is exactly the path that orphans the backend. Phase 3 replaces
/// this with real in-app commands dispatched into the webview; until then it exists
/// so that "clean shutdown" is reachable by a user rather than only by a script.
@MainActor
enum MainMenu {
    static func install(applicationName: String = "FinanceApp") {
        let mainMenu = NSMenu()

        // The first item's own submenu is the application menu; its title comes from
        // the bundle, not from this string.
        let appMenuItem = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(
            withTitle: "About \(applicationName)",
            action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)),
            keyEquivalent: ""
        )
        appMenu.addItem(.separator())
        appMenu.addItem(
            withTitle: "Hide \(applicationName)",
            action: #selector(NSApplication.hide(_:)),
            keyEquivalent: "h"
        )
        appMenu.addItem(.separator())
        appMenu.addItem(
            withTitle: "Quit \(applicationName)",
            action: #selector(NSApplication.terminate(_:)),
            keyEquivalent: "q"
        )
        appMenuItem.submenu = appMenu
        mainMenu.addItem(appMenuItem)

        // Edit, purely so that Copy works in the selectable path and log text. A
        // text view that cannot be copied from is a diagnostics panel nobody can
        // paste into a bug report.
        let editMenuItem = NSMenuItem()
        let editMenu = NSMenu(title: "Edit")
        editMenu.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        editMenu.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        editMenu.addItem(
            withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a"
        )
        editMenuItem.submenu = editMenu
        mainMenu.addItem(editMenuItem)

        // View. The zoom items drive WKWebView's pageZoom explicitly, because pinch
        // magnification is disabled: it fights ag-grid's row virtualisation and
        // WKWebView's rubber-band scrolling, and the loser is whichever the user
        // actually meant to scroll.
        let viewMenuItem = NSMenuItem()
        let viewMenu = NSMenu(title: "View")
        viewMenu.addItem(withTitle: "Reload", action: Selector(("reloadApp:")), keyEquivalent: "r")
        viewMenu.addItem(.separator())
        viewMenu.addItem(withTitle: "Actual Size", action: Selector(("resetZoom:")), keyEquivalent: "0")
        viewMenu.addItem(withTitle: "Zoom In", action: Selector(("zoomIn:")), keyEquivalent: "+")
        viewMenu.addItem(withTitle: "Zoom Out", action: Selector(("zoomOut:")), keyEquivalent: "-")
        viewMenu.addItem(.separator())
        viewMenu.addItem(
            withTitle: "Restart Backend", action: Selector(("restartBackend:")), keyEquivalent: ""
        )
        viewMenuItem.submenu = viewMenu
        mainMenu.addItem(viewMenuItem)

        let windowMenuItem = NSMenuItem()
        let windowMenu = NSMenu(title: "Window")
        windowMenu.addItem(
            withTitle: "Minimize",
            action: #selector(NSWindow.performMiniaturize(_:)),
            keyEquivalent: "m"
        )
        windowMenu.addItem(
            withTitle: "Close", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w"
        )
        windowMenuItem.submenu = windowMenu
        mainMenu.addItem(windowMenuItem)

        NSApp.mainMenu = mainMenu
        NSApp.windowsMenu = windowMenu
    }
}
