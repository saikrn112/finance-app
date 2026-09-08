import Foundation

/// A size-rotated append-only log file.
///
/// Rotation happens at *open* time, not on every write. The backend writes to the
/// file descriptor directly, so the shell never sees the individual writes -- there
/// is no hook to check a size on. Checking at open means a single long-running
/// session can exceed the cap, which is the honest trade for not interposing on the
/// child's stdout.
public struct LogFile: Sendable {
    public let url: URL
    public let maxBytes: Int
    /// How many rotated generations to keep (`backend.log.1` … `.N`).
    public let generations: Int

    public init(url: URL, maxBytes: Int = 4 * 1024 * 1024, generations: Int = 3) {
        self.url = url
        self.maxBytes = maxBytes
        self.generations = generations
    }

    /// Rotate if needed, then return a file descriptor open for appending.
    ///
    /// The descriptor is the caller's to close. It is handed to `posix_spawn`, so it
    /// must be a real fd rather than a `FileHandle` wrapper.
    public func openForAppending(fileManager: FileManager = .default) throws -> Int32 {
        try rotateIfNeeded(fileManager: fileManager)
        try fileManager.createDirectory(
            at: url.deletingLastPathComponent(),
            withIntermediateDirectories: true,
            attributes: [.posixPermissions: 0o700]
        )
        // 0600: backend logs can contain request paths and provider error text.
        let fd = open(url.path, O_WRONLY | O_CREAT | O_APPEND, 0o600)
        guard fd >= 0 else { throw LogFileError.cannotOpen(path: url.path, errno: errno) }
        return fd
    }

    public func rotateIfNeeded(fileManager: FileManager = .default) throws {
        guard let size = try? fileManager.attributesOfItem(atPath: url.path)[.size] as? Int,
              size >= maxBytes
        else { return }

        // Oldest first, so nothing is overwritten before it has been shifted.
        let oldest = url.appendingPathExtension("\(generations)")
        try? fileManager.removeItem(at: oldest)
        for generation in stride(from: generations - 1, through: 1, by: -1) {
            let from = url.appendingPathExtension("\(generation)")
            let to = url.appendingPathExtension("\(generation + 1)")
            if fileManager.fileExists(atPath: from.path) {
                try? fileManager.removeItem(at: to)
                try fileManager.moveItem(at: from, to: to)
            }
        }
        try fileManager.moveItem(at: url, to: url.appendingPathExtension("1"))
    }
}

public enum LogFileError: Error, CustomStringConvertible {
    case cannotOpen(path: String, errno: Int32)

    public var description: String {
        switch self {
        case .cannotOpen(let path, let code):
            "cannot open \(path): \(String(cString: strerror(code)))"
        }
    }
}
