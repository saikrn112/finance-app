import Foundation

/// Whether this device takes part in multi-device sync, and optionally through what.
///
/// Two independent settings, and conflating them was a real design error worth not repeating:
///
/// * `enabled` turns sync on. Off by default -- publishing writes financial history where other
///   devices can read it, so it starts when someone decides it should.
/// * `folder` is an **optional override** for the narrow case of a genuinely shared volume.
///
/// An earlier version derived "enabled" from "a folder is set", which made Google Drive unreachable
/// from this app. That is backwards: Drive is the only transport that spans devices, so it is what
/// sync means by default. A folder reaches only processes that can see that filesystem, so treating
/// one as the norm quietly reduces multi-device sync to single-machine sync -- a phone or a second Mac
/// could never join.
public enum SyncSettings {
    public static let enabledKey = "syncEnabled"
    public static let folderKey = "syncFolder"

    public static func isEnabled(defaults: UserDefaults = .standard) -> Bool {
        defaults.bool(forKey: enabledKey)
    }

    public static func setEnabled(_ enabled: Bool, defaults: UserDefaults = .standard) {
        defaults.set(enabled, forKey: enabledKey)
    }

    /// The shared-volume override, or nil to use Google Drive.
    ///
    /// Created if missing -- it is ours to own -- but a path that is a file, or unwritable, resolves to
    /// nil so sync falls back to Drive rather than silently doing nothing.
    public static func folder(
        defaults: UserDefaults = .standard, fileManager: FileManager = .default
    ) -> URL? {
        guard let path = defaults.string(forKey: folderKey), !path.isEmpty else { return nil }
        let url = URL(filePath: path)
        var isDirectory: ObjCBool = false
        if fileManager.fileExists(atPath: url.path, isDirectory: &isDirectory) {
            guard isDirectory.boolValue, fileManager.isWritableFile(atPath: url.path) else {
                return nil
            }
            return url
        }
        return (try? fileManager.createDirectory(at: url, withIntermediateDirectories: true))
            .map { _ in url }
    }

    public static func setFolder(_ url: URL?, defaults: UserDefaults = .standard) {
        if let url {
            defaults.set(url.path, forKey: folderKey)
        } else {
            defaults.removeObject(forKey: folderKey)
        }
    }

    /// What to tell the log, so "my devices are not syncing" is not invisible from inside the app.
    public static func describe(defaults: UserDefaults = .standard) -> String {
        guard isEnabled(defaults: defaults) else { return "device sync: off" }
        if let folder = folder(defaults: defaults) {
            return "device sync: shared folder \(folder.path)"
        }
        if defaults.string(forKey: folderKey)?.isEmpty == false {
            return "device sync: on, but the configured folder is unusable -- falling back to Google Drive"
        }
        return "device sync: on, via Google Drive"
    }
}
