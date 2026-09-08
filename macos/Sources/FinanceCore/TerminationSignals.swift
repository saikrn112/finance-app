import Darwin
import Foundation

/// Runs a handler on SIGTERM / SIGINT / SIGHUP.
///
/// AppKit does not turn a signal into `applicationWillTerminate`: the default action
/// kills the process outright, so the backend is orphaned and keeps the SQLite file
/// open. That was observed, not assumed -- `pkill` on the shell left a live uvicorn
/// behind and the next launch had to sweep it.
///
/// `StaleBackendSweeper` is still the backstop, because `SIGKILL` cannot be caught
/// and a panic cannot be handled. This just removes the common case.
public final class TerminationSignalHandler: @unchecked Sendable {
    private var sources: [DispatchSourceSignal] = []

    public static let handledSignals: [Int32] = [SIGTERM, SIGINT, SIGHUP]

    public init() {}

    /// - Parameter handler: called once, on the main queue, for the first signal
    ///   received. It must be fast and synchronous -- the process is on its way out.
    public func install(handler: @escaping @Sendable () -> Void) {
        let fired = ManagedAtomicFlag()
        for number in Self.handledSignals {
            // The DispatchSource only observes; the default action still applies
            // unless it is explicitly ignored first, and the default action for all
            // three of these is to terminate immediately.
            signal(number, SIG_IGN)
            let source = DispatchSource.makeSignalSource(signal: number, queue: .main)
            source.setEventHandler {
                // A second signal while shutdown is already running must not start a
                // second teardown; recording *that a signal was handled* rather than
                // *that one is in flight* is the distinction that matters.
                guard fired.testAndSet() else { return }
                handler()
            }
            source.resume()
            sources.append(source)
        }
    }
}

/// A one-way flag. Small enough not to justify a dependency on Atomics.
final class ManagedAtomicFlag: @unchecked Sendable {
    private let lock = NSLock()
    private var value = false

    /// Sets the flag and returns whether this call was the one that set it.
    func testAndSet() -> Bool {
        lock.lock()
        defer { lock.unlock() }
        if value { return false }
        value = true
        return true
    }
}
