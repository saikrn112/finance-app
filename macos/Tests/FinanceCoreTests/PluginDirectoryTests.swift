import Foundation
import Testing

@testable import FinanceCore

/// The plugin directory's validation, which exists to turn three late, confusing symptoms into one
/// early message: a statement that will not parse, an opaque 500 from the uncategorised review, and
/// silently loading only the bundled templates.
@Suite("PluginDirectory", .serialized)
struct PluginDirectoryTests {
    /// A defaults domain per test, so nothing leaks into the real app's preferences.
    private func isolatedDefaults() -> UserDefaults {
        let suite = "PluginDirectoryTests-\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defaults.removePersistentDomain(forName: suite)
        return defaults
    }

    /// A directory shaped like the real plugins repository: flat `.py` modules plus `rules/`.
    private func makeRepository(withRules: Bool = true, module: String = "statement_bank.py") -> URL {
        let url = FileManager.default.temporaryDirectory
            .appending(path: "plugins-\(UUID().uuidString)")
        try? FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        try? "def register():\n    pass\n".write(
            to: url.appending(path: module), atomically: true, encoding: .utf8
        )
        if withRules {
            try? FileManager.default.createDirectory(
                at: url.appending(path: "rules"), withIntermediateDirectories: true
            )
        }
        return url
    }

    // MARK: - Validation

    @Test("a real plugins repository validates")
    func acceptsRepository() {
        let url = makeRepository()
        defer { try? FileManager.default.removeItem(at: url) }
        #expect(PluginDirectory.diagnose(url) == nil)
    }

    @Test("a missing folder is reported as missing")
    func rejectsMissing() {
        let url = FileManager.default.temporaryDirectory.appending(path: "gone-\(UUID().uuidString)")
        #expect(PluginDirectory.diagnose(url) == .missing)
    }

    @Test("a file is not a folder")
    func rejectsFile() throws {
        let url = FileManager.default.temporaryDirectory.appending(path: "f-\(UUID().uuidString).py")
        try "x".write(to: url, atomically: true, encoding: .utf8)
        defer { try? FileManager.default.removeItem(at: url) }
        #expect(PluginDirectory.diagnose(url) == .notADirectory)
    }

