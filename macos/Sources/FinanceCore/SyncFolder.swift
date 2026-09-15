import Foundation

/// The folder this app exchanges sync payloads through.
///
/// Off unless set, deliberately: publishing writes the user's financial history somewhere another
/// process can read it, so it starts when someone decides it should rather than by default.
///
/// A folder is preferred over Google Drive for two devices on one machine -- the data never leaves it,
/// and the container app sees the same directory through its `./data` bind mount. Drive remains the
/// answer for genuinely separate machines, and the backend picks it automatically when no folder is
/// configured.
public enum SyncFolder {
    public static let defaultsKey = "syncFolder"

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

    /// The folder to hand the backend, or nil.
    ///
    /// Re-derived from disk each launch and **created if missing**: unlike the plugins folder, this
    /// one is ours to own, and refusing to sync because the directory has not been made yet would be
    /// an unhelpful way to fail. Only an unusable path -- a file, or somewhere unwritable -- disables
    /// sync, and that is reported rather than silently ignored.
    public static func usable(
        defaults: UserDefaults = .standard, fileManager: FileManager = .default
    ) -> URL? {
        guard let url = remembered(defaults: defaults) else { return nil }
        var isDirectory: ObjCBool = false
        if fileManager.fileExists(atPath: url.path, isDirectory: &isDirectory) {
            guard isDirectory.boolValue, fileManager.isWritableFile(atPath: url.path) else {
                return nil
            }
            return url
        }
        do {
            try fileManager.createDirectory(at: url, withIntermediateDirectories: true)
            return url
        } catch {
            return nil
        }
    }
}
