import AppKit

/// The menu bar, as data.
///
/// Every item that drives the app resolves to one command name on the frontend's command
/// bus (`frontend/src/commandBus.ts`). One dispatch path, declared in one table, so a
/// shortcut that stops working has exactly one place to look — and so the menu cannot
/// quietly diverge from what the frontend actually implements.
///
/// Shell-local items (zoom, reload, quit) use selectors instead, because they are the
/// shell's own behaviour and never reach the page.
enum AppCommands {
    struct Item {
        let title: String
        /// A command name on the frontend bus, or `nil` for a separator.
        let command: String?
        let key: String
        let modifiers: NSEvent.ModifierFlags

        static let separator = Item(title: "-", command: nil, key: "", modifiers: [])
    }

    struct Menu {
        let title: String
        let items: [Item]
    }

    private static func item(
        _ title: String,
        _ command: String,
        _ key: String = "",
        _ modifiers: NSEvent.ModifierFlags = .command
    ) -> Item {
        Item(title: title, command: command, key: key, modifiers: modifiers)
    }

    /// Menus whose items all dispatch to the bus. Ordered as they appear in the menu bar.
    static let busMenus: [Menu] = [
        Menu(
            title: "File",
            items: [
                item("Import Statements…", "open:imports", "i"),
                item("Sync Now", "sync", "s"),
            ]
        ),
        Menu(
            title: "Go",
            items: [
                // ⌘1…⌘8, in the order the sidebar presents them, so the numbers are
                // learnable rather than arbitrary.
                item("Dashboard", "navigate:home", "1"),
                item("Net Worth", "navigate:net-worth", "2"),
                item("Projects", "navigate:projects", "3"),
                item("Recurring", "navigate:recurring", "4"),
                item("Uncategorized", "navigate:uncategorized", "5"),
                item("Payroll", "navigate:payroll", "6"),
                item("Investments", "navigate:investments", "7"),
                item("Retirement", "navigate:retirement", "8"),
                .separator,
                item("Getting Started", "open:getting-started", ""),
            ]
        ),
    ]

    /// Items appended to the Edit menu, after the standard clipboard items.
    static let editItems: [Item] = [
        .separator,
        item("Find", "focus:search", "f"),
        item("Clear Filters", "clear:filters", "k", [.command, .shift]),
    ]

    /// Items appended to the View menu, after the shell's own zoom items.
    static let viewItems: [Item] = [
        .separator,
        item("Toggle Sidebar", "toggle:sidebar", "s", [.command, .control]),
        item("Dark Mode", "toggle:dark", "d", [.command, .shift]),
        item("Privacy Mode", "toggle:privacy", "p", [.command, .shift]),
    ]

    /// Item placed in the application menu.
    static let settingsItem = item("Settings…", "open:settings", ",")

    /// Every command name this menu can dispatch. Used by a test to check the shell and
    /// the frontend agree.
    static var allCommandNames: [String] {
        let fromMenus = busMenus.flatMap(\.items)
        return (fromMenus + editItems + viewItems + [settingsItem])
            .compactMap(\.command)
            .sorted()
    }
}
