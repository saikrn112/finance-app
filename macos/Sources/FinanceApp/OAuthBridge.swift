import AppKit
import FinanceCore
import Foundation

/// Carries a provider OAuth flow out to the system browser and back.
///
/// ## Why this has to exist
///
/// The frontend starts the Google Drive connection with
/// `window.open('/api/settings/vault/google/start')`, expecting a popup it can watch and a
/// `postMessage` from `window.opener`. Inside the shell neither holds:
///
/// * A same-origin *navigation* cannot carry the session token. The shell's user script
///   patches `fetch` and `XMLHttpRequest`, not the navigation loader — so loading that URL
///   in the webview produced a window containing the literal text
///   `{"detail":"unauthorized"}`, with the whole app gone. Observed, not theorised.
/// * Google's consent page must not render in the app's own origin. That origin holds the
///   session token for the entire financial API, and Google refuses embedded webviews for
///   OAuth anyway.
/// * A popup that opens in Safari has no `window.opener` back into the app, so the
///   frontend's "did the popup close?" poll never fires.
///
/// So the shell does the three steps the page cannot: resolve the provider URL *with* the
/// token, open it in the browser, and tell the page when the connection has landed.
@MainActor
final class OAuthBridge {
    /// Paths whose sole job is to redirect to a provider. Exact matches, so a future
    /// `/start-something-else` is not swept in by accident.
    static let startPaths: Set<String> = ["/api/settings/vault/google/start"]

    private let endpoint: BackendEndpoint
    private let log: ShellLog
    private var pollTask: Task<Void, Never>?

    /// Called when the vault reports itself connected, so the page can refresh.
    var onConnected: (() -> Void)?

    init(endpoint: BackendEndpoint, log: ShellLog) {
        self.endpoint = endpoint
        self.log = log
    }

    deinit {
        pollTask?.cancel()
    }

    static func isStart(_ url: URL?) -> Bool {
        guard let url else { return false }
        return startPaths.contains(url.path)
    }

    /// Resolve the provider's URL and hand it to the system browser.
    func begin(startURL: URL) {
        log.write("oauth: resolving the provider URL for \(startURL.path)")
        Task { [weak self] in
            guard let self else { return }
            guard let providerURL = await self.resolveRedirect(from: startURL) else {
                self.log.write("oauth: the backend did not return a provider redirect")
                self.presentFailure()
                return
            }
            self.log.write("oauth: opening \(ShellLog.safeOrigin(providerURL)) in the browser")
            NSWorkspace.shared.open(providerURL)
            self.startPollingForConnection()
        }
    }

    /// Follow exactly one redirect, with the token attached, and return its `Location`.
    ///
    /// Deliberately does not follow further: the next hop is the provider's, and fetching
    /// it here would both be pointless and send our request to a third party.
    private func resolveRedirect(from startURL: URL) async -> URL? {
        var request = URLRequest(url: startURL)
        request.setValue(endpoint.token.value, forHTTPHeaderField: "x-finance-token")
        request.httpMethod = "GET"

        let delegate = SingleRedirectBlocker()
        let session = URLSession(
            configuration: .ephemeral, delegate: delegate, delegateQueue: nil
        )
        defer { session.finishTasksAndInvalidate() }

        do {
            let (_, response) = try await session.data(for: request)
            guard let http = response as? HTTPURLResponse else { return nil }
            if let location = delegate.blockedLocation { return location }
            // Some backends return the URL in the body rather than as a redirect.
            if http.statusCode == 401 {
                log.write("oauth: the start endpoint refused our token")
            }
            return nil
        } catch {
            log.write("oauth: could not reach the start endpoint: \(error.localizedDescription)")
            return nil
        }
    }

    /// Poll `/api/settings` until the vault reports connected.
    ///
    /// Polling rather than waiting for the callback, because the callback is served by the
    /// *backend* — the shell never sees it. Bounded, so a user who abandons the flow in the
    /// browser does not leave a task running for the life of the app.
    private func startPollingForConnection() {
        pollTask?.cancel()
        pollTask = Task { [weak self] in
            guard let self else { return }
            let deadline = Date().addingTimeInterval(5 * 60)
            while !Task.isCancelled, Date() < deadline {
                try? await Task.sleep(for: .seconds(2))
                if await self.vaultIsConnected() {
                    self.log.write("oauth: the vault is connected; refreshing the app")
                    self.onConnected?()
                    return
                }
            }
            self.log.write("oauth: gave up waiting for the connection to land")
        }
    }

    private func vaultIsConnected() async -> Bool {
        var request = URLRequest(
            url: endpoint.baseURL.appending(path: "api/settings/")
        )
        request.setValue(endpoint.token.value, forHTTPHeaderField: "x-finance-token")
        do {
            let (data, _) = try await URLSession(configuration: .ephemeral).data(for: request)
            // Read only the one field we need. Decoding the whole settings payload here
            // would make the shell care about a schema it has no business knowing.
            guard
                let root = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                let vault = root["vault"] as? [String: Any],
                let connected = vault["connected"] as? Bool
            else { return false }
            return connected
        } catch {
            // A transient failure is not "not connected forever" — keep polling.
            return false
        }
    }

    private func presentFailure() {
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = "Could not start the Google connection"
        alert.informativeText =
            "The app could not reach its own backend to begin the sign-in flow. "
            + "Check the log at ~/Library/Logs/FinanceApp/ and try again."
        alert.addButton(withTitle: "OK")
        alert.runModal()
    }
}

/// Captures the first redirect instead of following it.
///
/// `@unchecked Sendable` with a lock rather than an actor: URLSession calls its delegate on
/// its own queue, and the mutable capture is a single URL written once.
private final class SingleRedirectBlocker: NSObject, URLSessionTaskDelegate, @unchecked Sendable {
    private let lock = NSLock()
    private var storedLocation: URL?

    var blockedLocation: URL? {
        lock.lock()
        defer { lock.unlock() }
        return storedLocation
    }

    func urlSession(
        _ session: URLSession,
        task: URLSessionTask,
        willPerformHTTPRedirection response: HTTPURLResponse,
        newRequest request: URLRequest,
        completionHandler: @escaping (URLRequest?) -> Void
    ) {
        lock.lock()
        storedLocation = request.url
        lock.unlock()
        // nil stops the redirect chain; the original task completes with the 3xx.
        completionHandler(nil)
    }
}
