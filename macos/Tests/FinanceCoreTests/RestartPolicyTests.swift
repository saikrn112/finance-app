import Foundation
import Testing

@testable import FinanceCore

/// The restart loop is the piece most worth covering with assertions rather than by
/// waiting: `RestartPolicy` has no clock and no I/O precisely so that this file can
/// exist (plan §11.8).
@Suite("RestartPolicy")
struct RestartPolicyTests {
    let now = Date(timeIntervalSince1970: 1_800_000_000)
    let policy = RestartPolicy(
        failureWindow: 300, maxFailuresInWindow: 5, baseDelay: 0.5, maxDelay: 30
    )

    @Test("an intentional stop is not a failure")
    func intentionalStop() {
        #expect(policy.decide(after: .stoppedIntentionally, recentFailures: [], now: now) == .stayStopped)
        #expect(policy.countsAsFailure(.stoppedIntentionally) == false)
    }

    @Test("a broken payload is the only reason to give up immediately")
    func payloadInvalid() {
        let decision = policy.decide(
            after: .payloadInvalid(reason: "ModuleNotFoundError: no module named 'x'"),
            recentFailures: [], now: now
        )
        #expect(decision == .giveUp(reason: "ModuleNotFoundError: no module named 'x'"))
        // It must not consume the failure budget: it already ended the loop, and
        // counting it would also poison a later explicit retry.
        #expect(policy.countsAsFailure(.payloadInvalid(reason: "x")) == false)
    }

    @Test("a port collision retries immediately and never counts as a failure")
    func portCollision() {
        // Losing the port race says nothing about the payload, and the next attempt
        // allocates a different port -- so even a hundred prior collisions must not
        // exhaust the budget.
        let manyPrior = (0..<99).map { now.addingTimeInterval(-Double($0)) }
        #expect(
            policy.decide(after: .portCollision, recentFailures: manyPrior, now: now)
                == .restart(after: 0.5)
        )
        #expect(policy.countsAsFailure(.portCollision) == false)
    }

    @Test("backoff doubles with each recent failure and is capped")
    func backoffGrowth() {
        func delay(priorFailures: Int) -> TimeInterval? {
            let history = (0..<priorFailures).map { now.addingTimeInterval(-Double($0 + 1)) }
            guard case .restart(let after) = policy.decide(
                after: .crashedAfterReady(code: 1), recentFailures: history, now: now
            ) else { return nil }
            return after
        }
        #expect(delay(priorFailures: 0) == 0.5)
        #expect(delay(priorFailures: 1) == 1.0)
        #expect(delay(priorFailures: 2) == 2.0)
        #expect(delay(priorFailures: 3) == 4.0)
    }

    @Test("backoff is capped at maxDelay")
    func backoffCap() {
        let generous = RestartPolicy(
            failureWindow: 300, maxFailuresInWindow: 100, baseDelay: 0.5, maxDelay: 30
        )
        let history = (0..<20).map { now.addingTimeInterval(-Double($0 + 1)) }
        #expect(
            generous.decide(after: .crashedAfterReady(code: 1), recentFailures: history, now: now)
                == .restart(after: 30)
        )
    }

    @Test("the budget is spent after maxFailuresInWindow failures")
    func budgetExhausted() {
        let history = (0..<4).map { now.addingTimeInterval(-Double($0 + 1)) }
        guard case .giveUp(let reason) = policy.decide(
            after: .failedBeforeReady(code: 1), recentFailures: history, now: now
        ) else {
            Issue.record("expected giveUp with 4 prior failures plus this one")
            return
        }
        #expect(reason.contains("5 times"))
    }

    @Test("failures older than the window are forgiven")
    func windowExpiry() {
        // An app left running for a week must not refuse to restart because of a
        // blip on Monday.
        let stale = (0..<10).map { now.addingTimeInterval(-301 - Double($0)) }
        #expect(
            policy.decide(after: .crashedAfterReady(code: 1), recentFailures: stale, now: now)
                == .restart(after: 0.5)
        )
    }

    @Test("a failure exactly at the window boundary is excluded")
    func windowBoundaryIsExclusive() {
        // Guards the comparison itself: `>` vs `>=` on the cutoff changes whether a
        // failure at precisely -300s counts, and an off-by-one here makes the budget
        // silently one larger or smaller than configured.
        let atBoundary = Array(repeating: now.addingTimeInterval(-300), count: 4)
        #expect(
            policy.decide(after: .crashedAfterReady(code: 1), recentFailures: atBoundary, now: now)
                == .restart(after: 0.5)
        )
        let justInside = Array(repeating: now.addingTimeInterval(-299), count: 4)
        guard case .giveUp = policy.decide(
            after: .crashedAfterReady(code: 1), recentFailures: justInside, now: now
        ) else {
            Issue.record("4 failures inside the window plus this one should exhaust the budget")
            return
        }
    }

    @Test("a crash after being ready is treated the same as one before")
    func crashAfterReadyCountsToo() {
        // Both are "it died and we do not know why", so both consume budget. The
        // distinction exists for the message, not for the decision.
        #expect(policy.countsAsFailure(.crashedAfterReady(code: -9)))
        #expect(policy.countsAsFailure(.failedBeforeReady(code: 1)))
    }
}
