import Foundation
import Testing

@testable import FinanceCore

@Suite("StartupFailureClassifier")
struct StartupFailureClassifierTests {
    let classifier = StartupFailureClassifier()

    @Test("uvicorn's bind failure is recognised as a port collision")
    func portCollision() {
        let log = """
            INFO:     Started server process [4242]
            ERROR:    [Errno 48] error while attempting to bind on address \
            ('127.0.0.1', 51234): address already in use
            """
        #expect(classifier.classify(exitCode: 1, logTail: log, wasReady: false) == .portCollision)
    }

    @Test("a missing module is a broken payload, not a retryable failure")
    func missingModule() {
        let log = """
            Traceback (most recent call last):
              File "bootstrap.py", line 60, in main
            ModuleNotFoundError: No module named 'pdfplumber'
            """
        guard case .payloadInvalid(let reason) = classifier.classify(
            exitCode: 1, logTail: log, wasReady: false
        ) else {
            Issue.record("expected payloadInvalid")
            return
        }
        #expect(reason.contains("ModuleNotFoundError"))
    }

    @Test("library validation rejection is a broken payload")
    func libraryValidation() {
        // The exact failure phase 0 hit with ad-hoc signatures under the hardened
        // runtime. Retrying it forever would be the worst possible response, since
        // the app would look like it was merely slow.
        let log = "ImportError: dlopen(...): code signature ... "
            + "not valid for use in process: mapping process and mapped file "
            + "(non-platform) have different Team IDs"
        guard case .payloadInvalid = classifier.classify(
            exitCode: 1, logTail: log, wasReady: false
        ) else {
            Issue.record("expected payloadInvalid")
            return
        }
    }

    @Test("bootstrap's own errors are a broken payload")
    func bootstrapError() {
        guard case .payloadInvalid = classifier.classify(
            exitCode: 1, logTail: "bootstrap: FINANCE_APP_PORT is required", wasReady: false
        ) else {
            Issue.record("expected payloadInvalid")
            return
        }
    }

    @Test("an unrecognised failure stays retryable")
    func unrecognisedFailureIsRetryable() {
        // The whole point: refusing to over-classify. An empty log and an unfamiliar
        // traceback must not become "give up", because that is the bug class that cost
        // the most in the Timeslice work.
        #expect(
            classifier.classify(exitCode: 1, logTail: "", wasReady: false)
                == .failedBeforeReady(code: 1)
        )
        #expect(
            classifier.classify(exitCode: 70, logTail: "RuntimeError: something odd", wasReady: false)
                == .failedBeforeReady(code: 70)
        )
    }

    @Test("readiness selects between the two retryable kinds")
    func readinessDistinction() {
        #expect(
            classifier.classify(exitCode: -9, logTail: "", wasReady: true)
                == .crashedAfterReady(code: -9)
        )
        #expect(
            classifier.classify(exitCode: -9, logTail: "", wasReady: false)
                == .failedBeforeReady(code: -9)
        )
    }

    @Test("a port collision wins over a coincidental import message")
    func portCollisionTakesPrecedence() {
        // Ordering matters: a bind failure that also happens to mention ImportError
        // somewhere in the log tail is still a bind failure, and classifying it as a
        // broken payload would strand the user on a failure screen a retry would fix.
        let log = """
            WARNING:  ImportError while loading an optional plugin, continuing
            ERROR:    [Errno 48] address already in use
            """
        #expect(classifier.classify(exitCode: 1, logTail: log, wasReady: false) == .portCollision)
    }
}

@Suite("tail(of:)")
struct TailTests {
    @Test("reads only the last bytes of a large file")
    func readsTail() throws {
        let url = FileManager.default.temporaryDirectory
            .appending(path: "tail-\(UUID().uuidString).log")
        defer { try? FileManager.default.removeItem(at: url) }

        let filler = String(repeating: "x", count: 5000)
        try (filler + "\nLAST LINE\n").write(to: url, atomically: true, encoding: .utf8)

        let result = tail(of: url, maxBytes: 64)
        #expect(result.contains("LAST LINE"))
        #expect(result.count <= 64)
    }

    @Test("a missing file yields an empty string rather than throwing")
    func missingFile() {
        // The classifier calls this on a backend that may have died before creating
        // its log; a throw here would mask the real failure.
        #expect(tail(of: URL(filePath: "/nonexistent/finance/backend.log")).isEmpty)
    }
}
