import Foundation

/// Where everything lives, on disk, at runtime.
///
/// Two separate concerns, deliberately not mixed:
///
/// * **Payload** paths are inside the app bundle and read-only.
/// * **Support** paths are the user's data, in `~/Library/Application Support` and
///   `~/Library/Logs`, and are the only places this app writes.
///
/// Kept free of `Bundle.main` lookups at the type level so tests can point it at a
/// temporary directory.
public struct BundleLayout: Sendable {
    /// `FinanceApp.app/Contents/Resources`
    public let resourcesDirectory: URL
    /// `~/Library/Application Support/FinanceApp`
    public let supportDirectory: URL
    /// `~/Library/Logs/FinanceApp`
    public let logDirectory: URL

    public init(resourcesDirectory: URL, supportDirectory: URL, logDirectory: URL) {
        self.resourcesDirectory = resourcesDirectory
        self.supportDirectory = supportDirectory
        self.logDirectory = logDirectory
    }

    // MARK: - Payload (read-only, inside the bundle)

    public var backendDirectory: URL { resourcesDirectory.appending(path: "backend") }
    public var interpreterURL: URL {
        backendDirectory.appending(path: "python/bin/python3")
    }
    public var bootstrapURL: URL { backendDirectory.appending(path: "bootstrap.py") }
    public var bundledPluginsDirectory: URL { backendDirectory.appending(path: "app/plugins") }
    /// `vite build` output, served by FastAPI as static files.
    public var webDirectory: URL { resourcesDirectory.appending(path: "web") }

    // MARK: - Support (the user's data; the only paths we write)

    /// Mirrors the container layout so an existing `data/` directory can be pointed
    /// at directly rather than migrated.
    public var dataDirectory: URL { supportDirectory.appending(path: "data") }
    public var runtimeDirectory: URL { dataDirectory.appending(path: "runtime/prod") }
    public var databaseURL: URL { runtimeDirectory.appending(path: "finances.db") }
    /// Plaid and Google client secrets. Mode 0600; see `SecretsFile`.
    public var configURL: URL { supportDirectory.appending(path: "config.yaml") }
    public var backendLogURL: URL { logDirectory.appending(path: "backend.log") }
    public var shellLogURL: URL { logDirectory.appending(path: "shell.log") }
    /// Records the backend's process-group id so a leaked child can be swept on
    /// the next launch.
    public var runtimeStateURL: URL { supportDirectory.appending(path: "runtime-state.json") }
    /// flock target for the single-instance guard.
    public var instanceLockURL: URL { supportDirectory.appending(path: "instance.lock") }
    /// The port used last launch. Reused when free so the page's origin -- and therefore
    /// its localStorage -- survives a restart.
    public var portMemoryURL: URL { supportDirectory.appending(path: "port") }

    // MARK: - Construction

    /// The layout for a real launch, derived from the running bundle.
    public static func forRunningApplication(
        bundle: Bundle = .main,
        applicationName: String = "FinanceApp",
        fileManager: FileManager = .default
    ) -> BundleLayout {
        let resources = bundle.resourceURL
            // `swift run` has no bundle Resources dir; fall back to the executable's
            // directory so the shell is debuggable outside a bundle.
            ?? bundle.bundleURL.appending(path: "Contents/Resources")
        let support = fileManager
            .urls(for: .applicationSupportDirectory, in: .userDomainMask).first!
            .appending(path: applicationName)
        let logs = fileManager
            .homeDirectoryForCurrentUser
            .appending(path: "Library/Logs")
            .appending(path: applicationName)
        return BundleLayout(
            resourcesDirectory: resources,
            supportDirectory: support,
            logDirectory: logs
        )
    }

    /// Create the directories this app writes to. Payload directories are never created.
    public func createSupportDirectories(fileManager: FileManager = .default) throws {
        for directory in [supportDirectory, runtimeDirectory, logDirectory] {
            try fileManager.createDirectory(
                at: directory,
                withIntermediateDirectories: true,
                // 0700: this tree holds a financial database and provider secrets.
                attributes: [.posixPermissions: 0o700]
            )
        }
    }
}
