import Foundation
import Testing
@testable import FinanceCore

@Suite("Native device sync API contract")
struct DeviceSyncClientTests {
    @Test func startupDoesNotCreateOrFallBackFromMissingChosenDatabase() throws {
        let folder = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: folder) }
        #expect(DataDirectory.existingDatabaseProblem(folder) == .databaseMissing)
        #expect(!FileManager.default.fileExists(atPath: folder.appending(path: "runtime").path))
    }
    @Test func requestUsesLaunchOriginAndToken() {
        let client = DeviceSyncClient(endpoint: BackendEndpoint(port: 61234, token: SessionToken(value: "synthetic-token")))
        let request = client.request(path: "run", method: "POST")
        #expect(request.url?.absoluteString == "http://127.0.0.1:61234/api/settings/device-sync/run")
        #expect(request.httpMethod == "POST")
        #expect(request.value(forHTTPHeaderField: "x-finance-token") == "synthetic-token")
        #expect(request.timeoutInterval == 30)
    }
    @Test func statusDistinguishesPausedAndConfiguredWithoutSecrets() throws {
        let data = Data(#"{"enabled":false,"configured":true,"reason":"Device sync is paused.","last_success":null,"last_error":null,"peer_count":2,"busy":false}"#.utf8)
        let status = try JSONDecoder().decode(DeviceSyncStatus.self, from: data)
        #expect(!status.enabled)
        #expect(status.configured)
        #expect(status.peerCount == 2)
        #expect(status.lastSuccess == nil)
    }
}
