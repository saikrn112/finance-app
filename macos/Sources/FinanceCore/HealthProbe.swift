import Foundation

/// Polls `GET /api/health` until the backend answers.
///
/// `/api/health` is exempt from the loopback token gate, so the probe deliberately
/// sends no token: if it did, a token mismatch would look identical to a backend
/// that never started.
public struct HealthProbe: Sendable {
    public let host: String
    public let port: UInt16
    public let timeout: TimeInterval
    public let pollInterval: TimeInterval

    public init(
        host: String = "127.0.0.1",
        port: UInt16,
        timeout: TimeInterval = 30,
        pollInterval: TimeInterval = 0.15
    ) {
        self.host = host
        self.port = port
        self.timeout = timeout
        self.pollInterval = pollInterval
    }

    public var healthURL: URL {
        URL(string: "http://\(host):\(port)/api/health")!
    }

    public enum Outcome: Sendable, Equatable {
        case ready
        /// The child exited while we were waiting -- there is nothing left to poll.
        case processExited
        /// `timeout` elapsed with the process still alive but not answering. This is
        /// *not* proof the payload is broken, so the caller treats it as retryable.
        case timedOut
    }

    /// - Parameter isProcessAlive: checked between polls so a crash is noticed
    ///   immediately rather than after the full timeout.
    public func waitUntilReady(isProcessAlive: @Sendable () -> Bool) async -> Outcome {
        let deadline = Date().addingTimeInterval(timeout)
        let session = URLSession(configuration: {
            let configuration = URLSessionConfiguration.ephemeral
            configuration.timeoutIntervalForRequest = 2
            // A proxy on a loopback request would be absurd, and a system proxy
            // that mangles it is a real failure mode.
            configuration.connectionProxyDictionary = [:]
            return configuration
        }())

        while Date() < deadline {
            guard isProcessAlive() else { return .processExited }
            if await probeOnce(session: session) { return .ready }
            try? await Task.sleep(for: .seconds(pollInterval))
        }
        return isProcessAlive() ? .timedOut : .processExited
    }

    private func probeOnce(session: URLSession) async -> Bool {
        var request = URLRequest(url: healthURL)
        request.httpMethod = "GET"
        request.cachePolicy = .reloadIgnoringLocalAndRemoteCacheData
        do {
            let (_, response) = try await session.data(for: request)
            return (response as? HTTPURLResponse)?.statusCode == 200
        } catch {
            return false
        }
    }
}
