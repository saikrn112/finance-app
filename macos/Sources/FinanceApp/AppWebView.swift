import AppKit
import FinanceCore
import WebKit

/// Hosts the existing React frontend.
///
/// ## How the session token reaches the API
///
/// A `WKUserScript` at `.atDocumentStart` wraps `fetch` and `XMLHttpRequest` so every
/// same-origin request carries `x-finance-token`.
///
/// The first attempt used a cookie in the webview's data store, and it did not work.
/// `HTTPCookie` construction succeeded, `setCookie`'s completion fired, and reading the
/// store back showed the cookie present -- but WKWebView never attached it to a request
/// to `http://127.0.0.1:<port>`. Neither `.domain` nor `.originURL` helped.
///
/// What made this worth an hour: the failure is invisible. The app renders, the layout is
/// right, and every panel shows `$0.00` -- which on an empty database is also what
/// *success* looks like. The onboarding gate never appeared either, because the settings
/// request 401ed too, so even the one obvious symptom was suppressed. Playwright was green
/// throughout, because Playwright has its own cookie jar and its own idea of
/// domain-matching. It took a screenshot from the user, then
/// `checkAuthenticatedFromInsideTheWebview()`, to see it at all.
///
/// The document request itself is not covered by a user script, which is fine: `/` and the
/// static assets are exempt from the gate by design, because nothing could present a token
/// before the app shell has loaded.
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
        // Non-persistent: the token is per-launch, and nothing else here is worth keeping
        // between launches. localStorage is the exception the frontend does use (theme,
        // "getting started" done), and losing it is a small cost against not persisting
        // anything derived from a financial API.
        configuration.websiteDataStore = .nonPersistent()
        configuration.suppressesIncrementalRendering = false
        configuration.userContentController.addUserScript(
            WKUserScript(
                source: Self.tokenInjectionScript(token: endpoint.token),
                injectionTime: .atDocumentStart,
                forMainFrameOnly: true
            )
        )

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
        navigationHandler.onLoadFinished = { [weak self] in
            self?.checkAuthenticatedFromInsideTheWebview()
        }
        webView.load(URLRequest(url: endpoint.baseURL))
    }

    /// Wraps `fetch` and `XMLHttpRequest` so same-origin requests carry the token.
    ///
    /// The token lives in a closure rather than on `window`, so page script cannot read it
    /// back out. That is not a real security boundary -- any script on this origin can make
    /// authenticated requests regardless -- but it does keep the value out of anything that
    /// serialises `window`, and out of a stray `console.log`.
    private static func tokenInjectionScript(token: SessionToken) -> String {
        // JSON-encoded so the value cannot break out of the string literal. The token is
        // URL-safe base64 today, but relying on that here would be a trap for later.
        let encoded = String(
            data: try! JSONEncoder().encode(token.value), encoding: .utf8
        )!
        return """
            (function () {
              'use strict';
              const TOKEN = \(encoded);
              const HEADER = 'x-finance-token';

              function isSameOrigin(url) {
                try {
                  return new URL(url, location.href).origin === location.origin;
                } catch (error) {
                  // A relative URL that URL() cannot parse is same-origin by construction.
                  return true;
                }
              }

              const originalFetch = window.fetch;
              window.fetch = function (input, init) {
                try {
                  const request = new Request(input, init);
                  if (isSameOrigin(request.url)) {
                    request.headers.set(HEADER, TOKEN);
                    return originalFetch.call(this, request);
                  }
                } catch (error) {
                  // Constructing a Request can throw for exotic bodies. Falling through
                  // unmodified is better than failing the call: an off-origin or
                  // unusual request losing the header is recoverable, a thrown fetch
                  // is not.
                }
                return originalFetch.call(this, input, init);
              };

              // XHR too. Nothing in the app uses it today, but a library might, and a
              // request that silently 401s is the exact failure this whole mechanism
              // exists to stop.
              const originalOpen = XMLHttpRequest.prototype.open;
              XMLHttpRequest.prototype.open = function (method, url) {
                this.__financeSameOrigin = isSameOrigin(url);
                return originalOpen.apply(this, arguments);
              };
              const originalSend = XMLHttpRequest.prototype.send;
              XMLHttpRequest.prototype.send = function () {
                if (this.__financeSameOrigin) {
                  try {
                    this.setRequestHeader(HEADER, TOKEN);
                  } catch (error) {
                    // Already sent, or a forbidden header name. Not worth failing over.
                  }
                }
                return originalSend.apply(this, arguments);
              };
            })();
            """
    }

    /// Ask the page itself whether its `fetch` calls are authenticated.
    ///
    /// This is the only way to answer the question that matters. Playwright can prove the
    /// frontend/backend contract, but it has its own cookie jar; whether *WKWebView's*
    /// store attaches our cookie to a same-origin `fetch` is a different question, and
    /// getting it wrong is close to invisible -- the app renders, every panel shows zero,
    /// and the onboarding gate never appears because the settings request failed too. That
    /// is exactly what it looked like the first time.
    private func checkAuthenticatedFromInsideTheWebview() {
        // callAsyncJavaScript, not evaluateJavaScript: the latter cannot await a promise
        // and hands back the Promise object itself, which arrives as "a result of an
        // unsupported type".
        let script = """
            try {
              const response = await fetch('/api/meta', { credentials: 'same-origin' });
              return response.status;
            } catch (error) {
              return -1;
            }
            """
        webView.callAsyncJavaScript(
            script, arguments: [:], in: nil, in: .page
        ) { [weak self] result in
            guard let self else { return }
            let status: Int
            switch result {
            case .success(let value):
                status = (value as? NSNumber)?.intValue ?? -1
            case .failure(let error):
                self.navigationHandler.log.write(
                    "auth self-check could not run: \(error.localizedDescription)"
                )
                return
            }
            switch status {
            case 200:
                self.navigationHandler.log.write("auth self-check: the webview's API calls are authenticated")
            case 401:
                // Loud, because the app looks *fine* in this state.
                self.navigationHandler.log.write(
                    "auth self-check FAILED: the session token is not reaching the API (401). "
                        + "Every panel will read zero and onboarding will not appear."
                )
            default:
                self.navigationHandler.log.write("auth self-check: unexpected status \(status)")
            }
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
    let log: ShellLog
    /// Called after each successful load, so the controller can run its self-check.
    var onLoadFinished: (() -> Void)?

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
        onLoadFinished?()
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
