import AppKit
import FinanceCore
import SwiftUI

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuItemValidation {
    private var instanceGuard: SingleInstanceGuard?
    private var supervisor: BackendSupervisor?
    private var window: NSWindow?
    private var rootController: RootViewController?
    private var activity: NSObjectProtocol?
    private var sweepTimer: Timer?
    private let signalHandler = TerminationSignalHandler()
    private var appearanceObserver: NSKeyValueObservation?
    /// Read once at launch from the environment; see the note where it is set.
    private var privacyMask = false
    /// `layout`, with any chosen data folder applied. What the backend actually gets.
    private lazy var activeLayout = layout

    private let layout = BundleLayout.forRunningApplication()
    private lazy var log = ShellLog(url: layout.shellLogURL)

    func applicationDidFinishLaunching(_ notification: Notification) {
        MainMenu.install()
        log.write("launching")

        // AppKit does not turn a signal into applicationWillTerminate, so without
        // this a `pkill` on the shell orphans the backend -- observed, then fixed.
        signalHandler.install { [weak self] in
            MainActor.assumeIsolated {
                self?.supervisor?.stopSynchronously()
                self?.instanceGuard?.release()
                exit(0)
            }
        }

        do {
            try layout.createSupportDirectories()
        } catch {
            presentFatal(
                "Cannot create the application support directory",
                detail: "\(layout.supportDirectory.path)\n\n\(error.localizedDescription)"
            )
            return
        }

        // Refuse to be the second instance. Two copies would race on one SQLite
        // database, and SQLite reports that as intermittent "database is locked"
        // rather than as the real problem.
        do {
            instanceGuard = try SingleInstanceGuard.acquire(at: layout.instanceLockURL)
        } catch {
            presentFatal(
                "FinanceApp is already running",
                detail: "Only one copy can use the database at a time."
            )
            return
        }

        sweepStaleBackend()

        // App Nap throttles timers in a background app. This prevents the *app*
        // napping without preventing the *system* sleeping, which is what a
        // background sync loop needs (plan §11.2).
        activity = ProcessInfo.processInfo.beginActivity(
            options: .userInitiatedAllowingIdleSystemSleep,
            reason: "Keeping the finance backend responsive"
        )

        // FINANCE_APP_PRIVACY_MASK is read from the shell's own environment rather than
        // being a setting, because its only current caller is the screenshot script: it must
        // be impossible to leave on by accident, and a launch from the Dock never sets it.
        let privacyMask = ["1", "true", "yes"].contains(
            (ProcessInfo.processInfo.environment["FINANCE_APP_PRIVACY_MASK"] ?? "").lowercased()
        )
        if privacyMask { log.write("privacy mask ON: API values are fake") }
        self.privacyMask = privacyMask

        startSupervisorAndWindow()
        observeAppearanceChanges()
        observeSleepWake()
        startPeriodicSweep()
    }

    /// Create the supervisor and its window, and start the backend.
    ///
    /// Separate from `applicationDidFinishLaunching` so choosing a different plugins folder can
    /// redo it: the backend imports plugins once at startup and reads the directory from its
    /// environment, so a new folder means a new child process.
    private func startSupervisorAndWindow() {
        // Where the database lives. Resolved every launch, and it falls back to the app's own copy
        // rather than refusing to start if a chosen folder has moved -- with the reason logged, and
        // the folder actually in use shown in the diagnostics row.
        let resolved = DataDirectory.resolved(default: layout.dataDirectory)
        if let problem = resolved.problem {
            log.write("data folder unusable, using the app's own copy: \(problem.explanation)")
        }
        activeLayout = layout.withDataDirectory(
            DataDirectory.remembered() != nil && resolved.problem == nil ? resolved.url : nil
        )
        log.write("database: \(activeLayout.databaseURL.path)")

        let pluginStatus = PluginDirectory.status()
        switch pluginStatus {
        case .notConfigured:
            log.write("private plugins: not configured; only the bundled templates will load")
        case .ready(let url):
            log.write("private plugins: \(url.lastPathComponent)")
        case .unusable(_, let problem):
            // Loud, because the symptom otherwise appears much later and somewhere else: a
            // statement that will not parse, or an opaque 500 from the uncategorised review.
            log.write("private plugins UNUSABLE: \(problem.explanation)")
        }

        // Multi-device sync, off unless a folder has been chosen. Logged either way: "my devices are
        // not syncing" is otherwise invisible from inside the app.
        let syncFolder = SyncFolder.usable()
        if let syncFolder {
            log.write("device sync via folder: \(syncFolder.path)")
        } else if SyncFolder.remembered() != nil {
            log.write("device sync DISABLED: the configured folder is not usable")
        }

        let supervisor = BackendSupervisor(
            layout: activeLayout,
            environment: BackendEnvironment(
                layout: activeLayout,
                privatePluginsDirectory: pluginStatus.usableDirectory,
                privacyMask: privacyMask,
                syncFolder: syncFolder
            )
        )
        self.supervisor = supervisor
        showWindow(for: supervisor)
        supervisor.start()
    }

    /// Pick the `data/` directory.
    ///
    /// Deliberately only moves the *pointer*. Two SQLite databases that have both been written
    /// cannot be reconciled without inventing an answer for every differing row, and that is a
    /// decision about real financial history — so nothing here copies, merges or deletes data.
    @objc func chooseDataFolder(_ sender: Any?) {
        let panel = NSOpenPanel()
        panel.title = "Choose the data folder"
        panel.message =
            "The folder containing runtime/prod/finances.db. Point this at the same folder the "
            + "container app uses and the two stop drifting apart. Nothing is copied or merged — "
            + "only which database the app opens.\n\nDo not run both apps against it at once: "
            + "SQLite allows one writer, and the second one fails with \"database is locked\"."
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.directoryURL = DataDirectory.remembered() ?? layout.dataDirectory
        panel.prompt = "Use Folder"

        guard panel.runModal() == .OK, let chosen = panel.url else { return }

        if let problem = DataDirectory.diagnose(chosen) {
            let alert = NSAlert()
            alert.alertStyle = .warning
            alert.messageText = "That folder cannot be used"
            alert.informativeText = problem.explanation
            alert.addButton(withTitle: "OK")
            alert.runModal()
            return
        }

        // Say what is there before switching. Pointing at a folder with no database starts an empty
        // one, which looks exactly like losing everything.
        let contents = DataDirectory.contents(of: chosen)
        let confirm = NSAlert()
        confirm.alertStyle = contents.databaseExists ? .informational : .warning
        confirm.messageText = "Use this data folder?"
        confirm.informativeText =
            "\(chosen.path)\n\n\(contents.summary)\n\n"
            + "The app will restart its backend and open that database. Your current one is left "
            + "untouched at \(layout.dataDirectory.path)."
        confirm.addButton(withTitle: "Use It")
        confirm.addButton(withTitle: "Cancel")
        guard confirm.runModal() == .alertFirstButtonReturn else { return }

        DataDirectory.remember(chosen)
        log.write("data folder set to \(chosen.path); restarting the backend")
        rebuildSupervisor()
    }

    func applicationWillTerminate(_ notification: Notification) {
        sweepTimer?.invalidate()
        // Synchronous: there is no time for an async task to unwind here, and a
        // leaked uvicorn holding the SQLite file makes the *next* launch fail in a
        // way that looks nothing like its cause.
        supervisor?.stopSynchronously()
        if let activity { ProcessInfo.processInfo.endActivity(activity) }
        instanceGuard?.release()
    }

    /// Phase 1 has one window and no document model, so closing it should quit.
    /// Revisit if the app grows multiple windows (plan §10 q5).
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }

    // MARK: - Window

    private func showWindow(for supervisor: BackendSupervisor) {
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1160, height: 780),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = "FinanceApp"
        // The ledger is a wide grid; below this it starts crushing columns into
        // ellipses (AGENTS.md caveat #6).
        window.minSize = NSSize(width: 900, height: 560)

        // Native chrome. The single biggest thing separating "a webview in a box" from an
        // app is the title bar: a full-height opaque bar with a centred title reads as a
        // browser window, while content running under a transparent bar reads as Mail,
        // Notes or Xcode.
        //
        // Content runs the full height of the window, under a transparent title bar --
        // the arrangement Mail, Notes and Xcode use, and the clearest single signal that
        // this is an app rather than a browser window.
        //
        // Safe only because the page insets itself: the shell sets --titlebar-height from
        // the real metric and macos.css pads the app shell by it, so the frontend's filter
        // row does not end up under the traffic lights.
        window.styleMask.insert(.fullSizeContentView)
        window.titlebarAppearsTransparent = true
        window.titleVisibility = .hidden
        // Follow the system, so ⌘⇧D inside the app is a *preference* rather than the only
        // way to get a dark window. Without this the frame is light while the content is
        // dark, which is the giveaway that it is a wrapped web page.
        window.appearance = nil
        window.isMovableByWindowBackground = false
        // Opaque, deliberately.
        //
        // This was briefly a non-opaque window over an NSVisualEffectView, so the desktop
        // showed through. It looked right in isolation and wrong in use: a data-dense page
        // composited over whatever wallpaper happens to be behind it turns muddy, the
        // wallpaper's colour bleeds into every panel, and panel-to-panel contrast stops
        // being predictable. macOS uses vibrancy for chrome -- sidebars, toolbars, popovers --
        // not for a full content area, and that distinction is the reason.
        //
        // Worth recording how it got shipped: it was verified with Playwright, which renders
        // the page on a plain background and cannot show a window material at all. The
        // defect was invisible to the only check that was run.
        window.isOpaque = true
        window.backgroundColor = NSColor(
            // Matches the dark theme's --surface-page (#020617) so there is no flash of a
            // different colour before the web content paints.
            srgbRed: 0x02 / 255, green: 0x06 / 255, blue: 0x17 / 255, alpha: 1
        )
        let root = RootViewController(supervisor: supervisor, layout: layout, log: log)
        rootController = root
        window.contentViewController = root
        window.center()
        // Restores size and position across launches. Cheap, and its absence is the
        // kind of thing that makes a webview app feel unfinished.
        window.setFrameAutosaveName("MainWindow")
        window.makeKeyAndOrderFront(nil)
        self.window = window
        NSApp.activate(ignoringOtherApps: true)
    }

    // MARK: - View menu actions
    //
    // AppKit routes unhandled menu actions to the app delegate, so these need no
    // explicit target. They are no-ops while the backend is not ready, which is
    // correct -- there is no page to zoom.

    @objc func zoomIn(_ sender: Any?) { rootController?.activeWebController?.zoomIn() }
    @objc func zoomOut(_ sender: Any?) { rootController?.activeWebController?.zoomOut() }
    @objc func resetZoom(_ sender: Any?) { rootController?.activeWebController?.resetZoom() }
    @objc func reloadApp(_ sender: Any?) { rootController?.activeWebController?.reload() }

    @objc func restartBackend(_ sender: Any?) {
        supervisor?.stop()
        supervisor?.start()
    }

    /// Pick the private plugin repository.
    ///
    /// A shell concern rather than a page in the web Settings: it is a filesystem path, it needs a
    /// real folder picker, and the backend reads it from the environment at *launch*, so changing
    /// it has to restart the child process.
    @objc func choosePluginsFolder(_ sender: Any?) {
        let panel = NSOpenPanel()
        panel.title = "Choose your private plugins folder"
        panel.message =
            "The repository holding your statement parsers and categorisation rules. The app reads "
            + "it in place and appends learned rules to its rules/categories.yaml, so it must be "
            + "your working copy rather than a copy."
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.directoryURL = PluginDirectory.remembered()
        panel.prompt = "Use Folder"

        guard panel.runModal() == .OK, let chosen = panel.url else { return }

        if let problem = PluginDirectory.diagnose(chosen) {
            let alert = NSAlert()
            alert.alertStyle = .warning
            alert.messageText = "That folder cannot be used"
            alert.informativeText = problem.explanation
            alert.addButton(withTitle: "OK")
            alert.runModal()
            return
        }

        PluginDirectory.remember(chosen)
        log.write("private plugins set to \(chosen.lastPathComponent); restarting the backend")
        // Plugins are imported once at startup, so a new folder only takes effect on a restart.
        // Doing it here rather than telling the user to do it: the alternative is an app that
        // silently keeps using the old folder.
        rebuildSupervisor()
    }

    /// Recreate the supervisor so the child is launched with a fresh environment.
    private func rebuildSupervisor() {
        supervisor?.stopSynchronously()
        supervisor = nil
        rootController = nil
        window?.close()
        window = nil
        startSupervisorAndWindow()
    }

    /// The single action behind every bus-backed menu item. The command name travels in
    /// `representedObject`, so adding a command means one line in `AppCommands` rather
    /// than another near-identical method here.
    @objc func dispatchAppCommand(_ sender: NSMenuItem) {
        guard let command = sender.representedObject as? String else { return }
        rootController?.activeWebController?.dispatch(command: command)
    }

    /// Grey out the app-driving items while the backend is not ready. Without this they
    /// look available and do nothing, which is worse than being visibly disabled.
    ///
    /// From NSMenuItemValidation, not an override: NSObject has no such method, and
    /// spelling it `override` fails to compile rather than silently doing nothing.
    func validateMenuItem(_ menuItem: NSMenuItem) -> Bool {
        let webIsLive = rootController?.activeWebController != nil
        switch menuItem.action {
        case #selector(dispatchAppCommand(_:)),
             #selector(reloadApp(_:)),
             #selector(zoomIn(_:)),
             #selector(zoomOut(_:)),
             #selector(resetZoom(_:)):
            return webIsLive
        default:
            return true
        }
    }

    private func presentFatal(_ message: String, detail: String) {
        let alert = NSAlert()
        alert.alertStyle = .critical
        alert.messageText = message
        alert.informativeText = detail
        alert.addButton(withTitle: "Quit")
        alert.runModal()
        NSApp.terminate(nil)
    }

    // MARK: - Stale backend from a previous launch

    private func sweepStaleBackend() {
        let result = StaleBackendSweeper.sweep(
            stateURL: layout.runtimeStateURL,
            expectedExecutable: layout.interpreterURL.resolvingSymlinksInPath().path
        )
        switch result {
        case .terminated(let pid):
            log.write("swept a backend left over from a previous launch (pgid \(pid))")
        case .notOurs:
            // Pid reuse. Signalling a stranger's process group is far worse than
            // failing to clean up, so this deliberately does nothing.
            log.write("recorded backend pid now belongs to another process; left alone")
        case .alreadyGone, .nothingRecorded:
            break
        }
    }

    // MARK: - Appearance

    /// Keep the web content's light/dark mode in step with System Settings.
    ///
    /// A Mac app follows the system by default; a web app remembers its own toggle. Left
    /// alone the two disagree, and a dark window frame around light content is the most
    /// obvious tell that the inside is a web page.
    private func observeAppearanceChanges() {
        appearanceObserver = NSApp.observe(\.effectiveAppearance) { [weak self] _, _ in
            MainActor.assumeIsolated { self?.pushAppearanceToWebContent() }
        }
        pushAppearanceToWebContent()
    }

    private func pushAppearanceToWebContent() {
        let isDark =
            NSApp.effectiveAppearance.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua
        rootController?.activeWebController?.setAppearance(dark: isDark)
    }

    // MARK: - Sleep, wake, and the periodic sweep

    private func observeSleepWake() {
        let center = NSWorkspace.shared.notificationCenter
        center.addObserver(
            forName: NSWorkspace.didWakeNotification, object: nil, queue: .main
        ) { [weak self] _ in
            // A Timer does not fire while the machine is asleep, so anything due
            // during sleep simply never happened. Re-derive on wake instead of
            // trusting the schedule (plan §11.2).
            MainActor.assumeIsolated { self?.supervisor?.revalidate() }
        }
    }

    private func startPeriodicSweep() {
        // Pairs with the wake handler: the sweep re-derives "is anything overdue?"
        // from state, so a missed timer costs at most one interval.
        let timer = Timer(timeInterval: 60, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.supervisor?.revalidate() }
        }
        RunLoop.main.add(timer, forMode: .common)
        sweepTimer = timer
    }
}
