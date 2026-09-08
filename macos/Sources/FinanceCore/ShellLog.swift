import Darwin
import Foundation
import os

/// The shell's own log, separate from the backend's.
///
/// Exists because the interesting shell events -- did the webview actually load, did a
/// restart happen, was a stale backend swept -- are otherwise invisible: `NSLog` goes to
/// the unified log, which needs `log stream` and a predicate to read, and the backend's
/// log only knows about the backend.
///
/// Deliberately narrow about content. Request paths and query strings never come through
/// here: a query string in this app can carry an account identifier or a Plaid item id
/// (plan §9). URLs are recorded as scheme + host + port only.
public final class ShellLog: @unchecked Sendable {
    private let url: URL
    private let logFile: LogFile
    private let queue = DispatchQueue(label: "dev.local.financeapp.shelllog")
    private let subsystem = Logger(subsystem: "dev.local.financeapp", category: "shell")

    public init(url: URL) {
        self.url = url
        // Smaller than the backend's: these are single lines, not tracebacks.
        self.logFile = LogFile(url: url, maxBytes: 512 * 1024, generations: 2)
    }

    public func write(_ message: String) {
        subsystem.info("\(message, privacy: .public)")
        queue.async { [self] in
            guard let descriptor = try? logFile.openForAppending() else { return }
            defer { close(descriptor) }
            let line = "\(Self.timestamp()) \(message)\n"
            // Darwin.write, not this type's own `write(_:)`, which shadows it here.
            _ = line.withCString { Darwin.write(descriptor, $0, strlen($0)) }
        }
    }

    /// A URL reduced to what is safe to record: no path, no query.
    public static func safeOrigin(_ url: URL?) -> String {
        guard let url else { return "<none>" }
        guard let scheme = url.scheme, let host = url.host else { return "<opaque>" }
        return url.port.map { "\(scheme)://\(host):\($0)" } ?? "\(scheme)://\(host)"
    }

    private static func timestamp() -> String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        return formatter.string(from: Date())
    }
}