    @Test("a folder with no plugin modules is refused")
    func rejectsFolderWithoutModules() {
        // The likeliest wrong answer in the folder picker is the *parent* of the repository, which
        // exists, is a directory, and is writable -- so without this check it would be accepted and
        // then quietly parse nothing.
        let url = FileManager.default.temporaryDirectory.appending(path: "empty-\(UUID().uuidString)")
        try? FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: url) }
        #expect(PluginDirectory.diagnose(url) == .noPluginModules)
    }

    @Test("private helper modules do not count as plugins")
    func underscoreModulesDoNotCount() {
        // The real repository has `_payslip_utils.py`; a folder containing only helpers is not a
        // plugin folder.
        let url = makeRepository(module: "_payslip_utils.py")
        defer { try? FileManager.default.removeItem(at: url) }
        #expect(PluginDirectory.diagnose(url) == .noPluginModules)
    }

    @Test("a missing rules folder is created rather than rejected")
    func createsRulesDirectory() throws {
        // Learned rules are appended to rules/categories.yaml, and the backend does not create the
        // directory itself -- a missing one surfaces as an opaque 500 from the Apply button.
        let url = makeRepository(withRules: false)
        defer { try? FileManager.default.removeItem(at: url) }

        #expect(PluginDirectory.diagnose(url) == nil)
        #expect(FileManager.default.fileExists(atPath: url.appending(path: "rules").path))
    }

    @Test("a read-only folder is refused before it can fail obscurely")
    func rejectsReadOnly() throws {
        let url = makeRepository()
        defer {
            try? FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: url.path)
            try? FileManager.default.removeItem(at: url)
        }
        try FileManager.default.setAttributes([.posixPermissions: 0o555], ofItemAtPath: url.path)
        #expect(PluginDirectory.diagnose(url) == .notWritable)
    }

    // MARK: - Status and persistence

    @Test("nothing remembered reads as not configured")
    func statusWithoutSelection() {
        #expect(PluginDirectory.status(defaults: isolatedDefaults()) == .notConfigured)
    }

    @Test("a remembered folder round-trips and reads as ready")
    func statusAfterSelection() {
        let defaults = isolatedDefaults()
        let url = makeRepository()
        defer { try? FileManager.default.removeItem(at: url) }

        PluginDirectory.remember(url, defaults: defaults)
        #expect(PluginDirectory.remembered(defaults: defaults)?.path == url.path)
        #expect(PluginDirectory.status(defaults: defaults) == .ready(url))
        #expect(PluginDirectory.status(defaults: defaults).usableDirectory?.path == url.path)
    }

    @Test("a folder that disappears after being chosen reports the problem, not readiness")
    func statusIsRederivedFromDisk() {
        // The folder is a git working copy the user edits outside this app: it can be moved,
        // renamed or made read-only between launches, so a cached "ready" would be a lie.
        let defaults = isolatedDefaults()
        let url = makeRepository()
        PluginDirectory.remember(url, defaults: defaults)
        #expect(PluginDirectory.status(defaults: defaults) == .ready(url))

        try? FileManager.default.removeItem(at: url)
        #expect(PluginDirectory.status(defaults: defaults) == .unusable(url, .missing))
        // And nothing unusable is ever handed to the backend, which would make it fall back to
        // the templates while the UI claimed a folder was in use.
        #expect(PluginDirectory.status(defaults: defaults).usableDirectory == nil)
    }

    @Test("forgetting the folder returns to not configured")
    func forget() {
        let defaults = isolatedDefaults()
        let url = makeRepository()
        defer { try? FileManager.default.removeItem(at: url) }
        PluginDirectory.remember(url, defaults: defaults)
        PluginDirectory.remember(nil, defaults: defaults)
        #expect(PluginDirectory.status(defaults: defaults) == .notConfigured)
    }

    @Test("every problem has a message that names the cause")
    func problemsExplainThemselves() {
        // The whole value of this type is the message; an empty one would make the row useless.
        for problem in [
            PluginDirectory.Problem.missing,
            .notADirectory,
            .noPluginModules,
            .rulesDirectoryUnavailable,
            .notWritable,
        ] {
            #expect(!problem.explanation.isEmpty)
        }
    }
}

/// The data directory, whose whole reason for existing is that two copies drifted apart.
@Suite("DataDirectory", .serialized)
struct DataDirectoryTests {
    private func isolatedDefaults() -> UserDefaults {
        let suite = "DataDirectoryTests-\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defaults.removePersistentDomain(forName: suite)
        return defaults
    }

    /// A directory shaped like the app's `data/`.
    private func makeDataDir(withDatabase bytes: Int? = nil, withRuntime: Bool = true) -> URL {
        let url = FileManager.default.temporaryDirectory.appending(path: "data-\(UUID().uuidString)")
        let runtime = url.appending(path: "runtime/prod")
        try? FileManager.default.createDirectory(
            at: withRuntime ? runtime : url, withIntermediateDirectories: true
        )
        if let bytes {
            try? Data(repeating: 0, count: bytes)
                .write(to: runtime.appending(path: "finances.db"))
        }
        return url
    }

    @Test("a real data folder validates")
    func acceptsDataDir() {
        let url = makeDataDir()
        defer { try? FileManager.default.removeItem(at: url) }
        #expect(DataDirectory.diagnose(url) == nil)
    }

    @Test("a missing folder is reported")
    func rejectsMissing() {
        let url = FileManager.default.temporaryDirectory.appending(path: "gone-\(UUID().uuidString)")
        #expect(DataDirectory.diagnose(url) == .missing)
    }

    @Test("runtime/prod is created when absent")
    func createsRuntime() {
        let url = makeDataDir(withRuntime: false)
        defer { try? FileManager.default.removeItem(at: url) }
        #expect(DataDirectory.diagnose(url) == nil)
        #expect(FileManager.default.fileExists(atPath: url.appending(path: "runtime/prod").path))
    }

