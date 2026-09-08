import Darwin
import Foundation

/// What the shell remembers about the backend across launches, so a leaked child can
/// be cleaned up.
///
/// This exists because a crash or a `SIGKILL` of the shell gives
/// `applicationWillTerminate` no chance to run, and the orphaned uvicorn keeps the
/// SQLite file open. The next launch then fails in a way that looks nothing like its
/// cause.
public struct RuntimeState: Codable, Sendable, Equatable {
    public var processGroupIdentifier: pid_t
    /// Used to tell "our leaked backend" from "an unrelated process that has since
    /// been assigned the same pid". Pid reuse is not hypothetical on a machine that
    /// has been up for a while, and killing a stranger's process is far worse than
    /// failing to clean up ours.
    public var startedAt: Date
    /// The bundled interpreter path, checked against the live process before
    /// signalling anything.
    public var executablePath: String

    public init(processGroupIdentifier: pid_t, startedAt: Date, executablePath: String) {
        self.processGroupIdentifier = processGroupIdentifier
        self.startedAt = startedAt
        self.executablePath = executablePath
    }

    public func write(to url: URL) throws {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        let data = try encoder.encode(self)
        try data.write(to: url, options: .atomic)
        try? FileManager.default.setAttributes(
            [.posixPermissions: 0o600], ofItemAtPath: url.path
        )
    }

    public static func read(from url: URL) -> RuntimeState? {
        guard let data = try? Data(contentsOf: url) else { return nil }
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        return try? decoder.decode(RuntimeState.self, from: data)
    }

    public static func clear(at url: URL) {
        try? FileManager.default.removeItem(at: url)
    }
}

/// Kills a backend left behind by a previous launch.
public enum StaleBackendSweeper {
    public enum Result: Sendable, Equatable {
        case nothingRecorded
        case alreadyGone
        /// The recorded pid is live but is not our interpreter -- pid reuse. Left alone.
        case notOurs(runningExecutable: String?)
        case terminated(pid_t)
    }

    /// - Parameter runningExecutablePath: injected so this is testable without
    ///   actually spawning something. Defaults to asking the kernel.
    public static func sweep(
        stateURL: URL,
        expectedExecutable: String,
        runningExecutablePath: (pid_t) -> String? = executablePath(forProcess:),
        terminate: (pid_t) -> Void = { kill(-$0, SIGTERM) }
    ) -> Result {
        guard let state = RuntimeState.read(from: stateURL) else { return .nothingRecorded }
        defer { RuntimeState.clear(at: stateURL) }

        let pid = state.processGroupIdentifier
        guard pid > 1 else { return .alreadyGone }
        guard kill(pid, 0) == 0 || errno == EPERM else { return .alreadyGone }

        // Confirm identity before signalling. `kill(-pid, ...)` on a stranger's group
        // is the worst possible outcome of a cleanup routine, so a mismatch means we
        // do nothing at all rather than guess.
        let actual = runningExecutablePath(pid)
        guard let actual, actual == expectedExecutable else {
            return .notOurs(runningExecutable: actual)
        }

        terminate(pid)
        return .terminated(pid)
    }

    /// The executable path of a running process, via `KERN_PROCARGS2`' simpler
    /// cousin `proc_pidpath`.
    public static func executablePath(forProcess pid: pid_t) -> String? {
        var buffer = [UInt8](repeating: 0, count: Int(4 * MAXPATHLEN))
        let length = proc_pidpath(pid, &buffer, UInt32(buffer.count))
        guard length > 0 else { return nil }
        return String(decoding: buffer[0..<Int(length)], as: UTF8.self)
    }
}
