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

    private let oauthBridge: OAuthBridge
    private var pendingAppearanceIsDark: Bool?

    init(endpoint: BackendEndpoint, log: ShellLog) {
        self.endpoint = endpoint
        self.navigationHandler = ExternalNavigationHandler(allowedPort: endpoint.port, log: log)
        self.oauthBridge = OAuthBridge(endpoint: endpoint, log: log)
        super.init(nibName: nil, bundle: nil)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("not supported") }

    /// Measured rather than hardcoded: a standard title bar and a large one differ, and
    /// this value positions the app's own header clear of the traffic lights.
    private var titlebarHeight: CGFloat {
        let contentRect = NSWindow.contentRect(
            forFrameRect: NSRect(x: 0, y: 0, width: 800, height: 600),
            styleMask: [.titled, .closable, .miniaturizable, .resizable]
        )
        return 600 - contentRect.height
    }

    override func loadView() {
        let configuration = WKWebViewConfiguration()
        // Persistent, deliberately. A non-persistent store was the first choice -- it kept
        // the per-launch session cookie from outliving its token -- but the token no longer
        // travels as a cookie, and wiping the store took localStorage with it. The visible
        // result: the twelve-step "getting started" tour reopened on *every* launch,
        // because the flag recording that it was finished never survived.
        configuration.websiteDataStore = .default()
        configuration.suppressesIncrementalRendering = false
        configuration.userContentController.addUserScript(
            WKUserScript(
                source: Self.tokenInjectionScript(
                    token: endpoint.token, titlebarHeight: titlebarHeight
                ),
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
        // The webview paints its own background. It briefly did not, so an
        // NSVisualEffectView behind it could show through -- see the note on
        // `window.isOpaque` in AppDelegate for why that was removed.
        view = webView
    }

    override func viewDidLoad() {
        super.viewDidLoad()
        navigationHandler.onLoadFinished = { [weak self] in
            guard let self else { return }
            self.checkAuthenticatedFromInsideTheWebview()
            // Re-apply after every load: a reload starts the frontend from its own stored
            // preference, which is not necessarily the system's.
            if let dark = self.pendingAppearanceIsDark {
                self.dispatch(command: "set:appearance", argument: dark ? "dark" : "light")
            }
        }
        navigationHandler.onOAuthStart = { [weak self] url in
            self?.oauthBridge.begin(startURL: url)
        }
        oauthBridge.onConnected = { [weak self] in
            // Two commands, tried in order, because the right thing to do depends on what
            // is on screen. The onboarding gate owns a whole post-connect flow (discover
            // backups, offer a restore) that must not be skipped; if the gate is not open,
            // refetching settings is all that is needed. Each name has exactly one owner,
            // so neither can silently overwrite the other in the bus.
            self?.dispatchFirstHandled(commands: [
                "onboarding:provider-returned",
                "refresh:settings",
            ])
        }
        webView.load(URLRequest(url: endpoint.baseURL))
    }

    /// Walk a list of bus commands, one every `interval` seconds, logging each.
    ///
    /// A debug affordance, driven by FINANCE_APP_DISPATCH. It exists because the visual
    /// verification gap it closes was expensive: the page was being checked with Playwright,
    /// which renders on a plain background and cannot show the window's material, its opacity
    /// or its real surfaces — so a wash that made the whole app look muddy was invisible to
    /// the check. This lets a script step the *real* window through every view and screenshot
    /// each one.
    ///
    /// Off unless the variable is set, and it only dispatches names the frontend already
    /// registers, so it can reach nothing a menu item could not.
    func runDispatchTour(commands: [String], interval: TimeInterval) {
        guard !commands.isEmpty else { return }
        Task { @MainActor [weak self] in
            for command in commands {
                try? await Task.sleep(for: .seconds(interval))
                guard let self else { return }
                self.dispatch(command: command)
            }
            self?.navigationHandler.log.write("dispatch tour finished")
        }
    }

    /// Wraps `fetch` and `XMLHttpRequest` so same-origin requests carry the token.
    ///
    /// The token lives in a closure rather than on `window`, so page script cannot read it
    /// back out. That is not a real security boundary -- any script on this origin can make
    /// authenticated requests regardless -- but it does keep the value out of anything that
    /// serialises `window`, and out of a stray `console.log`.
    private static func tokenInjectionScript(
        token: SessionToken, titlebarHeight: CGFloat
    ) -> String {
        let titlebarHeightLiteral = String(format: "%.0f", titlebarHeight)
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

              // Marks the page as running inside the macOS shell. macos.css is scoped
              // entirely to this class, so a browser is unaffected. Set at document start,
              // before first paint, so there is no flash of the web styling.
              document.documentElement.classList.add('platform-macos');
              // The title bar's real height, rather than a magic number that is wrong on
              // whichever machine nobody tested.
              document.documentElement.style.setProperty(
                '--titlebar-height', \(titlebarHeightLiteral) + 'px'
              );

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

    /// Tell the page which appearance to use. Also re-sent after every load, because a
    /// reload starts the frontend from its own stored preference again.
    func setAppearance(dark: Bool) {
        pendingAppearanceIsDark = dark
        dispatch(command: "set:appearance", argument: dark ? "dark" : "light")
    }

    // MARK: - Commands

    /// Dispatch a named command to the frontend's command bus.
    ///
    /// The bus reports whether a handler existed, and an unhandled command is logged
    /// rather than swallowed: a menu item that silently does nothing is the most annoying
    /// possible failure, and the shell's menu table and the frontend's registrations are
    /// two lists that can drift.
    func dispatch(command: String, argument: String? = nil) {
        let script = """
            const bus = window.__financeCommandBus;
            if (!bus) return 'no-bus';
            return (await bus.dispatch(name, argument)) ? 'ok' : 'unhandled';
            """
        webView.callAsyncJavaScript(
            script,
            arguments: ["name": command, "argument": argument ?? NSNull()],
            in: nil, in: .page
        ) { [weak self] result in
            guard let self else { return }
            switch result {
            case .success(let value):
                switch value as? String {
                case "ok":
                    // Logged on success too, not only on failure: the screenshot script reads
                    // these lines to know when a navigation has actually landed, rather than
                    // sleeping a guessed interval and hoping.
                    self.navigationHandler.log.write("dispatched '\(command)'")
                case "unhandled":
                    self.navigationHandler.log.write(
                        "command '\(command)' is in the menu but not registered by the frontend"
                    )
                default:
                    // Expected briefly during a reload, before the bundle has run.
                    self.navigationHandler.log.write(
                        "command '\(command)' arrived before the command bus was installed"
                    )
                }
            case .failure(let error):
                self.navigationHandler.log.write(
                    "command '\(command)' failed: \(error.localizedDescription)"
                )
            }
        }
    }

    /// Dispatch the first command in `commands` that the frontend actually handles.
    ///
    /// Used where the appropriate action depends on what the page is currently showing.
    /// Falling through in order beats asking the page "what state are you in?" and then
    /// deciding here, which would put a copy of the frontend's state machine in Swift.
    func dispatchFirstHandled(commands: [String]) {
        guard let first = commands.first else { return }
        let script = """
            const bus = window.__financeCommandBus;
            if (!bus) return false;
            return await bus.dispatch(name);
            """
        webView.callAsyncJavaScript(
            script, arguments: ["name": first], in: nil, in: .page
        ) { [weak self] result in
            guard let self else { return }
            let handled = ((try? result.get()) as? NSNumber)?.boolValue ?? false
            if handled {
                self.navigationHandler.log.write("dispatched '\(first)'")
            } else {
                self.dispatchFirstHandled(commands: Array(commands.dropFirst()))
            }
        }
    }

    /// The command names the frontend has actually registered. For diagnostics and tests.
    func registeredCommands(completion: @escaping ([String]) -> Void) {
        webView.callAsyncJavaScript(
            "return window.__financeCommandBus ? window.__financeCommandBus.list() : [];",
            arguments: [:], in: nil, in: .page
        ) { result in
            completion((try? result.get()) as? [String] ?? [])
        }
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
    /// Called when the page tries to reach a provider OAuth start endpoint.
    var onOAuthStart: ((URL) -> Void)?

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

        // An OAuth start endpoint is a redirect to a provider. It cannot be loaded here:
        // a navigation carries no session token, so it 401s, and the provider's consent
        // page must not run in this origin regardless.
        if isLocalApp(url), let url, OAuthBridge.isStart(url) {
            onOAuthStart?(url)
            decisionHandler(.cancel)
            return
        }

        // Nothing else under /api may become the window either. The app is a single page
        // that talks to the API with fetch; a top-level navigation to an API URL is always
        // a mistake, and the failure mode is severe -- the entire app is replaced by a JSON
        // body, with no way back but Reload. That is exactly what happened with the Google
        // popup before this check existed.
        if isLocalApp(url), let url, url.path.hasPrefix("/api/") {
            log.write("blocked a top-level navigation to an API path")
            decisionHandler(.cancel)
            return
        }

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
        guard let url = navigationAction.request.url else { return nil }

        // `window.open` on an OAuth start endpoint: the whole reason this method needed
        // rewriting. It used to load the popup's URL into the *main* webview, which
        // replaced the running app with `{"detail":"unauthorized"}`.
        if OAuthBridge.isStart(url) {
            onOAuthStart?(url)
            return nil
        }
        if !isLocalApp(url) {
            NSWorkspace.shared.open(url)
            return nil
        }
        // A same-origin popup we do not recognise. Loading it into the main webview would
        // destroy the app, so log it and do nothing: a missing popup is a visible,
        // recoverable annoyance; a replaced app is not.
        log.write("ignored a same-origin popup the shell does not handle")
        return nil
    }
}
