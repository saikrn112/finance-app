import Foundation

/// Whether this device takes part in multi-device sync.
///
/// One setting, and one transport: Google Drive, reusing the vault's existing connection. Off by
/// default -- publishing writes financial history where other devices can read it, so it starts when
/// someone decides it should.
///
/// A directory-based alternative existed briefly and was removed. A folder only reaches processes that
/// can see that filesystem, so it is single-machine sync wearing the label of multi-device sync: a
/// phone or a second Mac could never join, which is the entire point of the feature.
public enum SyncSettings {
    public static let enabledKey = "syncEnabled"

    public static func isEnabled(defaults: UserDefaults = .standard) -> Bool {
        defaults.bool(forKey: enabledKey)
    }

    public static func setEnabled(_ enabled: Bool, defaults: UserDefaults = .standard) {
        defaults.set(enabled, forKey: enabledKey)
    }

    /// What to tell the log, so "my devices are not syncing" is not invisible from inside the app.
    public static func describe(defaults: UserDefaults = .standard) -> String {
        isEnabled(defaults: defaults) ? "device sync: on, via Google Drive" : "device sync: off"
    }
}
