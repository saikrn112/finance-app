import Foundation

/// Which `data/` directory the app uses.
///
/// ## Why this is configurable
///
/// The app began with its own copy in Application Support, which was the right call to get started
/// and the wrong one to keep: the container app and the desktop app then each accumulate their own
/// history and drift apart in *both* directions. Measured after five days — the app's copy had three
/// transactions the repository's did not, and the repository's had thirty more project links, three
/// hundred splits and twenty-two notes the app's did not. Neither was a subset of the other, and
/// nothing warned about it.
///
/// So the location is a setting. Pointing at one directory is what makes "the desktop app" and "the
/// container app" the same app rather than two forks of the same data.
///
/// ## Why nothing merges
///
/// Two SQLite databases that have both been written cannot be reconciled automatically without
/// inventing an answer for every row that differs. Choosing which one wins is a decision about real
/// financial history, so the app moves the pointer and never the data.
public enum DataDirectory {
    public static let defaultsKey = "dataDirectory"

    public enum Problem: Equatable, Sendable {
        case missing
        case notADirectory
        case notWritable
        /// No `runtime/prod` and it cannot be created. Usually the repository root rather than its
        /// `data/`, which looks plausible and would start an empty database beside the real one.
        case runtimeDirectoryUnavailable

        public var explanation: String {
            switch self {
            case .missing: "That folder does not exist."
            case .notADirectory: "That is a file, not a folder."
            case .notWritable: "It is not writable, and the database has to be written."
            case .runtimeDirectoryUnavailable:
                "It has no `runtime/prod` folder and one could not be created — did you pick the "
                    + "repository root instead of its `data` folder?"
            }
        }
    }

    /// Whether a database already exists there, and how big it is.
    ///
    /// Shown before switching, because "point at this folder" reads very differently depending on
    /// whether it holds five years of history or nothing at all — and starting an empty database
    /// beside a real one is the mistake worth making impossible to do silently.
    public struct Contents: Equatable, Sendable {
        public let databaseExists: Bool
        public let databaseBytes: Int

        public var summary: String {
            guard databaseExists else { return "no database yet — a new, empty one would be created" }
            let megabytes = Double(databaseBytes) / 1_048_576
            return String(format: "existing database, %.1f MB", megabytes)
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

    /// The directory to use: the remembered one if it is usable, otherwise the app's own.
    ///
    /// Falls back rather than failing, and says so in the log: a database that has moved should not
    /// stop the app launching, and the diagnostics row shows which one is in use.
    public static func resolved(
        default fallback: URL,
        defaults: UserDefaults = .standard,
        fileManager: FileManager = .default
    ) -> (url: URL, problem: Problem?) {
        guard let chosen = remembered(defaults: defaults) else { return (fallback, nil) }
        if let problem = diagnose(chosen, fileManager: fileManager) {
            return (fallback, problem)
        }
        return (chosen, nil)
    }

    // MARK: - Validation

    public static func diagnose(_ url: URL, fileManager: FileManager = .default) -> Problem? {
        var isDirectory: ObjCBool = false
        guard fileManager.fileExists(atPath: url.path, isDirectory: &isDirectory) else {
            return .missing
        }
        guard isDirectory.boolValue else { return .notADirectory }
        guard fileManager.isWritableFile(atPath: url.path) else { return .notWritable }

        let runtime = url.appending(path: "runtime/prod")
        if !fileManager.fileExists(atPath: runtime.path) {
            do {
                try fileManager.createDirectory(at: runtime, withIntermediateDirectories: true)
            } catch {
                return .runtimeDirectoryUnavailable
            }
        }
        guard fileManager.isWritableFile(atPath: runtime.path) else { return .notWritable }
        return nil
    }

    public static func contents(
        of url: URL, fileManager: FileManager = .default
    ) -> Contents {
        let database = url.appending(path: "runtime/prod/finances.db")
        let size = (try? fileManager.attributesOfItem(atPath: database.path)[.size] as? Int) ?? nil
        return Contents(databaseExists: size != nil, databaseBytes: size ?? 0)
    }
}
