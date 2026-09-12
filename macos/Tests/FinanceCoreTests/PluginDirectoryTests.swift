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
