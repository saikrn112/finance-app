import AppKit
import FinanceCore
import WebKit

/// Hosts the existing React frontend.
///
/// The token is installed as a cookie in the data store *before* the first load rather
/// than via a `WKUserScript`: a user script at `.atDocumentStart` runs after the
/// document request has already gone out, so the initial `GET /` and any request the
/// bundle makes during parse would go unauthenticated.
@MainActor
final class AppWebViewController: NSViewController {
    private let endpoint: BackendEndpoint
    private var webView: WKWebView!
    private let navigationHandler: ExternalNavigationHandler

    /// Steps of the ⌘+/⌘− zoom ladder. `pageZoom` is controlled explicitly because
    /// WKWebView's own pinch magnification fights ag-grid's virtualisation.
    private static let zoomSteps: [CGFloat] = [0.75, 0.85, 1.0, 1.15, 1.3, 1.5]
    private static let zoomDefaultsKey = "webViewPageZoom"

    init(endpoint: BackendEndpoint, log: ShellLog) {
        self.endpoint = endpoint
        self.navigationHandler = ExternalNavigationHandler(allowedPort: endpoint.port, log: log)
        super.init(nibName: nil, bundle: nil)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("not supported") }

    override func loadView() {
        let configuration = WKWebViewConfiguration()
        // A non-persistent store would drop the cookie on every relaunch, but a
        // persistent one would outlive the token it holds. Non-persistent is correct:
        // the token is per-launch, and we set it explicitly below every time.
        configuration.websiteDataStore = .nonPersistent()
        configuration.suppressesIncrementalRendering = false

        webView = WKWebView(frame: .zero, configuration: configuration)
        webView.navigationDelegate = navigationHandler
        webView.uiDelegate = navigationHandler
        // Disable magnification: pinch-zoom plus ag-grid's row virtualisation and
        // WKWebView's rubber-banding fight each other, and the result is a grid that
        // scrolls the wrong thing. ⌘+/⌘− drive pageZoom instead.
        webView.allowsMagnification = false
        webView.allowsBackForwardNavigationGestures = false
        webView.pageZoom = Self.storedZoom()
        // Not a browser: no drag-out of the whole document.
        webView.setValue(false, forKey: "drawsBackground")
        view = webView
    }

    override func viewDidLoad() {
        super.viewDidLoad()
        installTokenCookie { [weak self] in
            guard let self else { return }
            self.webView.load(URLRequest(url: self.endpoint.baseURL))
        }
    }

    /// Set the session cookie, then continue. The completion is required: loading
    /// before the store has committed the cookie is a race that shows up as an
    /// intermittent 401 on the very first request.
    private func installTokenCookie(then continuation: @escaping () -> Void) {
        guard
            let cookie = HTTPCookie(properties: [
                .name: "finance_token",
                .value: endpoint.token.value,
                .domain: "127.0.0.1",
                .path: "/",
                .secure: false,
                // A session cookie: it must not outlive this launch's token.
                .discard: true,
            ])
        else {
            continuation()
            return
        }
        webView.configuration.websiteDataStore.httpCookieStore.setCookie(cookie) {
            continuation()
        }
    }

    // MARK: - Zoom

    private static func storedZoom() -> CGFloat {
        let stored = UserDefaults.standard.double(forKey: zoomDefaultsKey)
        return zoomSteps.contains(stored) ? stored : 1.0
    }

    func zoomIn() { step(by: 1) }
    func zoomOut() { step(by: -1) }
    func resetZoom() { apply(1.0) }

    private func step(by direction: Int) {
        let steps = Self.zoomSteps
        // Nearest rather than exact: a zoom restored from defaults, or set by a future
        // code path, need not be exactly one of the steps.
        let currentIndex = steps.indices.min {
            abs(steps[$0] - webView.pageZoom) < abs(steps[$1] - webView.pageZoom)
        } ?? steps.firstIndex(of: 1.0)!
        apply(steps[min(max(currentIndex + direction, 0), steps.count - 1)])
    }

    private func apply(_ zoom: CGFloat) {
        webView.pageZoom = zoom
        UserDefaults.standard.set(Double(zoom), forKey: Self.zoomDefaultsKey)
    }

    func reload() {
        webView.reloadFromOrigin()
    }
}

/// Keeps the webview on the local app and sends everything else to the browser.
///
/// A webview that will follow any link is a browser without an address bar, which is
/// both a worse browser and a security problem: the app's origin holds the session
/// cookie for the whole financial API. Provider OAuth (Plaid, Google) is phase 4 and
/// needs a custom scheme to come back; this is the general rule it will build on.
@MainActor
final class ExternalNavigationHandler: NSObject, WKNavigationDelegate, WKUIDelegate {
    private let allowedPort: UInt16
    private let log: ShellLog

    init(allowedPort: UInt16, log: ShellLog) {
        self.allowedPort = allowedPort
        self.log = log
    }

    private func isLocalApp(_ url: URL?) -> Bool {
        guard let url else { return false }
        if url.scheme == "about" || url.scheme == "blob" || url.scheme == "data" { return true }
        guard let host = url.host, host == "127.0.0.1" || host == "localhost" else { return false }
        return url.port == Int(allowedPort)
    }

    // The decisionHandler's exact type matters. A near-miss signature compiles, emits
    // only a warning ("nearly matches optional requirement"), and is never called --
    // which would leave off-origin navigation completely unenforced while looking
    // implemented. This bit once already; keep the @MainActor @Sendable attributes.
    func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationAction: WKNavigationAction,
        decisionHandler: @escaping @MainActor @Sendable (WKNavigationActionPolicy) -> Void
    ) {
        let url = navigationAction.request.url
        if isLocalApp(url) {
            decisionHandler(.allow)
            return
        }
        // Anything off-origin opens in the user's browser, where it has an address bar
        // and none of our cookies.
        log.write("navigation to \(ShellLog.safeOrigin(url)) sent to the system browser")
        if let url, url.scheme == "http" || url.scheme == "https" {
            NSWorkspace.shared.open(url)
        }
        decisionHandler(.cancel)
    }

    // MARK: - Load outcome
    //
    // Logged because it is otherwise unobservable: a webview that fails to load shows
    // WebKit's own error page, which says nothing about the backend, and there is no
    // reliable way to screenshot this window to check (plan §11.8).

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        log.write("webview loaded \(ShellLog.safeOrigin(webView.url))")
    }

    func webView(
        _ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error
    ) {
        log.write("webview navigation failed: \(error.localizedDescription)")
    }

    func webView(
        _ webView: WKWebView,
        didFailProvisionalNavigation navigation: WKNavigation!,
        withError error: Error
    ) {
        // The one that fires when the backend is not listening on the port we were told.
        log.write("webview could not reach the backend: \(error.localizedDescription)")
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        // A silent blank window otherwise. WebKit's content process can be killed under
        // memory pressure, and the app looks broken with nothing in any log.
        log.write("webview content process terminated; reloading")
        webView.reloadFromOrigin()
    }

    /// `target="_blank"` and `window.open` produce no webview here; the link goes to the
    /// browser instead. Returning nil without handling the URL would silently do
    /// nothing, which reads as a broken button.
    func webView(
        _ webView: WKWebView,
        createWebViewWith configuration: WKWebViewConfiguration,
        for navigationAction: WKNavigationAction,
        windowFeatures: WKWindowFeatures
    ) -> WKWebView? {
        if let url = navigationAction.request.url, !isLocalApp(url) {
            NSWorkspace.shared.open(url)
        } else if let url = navigationAction.request.url {
            webView.load(URLRequest(url: url))
        }
        return nil
    }
}