    @Test("a read-only folder is refused")
    func rejectsReadOnly() throws {
        let url = makeDataDir()
        defer {
            try? FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: url.path)
            try? FileManager.default.removeItem(at: url)
        }
        try FileManager.default.setAttributes([.posixPermissions: 0o555], ofItemAtPath: url.path)
        #expect(DataDirectory.diagnose(url) == .notWritable)
    }

    @Test("an existing database is reported with its size, and an absent one as empty")
    func contentsDistinguishEmptyFromPopulated() {
        // Pointing at a folder with no database starts an empty one, which looks exactly like
        // losing everything — so the confirmation has to be able to say which case it is.
        let populated = makeDataDir(withDatabase: 2 * 1_048_576)
        let empty = makeDataDir()
        defer {
            try? FileManager.default.removeItem(at: populated)
            try? FileManager.default.removeItem(at: empty)
        }

        let full = DataDirectory.contents(of: populated)
        #expect(full.databaseExists)
        #expect(full.summary.contains("2.0 MB"))

        let blank = DataDirectory.contents(of: empty)
        #expect(!blank.databaseExists)
        #expect(blank.summary.contains("empty"))
    }

    @Test("nothing chosen resolves to the app's own folder")
    func resolvesToDefault() {
        let fallback = URL(filePath: "/tmp/app-owned-data")
        let resolved = DataDirectory.resolved(default: fallback, defaults: isolatedDefaults())
        #expect(resolved.url == fallback)
        #expect(resolved.problem == nil)
    }

    @Test("a chosen folder wins")
    func resolvesToChoice() {
        let defaults = isolatedDefaults()
        let url = makeDataDir()
        defer { try? FileManager.default.removeItem(at: url) }
        DataDirectory.remember(url, defaults: defaults)
        let resolved = DataDirectory.resolved(default: URL(filePath: "/tmp/x"), defaults: defaults)
        #expect(resolved.url == url)
        #expect(resolved.problem == nil)
    }

    @Test("a chosen folder that has moved falls back rather than failing to launch")
    func fallsBackWhenChoiceDisappears() {
        // An external volume that is not mounted, or a repository that was moved. Refusing to start
        // would be a worse answer than opening the app's own database and saying so.
        let defaults = isolatedDefaults()
        let url = makeDataDir()
        DataDirectory.remember(url, defaults: defaults)
        try? FileManager.default.removeItem(at: url)

        let fallback = URL(filePath: "/tmp/app-owned-data")
        let resolved = DataDirectory.resolved(default: fallback, defaults: defaults)
        #expect(resolved.url == fallback)
        #expect(resolved.problem == .missing)
    }
}

@Suite("BundleLayout: data directory override")
struct BundleLayoutOverrideTests {
    private let base = BundleLayout(
        resourcesDirectory: URL(filePath: "/tmp/FinanceApp.app/Contents/Resources"),
        supportDirectory: URL(filePath: "/tmp/support"),
        logDirectory: URL(filePath: "/tmp/logs")
    )

    @Test("without an override the app owns its data")
    func defaultsToSupport() {
        #expect(base.dataDirectory.path == "/tmp/support/data")
        #expect(base.databaseURL.path == "/tmp/support/data/runtime/prod/finances.db")
    }

    @Test("an override moves the database and the runtime directory, and nothing else")
    func overrideMovesOnlyData() {
        let moved = base.withDataDirectory(URL(filePath: "/elsewhere/data"))
        #expect(moved.databaseURL.path == "/elsewhere/data/runtime/prod/finances.db")
        #expect(moved.runtimeDirectory.path == "/elsewhere/data/runtime/prod")
        // Logs, the config, the instance lock and the port memory stay with the app: they are
        // properties of this installation, not of the financial data.
        #expect(moved.backendLogURL == base.backendLogURL)
        #expect(moved.configURL == base.configURL)
        #expect(moved.instanceLockURL == base.instanceLockURL)
        #expect(moved.portMemoryURL == base.portMemoryURL)
    }
}
