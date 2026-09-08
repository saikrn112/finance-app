import Darwin
import Foundation

/// A child process that is the leader of its own process group.
///
/// Foundation's `Process` is not used here for one reason: it gives the child the
/// parent's process group, and killing a recorded pid is not enough. The repo
/// already paid for this once -- `AGENTS.md` caveat #10 records that `npx` forks a
/// grandchild, so killing the pid it printed left the real process running. A
/// leaked uvicorn holding the SQLite file is exactly the same failure with a worse
/// symptom, so the shell spawns into a fresh process group and signals the whole
/// group.
public final class SpawnedProcess: @unchecked Sendable {
    public let processIdentifier: pid_t
    /// Equal to `processIdentifier`: the child is its own group leader, so the group
    /// can be signalled as `-pid`.
    public var processGroupIdentifier: pid_t { processIdentifier }

    private let lock = NSLock()
    private var reapedStatus: Int32?

    private init(processIdentifier: pid_t) {
        self.processIdentifier = processIdentifier
    }

    public enum Failure: Error, CustomStringConvertible {
        case executableMissing(path: String)
        case spawnFailed(errno: Int32)

        public var description: String {
            switch self {
            case .executableMissing(let path): "not executable: \(path)"
            case .spawnFailed(let code): "posix_spawn failed: \(String(cString: strerror(code)))"
            }
        }
    }

    /// - Parameters:
    ///   - environment: the child's *entire* environment. Nothing is inherited: the
    ///     backend runs under `-I`, and inheriting the parent's environment would
    ///     reintroduce exactly the machine-specific variables that isolation exists
    ///     to exclude.
    ///   - outputDescriptor: receives both stdout and stderr. Not closed by this call.
    public static func spawn(
        executable: URL,
        arguments: [String],
        environment: [String: String],
        workingDirectory: URL?,
        outputDescriptor: Int32
    ) throws -> SpawnedProcess {
        guard FileManager.default.isExecutableFile(atPath: executable.path) else {
            throw Failure.executableMissing(path: executable.path)
        }

        var attributes: posix_spawnattr_t?
        posix_spawnattr_init(&attributes)
        defer { posix_spawnattr_destroy(&attributes) }

        // POSIX_SPAWN_SETPGROUP with group 0 makes the child its own group leader.
        // POSIX_SPAWN_SETSIGDEF resets inherited signal dispositions, so the child
        // is not born ignoring SIGTERM because the shell happened to.
        // POSIX_SPAWN_CLOEXEC_DEFAULT closes every descriptor not named in
        // `fileActions`. Defaulting to "inherit" is how the single-instance flock
        // ended up held by the backend, which made the app permanently unlaunchable
        // after one crash; opting in per descriptor is the safe direction.
        posix_spawnattr_setflags(
            &attributes,
            Int16(POSIX_SPAWN_SETPGROUP | POSIX_SPAWN_SETSIGDEF | POSIX_SPAWN_CLOEXEC_DEFAULT)
        )
        posix_spawnattr_setpgroup(&attributes, 0)
        var defaultSignals = sigset_t()
        sigfillset(&defaultSignals)
        posix_spawnattr_setsigdefault(&attributes, &defaultSignals)

        var fileActions: posix_spawn_file_actions_t?
        posix_spawn_file_actions_init(&fileActions)
        defer { posix_spawn_file_actions_destroy(&fileActions) }
        // stdin from /dev/null: a backend that blocks on a read from an inherited
        // terminal is impossible to diagnose from a GUI app.
        posix_spawn_file_actions_addopen(&fileActions, 0, "/dev/null", O_RDONLY, 0)
        posix_spawn_file_actions_adddup2(&fileActions, outputDescriptor, 1)
        posix_spawn_file_actions_adddup2(&fileActions, outputDescriptor, 2)
        if let workingDirectory {
            // The _np spelling is deprecated as of macOS 26 but is the only one
            // available below it; the deployment target is macOS 14.
            if #available(macOS 26.0, *) {
                posix_spawn_file_actions_addchdir(&fileActions, workingDirectory.path)
            } else {
                posix_spawn_file_actions_addchdir_np(&fileActions, workingDirectory.path)
            }
        }

