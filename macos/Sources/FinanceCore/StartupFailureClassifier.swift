import Foundation

/// Turns "the backend exited" into a specific reason, from the tail of its log.
///
/// The whole point is to refuse to over-classify. Only a message that can mean one
/// thing produces a definitive answer; anything else stays `failedBeforeReady`, which
/// is retryable. The Timeslice work paid three times for the opposite habit --
/// treating a generic failure as proof that a resource was gone (plan §11.3).
public struct StartupFailureClassifier: Sendable {
    public init() {}

    /// - Parameters:
    ///   - exitCode: negative for a signalled death.
    ///   - logTail: the last few KB of the backend log.
    ///   - wasReady: whether `/api/health` had answered before the exit.
    public func classify(exitCode: Int32, logTail: String, wasReady: Bool) -> BackendExit {
        // A port taken between allocation and bind. Unambiguous, and specifically
        // worth retrying because the next attempt allocates a different port.
        if logTail.contains("Address already in use")
            || logTail.contains("[Errno 48]")
            || logTail.contains("error while attempting to bind on address")
        {
            return .portCollision
        }

        // A broken payload. These are the shipped-bundle equivalent of a missing
        // file, and no amount of retrying produces the module.
        for marker in [
            "ModuleNotFoundError",
            "ImportError",
            "not valid for use in process",  // hardened-runtime library validation
            "bootstrap: ",                   // bootstrap.py's own SystemExit messages
        ] where logTail.contains(marker) {
            return .payloadInvalid(reason: firstLine(containing: marker, in: logTail) ?? marker)
        }

        return wasReady
            ? .crashedAfterReady(code: exitCode)
            : .failedBeforeReady(code: exitCode)
    }

    private func firstLine(containing marker: String, in text: String) -> String? {
        text.split(separator: "\n", omittingEmptySubsequences: true)
            .first { $0.contains(marker) }
            .map { String($0.prefix(300)) }
    }
}

/// Reads the last `maxBytes` of a file without loading the whole thing.
public func tail(of url: URL, maxBytes: Int = 16 * 1024) -> String {
    guard let handle = try? FileHandle(forReadingFrom: url) else { return "" }
    defer { try? handle.close() }
    guard let size = try? handle.seekToEnd() else { return "" }
    let offset = size > UInt64(maxBytes) ? size - UInt64(maxBytes) : 0
    try? handle.seek(toOffset: offset)
    guard let data = try? handle.readToEnd() else { return "" }
    return String(decoding: data, as: UTF8.self)
}
