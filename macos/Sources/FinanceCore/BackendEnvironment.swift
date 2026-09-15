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
    /// Replaces monetary values in API responses with fake ones.
    ///
    /// For screenshots and screen sharing. Set at the *backend*, not just in the UI, so real
    /// figures never leave the process — the frontend's own privacy toggle only hides what it
    /// has already received.
    public var privacyMask: Bool
    /// The shared folder to exchange sync payloads through, or nil for no multi-device sync.
    ///
    /// A folder rather than Google Drive keeps the data on this machine, and the container app can
    /// see the same directory through its `./data` bind mount. Absent means sync stays off, which is
    /// the default -- it publishes real financial history and should start deliberately.
    public var syncFolder: URL?

    public init(
        layout: BundleLayout,
        mode: String = "live",
        privatePluginsDirectory: URL? = nil,
        displayCurrency: String? = nil,
        logLevel: String = "info",
        privacyMask: Bool = false,
        syncFolder: URL? = nil
    ) {
        self.layout = layout
        self.mode = mode
        self.privatePluginsDirectory = privatePluginsDirectory
        self.displayCurrency = displayCurrency
        self.logLevel = logLevel
        self.privacyMask = privacyMask
        self.syncFolder = syncFolder
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

            // The Google OAuth redirect has to point at *this* launch's port.
            //
            // A config.yaml written for the container app pins this to
            // http://localhost:8000/..., so Google would send the browser to port 8000 --
            // either nothing, or the container app, but never this process. The symptom is
            // a sign-in that appears to succeed and a vault that never connects.
            //
            // Google allows any loopback port for a "desktop" OAuth client, which is what
            // google_drive.oauth_client_type defaults to, so a per-launch port is fine.
            "FINANCE_APP_GOOGLE_REDIRECT_URI":
                "http://localhost:\(port)/api/settings/vault/google/callback",
            // Splitwise needs the same treatment for the same reason. Added when Splitwise
            // landed; without it the browser would return to whatever port config.yaml names,
            // and the sign-in would appear to succeed while nothing connected.
            "FINANCE_APP_SPLITWISE_REDIRECT_URI":
                "http://localhost:\(port)/api/splitwise/callback",
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
        if privacyMask {
            environment["FINANCE_APP_PRIVACY_MASK"] = "1"
        }
        if let syncFolder {
            environment["FINANCE_APP_SYNC"] = "1"
            environment["FINANCE_APP_SYNC_FOLDER"] = syncFolder.path
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
