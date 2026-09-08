import CryptoKit
import Foundation

/// The per-launch shared secret between the shell, the webview and the backend.
///
/// Regenerated every launch and never written to disk: it protects a port that only
/// exists while the app is running, so persisting it would add a file to protect
/// without adding any safety.
public struct SessionToken: Sendable, Equatable {
    public let value: String

    /// 32 bytes of `SecRandomCopyBytes`-grade entropy, URL-safe base64.
    public init() {
        var bytes = [UInt8](repeating: 0, count: 32)
        // Reading /dev/urandom would work too; SystemRandomNumberGenerator is the
        // documented cryptographically-secure source on Darwin.
        var generator = SystemRandomNumberGenerator()
        for index in bytes.indices {
            bytes[index] = UInt8.random(in: .min ... .max, using: &generator)
        }
        self.value = Data(bytes)
            .base64EncodedString()
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
    }

    /// For tests only.
    public init(value: String) {
        self.value = value
    }

    /// A short, non-reversible fingerprint, safe for a log line or a diagnostics
    /// panel: enough to tell two launches apart, not enough to authenticate with.
    /// Never log `value` itself.
    public var fingerprint: String {
        let digest = SHA256.hash(data: Data(value.utf8))
        return digest.prefix(4).map { String(format: "%02x", $0) }.joined()
    }
}
