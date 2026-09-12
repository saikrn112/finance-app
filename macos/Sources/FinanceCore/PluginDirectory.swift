import Foundation

/// The user's private plugin repository.
///
/// ## Why the app cannot ship these
///
/// Provider-specific parsers, aliases and categorisation rules live in a separate git repository
/// precisely so they never enter the public tree. The bundle carries only the tracked template
/// plugins, so a desktop app with nothing configured can read a generic CSV and *cannot* parse the
/// owner's actual statements or payslips. That is the correct default -- silently falling back to
/// the templates would look like working software that quietly mis-parses real money.
///
/// ## Why it is referenced in place rather than copied
///
/// The directory is not read-only input. `rules/categories.yaml` is **appended to at runtime**
/// every time a merchant is categorised from the uncategorised review, and those learned rules are
/// meant to end up committed to the plugins repository. Copying the folder into Application Support
/// would fork them: the app would learn rules the repository never sees, and the repository would
/// gain rules the app never applies.
///
/// So the app points at the working copy, which is also exactly what the container flow does with
/// `FINANCE_PLUGINS_DIR`.
public enum PluginDirectory {
    /// Where the chosen path is remembered. `UserDefaults`, not the database: it is a property of
    /// this machine rather than of the financial data, and a restored backup from another machine
    /// must not drag a path that does not exist there.
    public static let defaultsKey = "privatePluginsDirectory"

    /// What is wrong with a candidate directory, in the order it is worth telling the user.
    public enum Problem: Equatable, Sendable {
        case missing
        case notADirectory
        /// No `*.py` at the top level. Almost always the parent of the repository, or its `src`.
        case noPluginModules
        /// `rules/` is absent and cannot be created.
        case rulesDirectoryUnavailable
        /// Learned categorisation rules are appended here at runtime. A read-only directory
        /// surfaces as an opaque 500 from the "Apply" button in the uncategorised review, with
        /// nothing pointing at the cause.
        case notWritable

        public var explanation: String {
            switch self {
            case .missing:
                "That folder does not exist."
            case .notADirectory:
                "That is a file, not a folder."
            case .noPluginModules:
                "No plugin files (*.py) directly inside it — is this the right folder?"
            case .rulesDirectoryUnavailable:
                "Its `rules` folder is missing and could not be created."
            case .notWritable:
                "It is not writable. Learned categorisation rules are saved into it, and a "
                    + "read-only folder makes the uncategorised review fail with no explanation."
            }
        }
    }

    public enum Status: Equatable, Sendable {
        case notConfigured
        case ready(URL)
        case unusable(URL, Problem)

        /// The directory to hand the backend, or `nil` — in which case `FINANCE_PLUGINS_DIR` is
        /// left unset and the backend falls back to the bundled templates.
        public var usableDirectory: URL? {
            if case .ready(let url) = self { return url }
            return nil
        }
    }

    // MARK: - Persistence

    public static func remembered(defaults: UserDefaults = .standard) -> URL? {
        guard let path = defaults.string(forKey: defaultsKey), !path.isEmpty else { return nil }
        return URL(filePath: path)
    }

    public static func remember(_ url: URL?, defaults: UserDefaults = .standard) {
        if let url {
            defaults.set(url.path, forKey: defaultsKey)
        } else {
            defaults.removeObject(forKey: defaultsKey)
        }
    }

    /// The current status, re-derived from disk every time.
    ///
    /// Not cached: the folder is a git working copy the user edits outside this app, and it can be
    /// moved, renamed or made read-only between launches. A cached "ready" would then be a lie.
    public static func status(
        defaults: UserDefaults = .standard, fileManager: FileManager = .default
    ) -> Status {
        guard let url = remembered(defaults: defaults) else { return .notConfigured }
        if let problem = diagnose(url, fileManager: fileManager) {
            return .unusable(url, problem)
        }
        return .ready(url)
    }

    // MARK: - Validation

    /// The first thing wrong with `url`, or `nil` if it is usable.
    ///
    /// Ordered from most to least fundamental, so the message names the actual cause rather than a
    /// consequence of it.
    public static func diagnose(_ url: URL, fileManager: FileManager = .default) -> Problem? {
        var isDirectory: ObjCBool = false
        guard fileManager.fileExists(atPath: url.path, isDirectory: &isDirectory) else {
            return .missing
        }
        guard isDirectory.boolValue else { return .notADirectory }

        let entries = (try? fileManager.contentsOfDirectory(atPath: url.path)) ?? []
        guard entries.contains(where: { $0.hasSuffix(".py") && !$0.hasPrefix("_") }) else {
            return .noPluginModules
        }

        // Checked by attempting it, not by reading permission bits: the answer depends on ACLs,
        // the mount, and the sandbox, and only the attempt accounts for all three.
        guard fileManager.isWritableFile(atPath: url.path) else { return .notWritable }

        let rules = url.appending(path: "rules")
        if !fileManager.fileExists(atPath: rules.path) {
            do {
                try fileManager.createDirectory(at: rules, withIntermediateDirectories: true)
            } catch {
                return .rulesDirectoryUnavailable
            }
        }
        guard fileManager.isWritableFile(atPath: rules.path) else { return .notWritable }

        return nil
    }
}
