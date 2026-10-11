import Foundation

public struct DeviceSyncStatus: Decodable, Sendable {
    public let enabled: Bool
    public let configured: Bool
    public let reason: String
    public let lastSuccess: String?
    public let lastError: String?
    public let peerCount: Int
    public let busy: Bool
    enum CodingKeys: String, CodingKey {
        case enabled, configured, reason, busy
        case lastSuccess = "last_success", lastError = "last_error", peerCount = "peer_count"
    }
}

/// The native controls use the same per-launch authenticated API as the page.
public struct DeviceSyncClient: Sendable {
    public let endpoint: BackendEndpoint
    public init(endpoint: BackendEndpoint) { self.endpoint = endpoint }

    public func request(path: String, method: String = "GET") -> URLRequest {
        var request = URLRequest(url: endpoint.baseURL.appending(path: "api/settings/device-sync/" + path))
        request.httpMethod = method
        request.timeoutInterval = 30
        request.setValue(endpoint.token.value, forHTTPHeaderField: "x-finance-token")
        return request
    }

    public func status() async throws -> DeviceSyncStatus {
        let data = try await send(request(path: "status"))
        return try JSONDecoder().decode(DeviceSyncStatus.self, from: data)
    }
    public func syncNow() async throws {
        let data = try await send(request(path: "run", method: "POST"))
        struct Result: Decodable { let ran: Bool; let completed: Bool }
        let result = try JSONDecoder().decode(Result.self, from: data)
        guard result.ran && result.completed else { throw Failure.incomplete }
    }
    private func send(_ request: URLRequest) async throws -> Data {
        let (data, response) = try await URLSession.shared.data(for: request)
        guard let http = response as? HTTPURLResponse else { throw Failure.unavailable }
        guard (200..<300).contains(http.statusCode) else {
            throw http.statusCode == 409 ? Failure.busyOrPaused : Failure.unavailable
        }
        return data
    }
    public enum Failure: LocalizedError {
        case unavailable, busyOrPaused, incomplete
        public var errorDescription: String? {
            switch self {
            case .unavailable: "Device sync could not complete. Check Google Drive in Settings."
            case .busyOrPaused: "Device sync is paused or another job is running. Try again shortly."
            case .incomplete: "Device sync did not complete. Open Device Sync to review its status."
            }
        }
    }
}
