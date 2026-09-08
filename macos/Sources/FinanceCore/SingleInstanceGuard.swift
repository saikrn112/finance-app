import Darwin
import Foundation

/// An advisory `flock` held for the lifetime of the process.
///
/// Two instances of this app would race on one SQLite database, and SQLite's own
/// locking turns that into intermittent `database is locked` errors rather than a
/// clear refusal. `flock` is used rather than a pid file because the kernel releases
/// it when the process dies, however it dies -- a pid file left behind by a crash
/// has to be second-guessed, and second-guessing it is how a stale lock becomes a
/// permanent one.
public final class SingleInstanceGuard {
    /// Internal rather than private so a test can assert `FD_CLOEXEC` is set on it.
    /// That flag is the difference between a lock and a permanent deadlock, and it is
    /// invisible from the outside otherwise.
    let descriptor: Int32
    public let lockURL: URL

    private init(descriptor: Int32, lockURL: URL) {
        self.descriptor = descriptor
        self.lockURL = lockURL
    }

    public enum Failure: Error, CustomStringConvertible {
        case alreadyRunning
        case cannotOpenLock(path: String, errno: Int32)

        public var description: String {
            switch self {
            case .alreadyRunning: "another instance is already running"
            case .cannotOpenLock(let path, let code):
                "cannot open lock file \(path): \(String(cString: strerror(code)))"
            }
        }
    }

    /// Acquire the lock, or throw `.alreadyRunning`.
    public static func acquire(at lockURL: URL, fileManager: FileManager = .default) throws -> SingleInstanceGuard {
        try fileManager.createDirectory(
            at: lockURL.deletingLastPathComponent(),
            withIntermediateDirectories: true,
            attributes: [.posixPermissions: 0o700]
        )
        // O_CLOEXEC is not optional here, it is the whole difference between a lock
        // and a deadlock. Without it the backend inherits this descriptor, so an
        // orphaned backend keeps holding the lock after the shell dies -- and since
        // the guard runs before the stale-backend sweep, every subsequent launch is
        // refused with "already running" and the app can never recover. Observed:
        // one SIGKILL of the shell made the app permanently unlaunchable.
        let fd = open(lockURL.path, O_RDWR | O_CREAT | O_CLOEXEC, 0o600)
        guard fd >= 0 else {
            throw Failure.cannotOpenLock(path: lockURL.path, errno: errno)
        }
        guard flock(fd, LOCK_EX | LOCK_NB) == 0 else {
            close(fd)
            throw Failure.alreadyRunning
        }
        return SingleInstanceGuard(descriptor: fd, lockURL: lockURL)
    }

    public func release() {
        flock(descriptor, LOCK_UN)
        close(descriptor)
    }
}
