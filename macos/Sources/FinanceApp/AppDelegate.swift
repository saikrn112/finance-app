import AppKit
import FinanceCore
import SwiftUI

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    private var instanceGuard: SingleInstanceGuard?
    private var supervisor: BackendSupervisor?
    private var window: NSWindow?
    private var activity: NSObjectProtocol?
    private var sweepTimer: Timer?
    private let signalHandler = TerminationSignalHandler()

    private let layout = BundleLayout.forRunningApplication()

    func applicationDidFinishLaunching(_ notification: Notification) {
        MainMenu.install()

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

        let supervisor = BackendSupervisor(
            layout: layout,
            environment: BackendEnvironment(layout: layout)
        )
        self.supervisor = supervisor

        showWindow(for: supervisor)
        observeSleepWake()
        startPeriodicSweep()
        supervisor.start()
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
            contentRect: NSRect(x: 0, y: 0, width: 620, height: 460),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = "FinanceApp"
        window.contentView = NSHostingView(
            rootView: BackendStatusView(supervisor: supervisor, layout: layout)
        )
        window.center()
        // Restores size and position across launches. Cheap, and its absence is the
        // kind of thing that makes a webview app feel unfinished.
        window.setFrameAutosaveName("MainWindow")
        window.makeKeyAndOrderFront(nil)
        self.window = window
        NSApp.activate(ignoringOtherApps: true)
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
            NSLog("swept a backend left over from a previous launch (pgid %d)", pid)
        case .notOurs:
            // Pid reuse. Signalling a stranger's process group is far worse than
            // failing to clean up, so this deliberately does nothing.
            NSLog("recorded backend pid now belongs to another process; left alone")
        case .alreadyGone, .nothingRecorded:
            break
        }
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
