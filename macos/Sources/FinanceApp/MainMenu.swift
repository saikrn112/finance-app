import AppKit

/// Builds the menu bar.
///
/// An AppKit app with no main menu has no ⌘Q at all, so the only way to quit is to kill the
/// process — which is exactly the path that orphans the backend. That is why even the
/// minimum version of this was part of phase 1.
///
/// Items that drive the app carry their bus command name in `representedObject` and share
/// one action. The alternative — a selector per command — means twenty near-identical
/// methods on the delegate, and one of them being wrong is invisible until someone presses
/// that key.
@MainActor
enum MainMenu {
    /// The shared action for every bus-backed item. `#selector` rather than a string, so a
    /// renamed method breaks the build instead of breaking a menu item at runtime.
    static let dispatchSelector = #selector(AppDelegate.dispatchAppCommand(_:))

    static func install(applicationName: String = "FinanceApp") {
        let mainMenu = NSMenu()

        mainMenu.addItem(applicationMenuItem(applicationName: applicationName))
        mainMenu.addItem(editMenuItem())
        mainMenu.addItem(viewMenuItem())
        for menu in AppCommands.busMenus {
            mainMenu.addItem(busMenuItem(menu))
        }
        let windowMenu = NSMenu(title: "Window")
        mainMenu.addItem(windowMenuItem(windowMenu))

        NSApp.mainMenu = mainMenu
        NSApp.windowsMenu = windowMenu
    }

    // MARK: - Menus

    private static func applicationMenuItem(applicationName: String) -> NSMenuItem {
        // The first item's submenu is the application menu; its title comes from the
        // bundle, not from anything set here.
        let container = NSMenuItem()
        let menu = NSMenu()
        menu.addItem(
            withTitle: "About \(applicationName)",
            action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)),
            keyEquivalent: ""
        )
        menu.addItem(.separator())
        if let settings = commandItem(AppCommands.settingsItem) {
            menu.addItem(settings)
        }
        // Shell-local: a filesystem path needs a real folder picker, and the backend reads it at
        // launch, so changing it restarts the child.
        menu.addItem(
            withTitle: "Private Plugins Folder…",
            action: #selector(AppDelegate.choosePluginsFolder(_:)),
            keyEquivalent: ""
        )
        menu.addItem(.separator())
        menu.addItem(
            withTitle: "Hide \(applicationName)",
            action: #selector(NSApplication.hide(_:)),
            keyEquivalent: "h"
        )
        menu.addItem(.separator())
        menu.addItem(
            withTitle: "Quit \(applicationName)",
            action: #selector(NSApplication.terminate(_:)),
            keyEquivalent: "q"
        )
        container.submenu = menu
        return container
    }

    private static func editMenuItem() -> NSMenuItem {
        let container = NSMenuItem()
        let menu = NSMenu(title: "Edit")
        // Standard clipboard items, so text in the app (and in the diagnostics panel) can
        // be copied at all. A panel nobody can paste from is not a diagnostics panel.
        menu.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        menu.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        menu.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        menu.addItem(
            withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a"
        )
        append(AppCommands.editItems, to: menu)
        container.submenu = menu
        return container
    }

    private static func viewMenuItem() -> NSMenuItem {
        let container = NSMenuItem()
        let menu = NSMenu(title: "View")
        // Shell-local: these never reach the page. The zoom items drive WKWebView's
        // pageZoom explicitly because pinch magnification is disabled — it fights
        // ag-grid's row virtualisation, and the loser is whichever the user meant to
        // scroll.
        menu.addItem(withTitle: "Reload", action: #selector(AppDelegate.reloadApp(_:)), keyEquivalent: "r")
        menu.addItem(.separator())
        menu.addItem(withTitle: "Actual Size", action: #selector(AppDelegate.resetZoom(_:)), keyEquivalent: "0")
        menu.addItem(withTitle: "Zoom In", action: #selector(AppDelegate.zoomIn(_:)), keyEquivalent: "+")
        menu.addItem(withTitle: "Zoom Out", action: #selector(AppDelegate.zoomOut(_:)), keyEquivalent: "-")
        append(AppCommands.viewItems, to: menu)
        menu.addItem(.separator())
        menu.addItem(
            withTitle: "Restart Backend", action: #selector(AppDelegate.restartBackend(_:)),
            keyEquivalent: ""
        )
        container.submenu = menu
        return container
    }

    private static func busMenuItem(_ definition: AppCommands.Menu) -> NSMenuItem {
        let container = NSMenuItem()
        let menu = NSMenu(title: definition.title)
        append(definition.items, to: menu)
        container.submenu = menu
        return container
    }

    private static func windowMenuItem(_ menu: NSMenu) -> NSMenuItem {
        let container = NSMenuItem()
        menu.addItem(
            withTitle: "Minimize",
            action: #selector(NSWindow.performMiniaturize(_:)),
            keyEquivalent: "m"
        )
        menu.addItem(
            withTitle: "Close", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w"
        )
        container.submenu = menu
        return container
    }

    // MARK: - Items

    private static func append(_ items: [AppCommands.Item], to menu: NSMenu) {
        for item in items {
            menu.addItem(commandItem(item) ?? .separator())
        }
    }

    /// `nil` for a separator.
    private static func commandItem(_ definition: AppCommands.Item) -> NSMenuItem? {
        guard let command = definition.command else { return nil }
        let item = NSMenuItem(
            title: definition.title, action: dispatchSelector, keyEquivalent: definition.key
        )
        item.keyEquivalentModifierMask = definition.modifiers
        item.representedObject = command
        return item
    }
}
