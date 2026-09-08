import Foundation

/// Why a backend attempt ended.
///
/// The distinction matters because of the most expensive bug class in the Timeslice
/// work (plan §11.3): treating "the operation failed" as "the resource is
/// definitively gone". Only an unambiguous signal may stop us retrying. Everything
/// else keeps trying.
public enum BackendExit: Sendable, Equatable {
    /// The process died after having been healthy. Almost always worth restarting.
    case crashedAfterReady(code: Int32)
    /// The process exited before `/api/health` ever answered.
    case failedBeforeReady(code: Int32)
    /// The port we allocated was taken between allocation and bind. Retryable, and
    /// specifically *not* a reason to give up: the next attempt gets a new port.
    case portCollision
    /// The payload is wrong -- a missing interpreter, a missing bootstrap. Retrying
    /// cannot fix this and pretending otherwise hides the real problem behind a
    /// spinner.
    case payloadInvalid(reason: String)
    /// We asked it to stop.
    case stoppedIntentionally
}

/// Decides whether and when to restart the backend. A pure value type with no
/// clock and no I/O, so the loop can be covered by assertions rather than by
/// waiting (plan §11.8).
public struct RestartPolicy: Sendable {
    /// Attempts that count as "recent". Older failures are forgiven, so an app left
    /// running for a week doesn't refuse to restart because of a blip on Monday.
    public let failureWindow: TimeInterval
    /// How many failures inside the window before we stop and show the failure state.
    public let maxFailuresInWindow: Int
    public let baseDelay: TimeInterval
    public let maxDelay: TimeInterval

    public init(
        failureWindow: TimeInterval = 300,
        maxFailuresInWindow: Int = 5,
        baseDelay: TimeInterval = 0.5,
        maxDelay: TimeInterval = 30
    ) {
        self.failureWindow = failureWindow
        self.maxFailuresInWindow = maxFailuresInWindow
        self.baseDelay = baseDelay
        self.maxDelay = maxDelay
    }

    public enum Decision: Sendable, Equatable {
        case restart(after: TimeInterval)
        case giveUp(reason: String)
        case stayStopped
    }

    /// - Parameters:
    ///   - exit: how the attempt that just ended finished.
    ///   - recentFailures: timestamps of prior failed attempts, most recent last.
    ///   - now: the current time, injected so tests need no clock.
    public func decide(
        after exit: BackendExit,
        recentFailures: [Date],
        now: Date
    ) -> Decision {
        switch exit {
        case .stoppedIntentionally:
            return .stayStopped

        case .payloadInvalid(let reason):
            // The only case where retrying is definitively wrong. Named explicitly
            // rather than inferred from an exit code, so a merely-unlucky start
            // never lands here.
            return .giveUp(reason: reason)

        case .portCollision:
            // Not counted as a failure at all: it says nothing about the payload,
            // and the next attempt allocates a different port.
            return .restart(after: baseDelay)

        case .crashedAfterReady, .failedBeforeReady:
            let cutoff = now.addingTimeInterval(-failureWindow)
            let recent = recentFailures.filter { $0 > cutoff }
            // +1 for the failure being decided now.
            let count = recent.count + 1
            if count >= maxFailuresInWindow {
                return .giveUp(
                    reason: "the backend failed \(count) times in "
                        + "\(Int(failureWindow / 60)) minutes"
                )
            }
            // Exponential, from the count of *recent* failures only, so a
            // long-running app that hiccups twice a day restarts immediately both
            // times instead of inheriting yesterday's backoff.
            let delay = baseDelay * pow(2, Double(count - 1))
            return .restart(after: min(delay, maxDelay))
        }
    }

    /// Whether this exit should be added to the failure history at all.
    ///
    /// Kept next to `decide` and shared with the caller so the two cannot drift --
    /// the sweep-versus-timer split in Timeslice went wrong precisely because two
    /// paths each had their own idea of what counted (plan §11.2).
    public func countsAsFailure(_ exit: BackendExit) -> Bool {
        switch exit {
        case .crashedAfterReady, .failedBeforeReady: true
        case .portCollision, .payloadInvalid, .stoppedIntentionally: false
        }
    }
}