        let argv = [executable.path] + arguments
        let envp = environment.map { "\($0.key)=\($0.value)" }

        var pid: pid_t = 0
        let status = withCStringArray(argv) { argvPointers in
            withCStringArray(envp) { envpPointers in
                posix_spawn(&pid, executable.path, &fileActions, &attributes, argvPointers, envpPointers)
            }
        }
        guard status == 0 else { throw Failure.spawnFailed(errno: status) }
        return SpawnedProcess(processIdentifier: pid)
    }

    // MARK: - Lifecycle

    /// Whether the process group still has a live member.
    public var isRunning: Bool {
        lock.lock()
        defer { lock.unlock() }
        if reapedStatus != nil { return false }
        // Signal 0 tests for existence without delivering anything.
        return kill(processIdentifier, 0) == 0 || errno == EPERM
    }

    /// Reap the child if it has exited. Returns its exit status, or `nil` if it is
    /// still running. Must be called to avoid a zombie.
    @discardableResult
    public func reapIfExited() -> Int32? {
        lock.lock()
        defer { lock.unlock() }
        if let reapedStatus { return reapedStatus }
        var status: Int32 = 0
        let result = waitpid(processIdentifier, &status, WNOHANG)
        guard result == processIdentifier else { return nil }
        let exitCode = exitCodeFromWaitStatus(status)
        reapedStatus = exitCode
        return exitCode
    }

    /// Block until the child exits, then return its exit status.
    @discardableResult
    public func wait() -> Int32 {
        lock.lock()
        if let reapedStatus {
            lock.unlock()
            return reapedStatus
        }
        lock.unlock()

        var status: Int32 = 0
        while true {
            let result = waitpid(processIdentifier, &status, 0)
            if result == processIdentifier { break }
            if result < 0 && errno == EINTR { continue }
            // ECHILD: already reaped elsewhere.
            lock.lock()
            let known = reapedStatus ?? -1
            lock.unlock()
            return known
        }
        let exitCode = exitCodeFromWaitStatus(status)
        lock.lock()
        reapedStatus = exitCode
        lock.unlock()
        return exitCode
    }

    /// SIGTERM the whole group, then SIGKILL anything still alive after `graceSeconds`.
    ///
    /// Signalling `-pid` rather than `pid` is the entire point of the custom spawn:
    /// it reaches grandchildren too.
    public func terminateGroup(graceSeconds: TimeInterval = 5) {
        kill(-processIdentifier, SIGTERM)
        let deadline = Date().addingTimeInterval(graceSeconds)
        while Date() < deadline {
            if reapIfExited() != nil { return }
            usleep(50_000)
        }
        kill(-processIdentifier, SIGKILL)
        _ = wait()
    }
}

// MARK: - Helpers

private func exitCodeFromWaitStatus(_ status: Int32) -> Int32 {
    // Mirrors WIFEXITED/WEXITSTATUS/WTERMSIG, which are macros and so unavailable
    // to Swift. A signalled death is reported as -signal to keep the two cases
    // distinguishable in one Int32.
    if status & 0x7F == 0 { return (status >> 8) & 0xFF }
    return -(status & 0x7F)
}

/// Run `body` with a NULL-terminated `char *[]` built from `strings`.
private func withCStringArray<Result>(
    _ strings: [String],
    _ body: (UnsafeMutablePointer<UnsafeMutablePointer<CChar>?>) -> Result
) -> Result {
    var pointers: [UnsafeMutablePointer<CChar>?] = strings.map { strdup($0) }
    pointers.append(nil)
    defer { for pointer in pointers where pointer != nil { free(pointer) } }
    return pointers.withUnsafeMutableBufferPointer { body($0.baseAddress!) }
}
