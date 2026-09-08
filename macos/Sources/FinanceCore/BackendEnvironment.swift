import Foundation

/// Everything the child interpreter is told, built explicitly.
///
/// Nothing is inherited from the shell's own environment. The backend runs under
/// `-I`, and inheriting would reintroduce the machine-specific variables that
/// isolation exists to exclude -- a Homebrew `PYTHONPATH`, a stray
/// `FINANCE_APP_MODE` from a terminal session, a `DYLD_*` override.
public struct BackendEnvironment: Sendable {
    public var layout: BundleLayout
    public var mode: String
    /// The user-chosen private plugin directory. `nil` means "not configured", which
    /// the backend must surface as such rather than silently falling back to the
    /// bundled templates.
    public var privatePluginsDirectory: URL?
    public var displayCurrency: String?
    public var logLevel: String

    public init(
        layout: BundleLayout,
        mode: String = "live",
        privatePluginsDirectory: URL? = nil,
        displayCurrency: String? = nil,
        logLevel: String = "info"
    ) {
        self.layout = layout
        self.mode = mode
        self.privatePluginsDirectory = privatePluginsDirectory
        self.displayCurrency = displayCurrency
        self.logLevel = logLevel
    }

    public func variables(port: UInt16, token: SessionToken) -> [String: String] {
        var environment: [String: String] = [
            // A minimal PATH. The backend shells out to nothing, but a completely
            // empty PATH makes any future subprocess fail in a puzzling way.
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": FileManager.default.homeDirectoryForCurrentUser.path,
            "LANG": "en_US.UTF-8",
            // Unbuffered, so a crash traceback reaches the log file instead of dying
            // in a pipe buffer -- the difference between a diagnosable failure and
            // an empty log.
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",

            "FINANCE_APP_HOST": "127.0.0.1",
            "FINANCE_APP_PORT": String(port),
            "FINANCE_APP_LOCAL_TOKEN": token.value,
            "FINANCE_APP_MODE": mode,
            "FINANCE_APP_CONFIG": layout.configURL.path,
            "FINANCE_APP_DATA_DIR": layout.dataDirectory.path,
            "FINANCE_APP_RUNTIME_DIR": layout.runtimeDirectory.path,
            "FINANCE_APP_DB_PATH": layout.databaseURL.path,
            "FINANCE_APP_LOG_LEVEL": logLevel,
        ]
        // Only when the built frontend is actually present. Pointing the backend at a
        // missing directory would make it serve 404s for the app shell, which reads as
        // a broken app rather than a bundle built without `vite build`.
        if FileManager.default.fileExists(
            atPath: layout.webDirectory.appending(path: "index.html").path
        ) {
            environment["FINANCE_APP_WEB_DIR"] = layout.webDirectory.path
        }
        if let privatePluginsDirectory {
            environment["FINANCE_PLUGINS_DIR"] = privatePluginsDirectory.path
        }
        if let displayCurrency {
            environment["FINANCE_APP_DISPLAY_CURRENCY"] = displayCurrency
        }
        return environment
    }

    /// A copy safe to show in a diagnostics panel or write to the shell log.
    ///
    /// The token is replaced by its fingerprint. Paths are kept: the user needs to
    /// see where their database is, and it is their own machine. Nothing here may
    /// leave the machine.
    public func redactedVariables(port: UInt16, token: SessionToken) -> [String: String] {
        var redacted = variables(port: port, token: token)
        redacted["FINANCE_APP_LOCAL_TOKEN"] = "<\(token.fingerprint)>"
        return redacted
    }
}
