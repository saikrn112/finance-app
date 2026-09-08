import Darwin
import Foundation
import Testing

@testable import FinanceCore

@Suite("PortAllocator")
struct PortAllocatorTests {
    @Test("returns a port that can actually be bound")
    func portIsBindable() throws {
        let port = try PortAllocator.freeLoopbackPort()
        #expect(port > 1024, "should be an ephemeral port, got \(port)")

        // Prove it: bind it for real. If the allocator returned something already in
        // use, this fails -- which is the whole claim being made.
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        #expect(fd >= 0)
        defer { close(fd) }
        var address = sockaddr_in()
        address.sin_family = sa_family_t(AF_INET)
        address.sin_port = port.bigEndian
        address.sin_addr.s_addr = INADDR_LOOPBACK.bigEndian
        address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        let result = withUnsafePointer(to: &address) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.bind(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        #expect(result == 0, "the allocated port \(port) was not bindable")
    }

    @Test("successive calls generally differ")
    func portsVary() throws {
        // Not a guarantee the kernel makes, so this only asserts that we are not
        // returning a constant -- which a bug in the getsockname path would.
        let ports = try (0..<8).map { _ in try PortAllocator.freeLoopbackPort() }
        #expect(Set(ports).count > 1)
    }
}

@Suite("SessionToken")
struct SessionTokenTests {
    @Test("tokens are long, URL-safe, and distinct")
    func tokenShape() {
        let first = SessionToken()
        let second = SessionToken()
        #expect(first != second)
        // 32 bytes base64 without padding.
        #expect(first.value.count == 43)
        // URL-safe: the token travels as a cookie value and an HTTP header, and `+`
        // or `/` in either is a source of silent corruption.
        #expect(first.value.allSatisfy { $0.isLetter || $0.isNumber || $0 == "-" || $0 == "_" })
    }

    @Test("the fingerprint is short, stable, and not the token")
    func fingerprint() {
        let token = SessionToken(value: "a-known-test-value")
        #expect(token.fingerprint.count == 8)
        #expect(token.fingerprint == SessionToken(value: "a-known-test-value").fingerprint)
        #expect(token.fingerprint != SessionToken(value: "a-known-test-valuf").fingerprint)
        // The thing that must never happen: the secret appearing in a "safe" label.
        #expect(!token.fingerprint.contains("known"))
    }
}

@Suite("LogFile")
struct LogFileTests {
    private func makeDirectory() -> URL {
        let url = FileManager.default.temporaryDirectory
            .appending(path: "logs-\(UUID().uuidString)")
        try? FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        return url
    }

    @Test("appends rather than truncating")
    func appends() throws {
        let directory = makeDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let logFile = LogFile(url: directory.appending(path: "backend.log"))

        for line in ["first\n", "second\n"] {
            let fd = try logFile.openForAppending()
            _ = line.withCString { write(fd, $0, strlen($0)) }
            close(fd)
        }

        let contents = try String(contentsOf: logFile.url, encoding: .utf8)
        #expect(contents == "first\nsecond\n")
    }

    @Test("rotates when over the size cap and shifts generations")
    func rotates() throws {
        let directory = makeDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let url = directory.appending(path: "backend.log")
        let logFile = LogFile(url: url, maxBytes: 16, generations: 3)

        // Three rotations, each leaving a recognisable marker behind.
        for marker in ["AAAA", "BBBB", "CCCC"] {
            let fd = try logFile.openForAppending()
            let payload = String(repeating: marker, count: 8)  // 32 bytes, over the cap
            _ = payload.withCString { write(fd, $0, strlen($0)) }
            close(fd)
        }
        // Opening once more rotates CCCC out of the live file.
        close(try logFile.openForAppending())

        #expect(try String(contentsOf: url.appendingPathExtension("1"), encoding: .utf8).contains("CCCC"))
        #expect(try String(contentsOf: url.appendingPathExtension("2"), encoding: .utf8).contains("BBBB"))
        #expect(try String(contentsOf: url.appendingPathExtension("3"), encoding: .utf8).contains("AAAA"))
    }

    @Test("keeps only the configured number of generations")
    func discardsOldest() throws {
        let directory = makeDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let url = directory.appending(path: "backend.log")
        let logFile = LogFile(url: url, maxBytes: 16, generations: 2)

        for _ in 0..<6 {
            let fd = try logFile.openForAppending()
            _ = "0123456789012345678901234567890123456789".withCString {
                write(fd, $0, strlen($0))
            }
            close(fd)
        }

        #expect(FileManager.default.fileExists(atPath: url.appendingPathExtension("2").path))
        #expect(!FileManager.default.fileExists(atPath: url.appendingPathExtension("3").path))
    }

    @Test("a small file is not rotated")
    func doesNotRotateSmallFile() throws {
        let directory = makeDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let url = directory.appending(path: "backend.log")
        let logFile = LogFile(url: url, maxBytes: 1024)

        let fd = try logFile.openForAppending()
        _ = "short\n".withCString { write(fd, $0, strlen($0)) }
        close(fd)
        close(try logFile.openForAppending())

        #expect(!FileManager.default.fileExists(atPath: url.appendingPathExtension("1").path))
    }

    @Test("creates the log directory if it does not exist")
    func createsDirectory() throws {
        let root = FileManager.default.temporaryDirectory
            .appending(path: "logs-missing-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: root) }
        let logFile = LogFile(url: root.appending(path: "nested/backend.log"))
        close(try logFile.openForAppending())
        #expect(FileManager.default.fileExists(atPath: logFile.url.path))
    }

    @Test("the log is not world-readable")
    func permissions() throws {
        let directory = makeDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let logFile = LogFile(url: directory.appending(path: "backend.log"))
        close(try logFile.openForAppending())

        // Backend logs can contain request paths and provider error text.
        let attributes = try FileManager.default.attributesOfItem(atPath: logFile.url.path)
        let mode = (attributes[.posixPermissions] as? NSNumber)?.intValue ?? 0
        #expect(mode & 0o077 == 0, "log mode was \(String(mode, radix: 8))")
    }
}

@Suite("SingleInstanceGuard", .serialized)
struct SingleInstanceGuardTests {
    @Test("a second acquire in the same process is refused")
    func refusesSecond() throws {
        let url = FileManager.default.temporaryDirectory
            .appending(path: "guard-\(UUID().uuidString)/instance.lock")
        defer { try? FileManager.default.removeItem(at: url.deletingLastPathComponent()) }

        let first = try SingleInstanceGuard.acquire(at: url)
        defer { first.release() }

        #expect(throws: SingleInstanceGuard.Failure.self) {
            _ = try SingleInstanceGuard.acquire(at: url)
        }
    }

    @Test("the lock is reacquirable after release")
    func reacquirable() throws {
        let url = FileManager.default.temporaryDirectory
            .appending(path: "guard-\(UUID().uuidString)/instance.lock")
        defer { try? FileManager.default.removeItem(at: url.deletingLastPathComponent()) }

        try SingleInstanceGuard.acquire(at: url).release()
        // A stale lock that could not be reacquired would be worse than no lock: the
        // app would refuse to start forever after one crash.
        let second = try SingleInstanceGuard.acquire(at: url)
        second.release()
    }

    @Test("the lock descriptor is close-on-exec")
    func lockIsCloseOnExec() throws {
        // The bug this pins down made the app permanently unlaunchable after a single
        // crash. The backend inherited the flock descriptor; when the shell was
        // SIGKILLed it never released the lock, and the inherited descriptor kept the
        // open file description -- and therefore the lock -- alive. Because the guard
        // runs *before* the stale-backend sweep, every later launch was refused with
        // "already running" before it could clean anything up.
        //
        // Asserting the flag rather than the scenario: reproducing it needs a helper
        // process to SIGKILL, and a test that only calls release() passes either way,
        // because flock is keyed to the shared open file description and release()
        // drops it for the child too. That version of this test passed with the fix
        // reverted, which is worse than no test.
        let url = FileManager.default.temporaryDirectory
            .appending(path: "guard-cloexec-\(UUID().uuidString)/instance.lock")
        defer { try? FileManager.default.removeItem(at: url.deletingLastPathComponent()) }

        let guardInstance = try SingleInstanceGuard.acquire(at: url)
        defer { guardInstance.release() }

        let flags = fcntl(guardInstance.descriptor, F_GETFD)
        #expect(flags >= 0)
        #expect(flags & FD_CLOEXEC != 0, "the lock descriptor would be inherited by the backend")
    }

    @Test("creates the containing directory")
    func createsDirectory() throws {
        let url = FileManager.default.temporaryDirectory
            .appending(path: "guard-deep-\(UUID().uuidString)/a/b/instance.lock")
        defer {
            try? FileManager.default.removeItem(
                at: url.deletingLastPathComponent().deletingLastPathComponent()
                    .deletingLastPathComponent()
            )
        }
        try SingleInstanceGuard.acquire(at: url).release()
        #expect(FileManager.default.fileExists(atPath: url.path))
    }
}

@Suite("BundleLayout")
struct BundleLayoutTests {
    private let layout = BundleLayout(
        resourcesDirectory: URL(filePath: "/tmp/FinanceApp.app/Contents/Resources"),
        supportDirectory: URL(filePath: "/tmp/support"),
        logDirectory: URL(filePath: "/tmp/logs")
    )

    @Test("payload paths match what build_backend.sh assembles")
    func payloadPaths() {
        // These must stay in step with macos/scripts/build_backend.sh; a mismatch
        // surfaces as "the bundled Python interpreter is missing".
        #expect(layout.backendDirectory.lastPathComponent == "backend")
        #expect(layout.interpreterURL.path.hasSuffix("backend/python/bin/python3"))
        #expect(layout.bootstrapURL.path.hasSuffix("backend/bootstrap.py"))
        #expect(layout.webDirectory.path.hasSuffix("Resources/web"))
    }

    @Test("the runtime layout mirrors the container's")
    func runtimeLayout() {
        // Mirroring data/runtime/prod means an existing container database can be
        // pointed at rather than migrated.
        #expect(layout.runtimeDirectory.path.hasSuffix("data/runtime/prod"))
        #expect(layout.databaseURL.path.hasSuffix("data/runtime/prod/finances.db"))
    }

    @Test("nothing writable lives inside the bundle")
    func writablePathsAreOutsideTheBundle() {
        // A bundle is read-only once installed and signed; a write inside it would
        // also invalidate the signature.
        for url in [
            layout.databaseURL, layout.configURL, layout.backendLogURL,
            layout.runtimeStateURL, layout.instanceLockURL,
        ] {
            #expect(!url.path.contains(".app/"), "\(url.path) is inside the bundle")
        }
    }

    @Test("createSupportDirectories makes a private tree")
    func createsPrivateDirectories() throws {
        let root = FileManager.default.temporaryDirectory
            .appending(path: "layout-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: root) }
        let subject = BundleLayout(
            resourcesDirectory: root.appending(path: "Resources"),
            supportDirectory: root.appending(path: "support"),
            logDirectory: root.appending(path: "logs")
        )
        try subject.createSupportDirectories()

        for directory in [subject.supportDirectory, subject.runtimeDirectory, subject.logDirectory] {
            #expect(FileManager.default.fileExists(atPath: directory.path))
        }
        // This tree holds a financial database and provider secrets.
        let mode = (try FileManager.default
            .attributesOfItem(atPath: subject.supportDirectory.path)[.posixPermissions]
            as? NSNumber)?.intValue ?? 0
        #expect(mode & 0o077 == 0, "support directory mode was \(String(mode, radix: 8))")
        // Payload directories must not be conjured into existence -- doing so would
        // turn "the bundle is broken" into "the bundle is empty".
        #expect(!FileManager.default.fileExists(atPath: subject.backendDirectory.path))
    }
}

@Suite("BackendEnvironment")
struct BackendEnvironmentTests {
    private let layout = BundleLayout(
        resourcesDirectory: URL(filePath: "/tmp/FinanceApp.app/Contents/Resources"),
        supportDirectory: URL(filePath: "/tmp/support"),
        logDirectory: URL(filePath: "/tmp/logs")
    )

    @Test("the child is told the port, the token and where the data lives")
    func essentials() {
        let variables = BackendEnvironment(layout: layout)
            .variables(port: 51234, token: SessionToken(value: "tok"))
        #expect(variables["FINANCE_APP_PORT"] == "51234")
        #expect(variables["FINANCE_APP_HOST"] == "127.0.0.1")
        #expect(variables["FINANCE_APP_LOCAL_TOKEN"] == "tok")
        #expect(variables["FINANCE_APP_CONFIG"] == layout.configURL.path)
        #expect(variables["FINANCE_APP_DB_PATH"] == layout.databaseURL.path)
    }

    @Test("an unconfigured private plugins directory is absent, not empty")
    func pluginsDirectoryOmittedWhenUnset() {
        // An empty FINANCE_PLUGINS_DIR would make the backend resolve paths against
        // the filesystem root; absent makes it fall back to the bundled templates,
        // which is the documented behaviour.
        let variables = BackendEnvironment(layout: layout)
            .variables(port: 1, token: SessionToken(value: "t"))
        #expect(variables["FINANCE_PLUGINS_DIR"] == nil)

        let configured = BackendEnvironment(
            layout: layout, privatePluginsDirectory: URL(filePath: "/tmp/private-plugins")
        ).variables(port: 1, token: SessionToken(value: "t"))
        #expect(configured["FINANCE_PLUGINS_DIR"] == "/tmp/private-plugins")
    }

    @Test("output is unbuffered so a crash traceback reaches the log")
    func unbuffered() {
        let variables = BackendEnvironment(layout: layout)
            .variables(port: 1, token: SessionToken(value: "t"))
        #expect(variables["PYTHONUNBUFFERED"] == "1")
    }

    @Test("the redacted form never contains the token")
    func redaction() {
        let token = SessionToken(value: "super-secret-token-value")
        let redacted = BackendEnvironment(layout: layout)
            .redactedVariables(port: 1, token: token)
        #expect(redacted["FINANCE_APP_LOCAL_TOKEN"] != token.value)
        #expect(!redacted.values.contains { $0.contains("super-secret") })
        // Still useful: the fingerprint identifies the session.
        #expect(redacted["FINANCE_APP_LOCAL_TOKEN"]?.contains(token.fingerprint) == true)
    }

    @Test("PYTHONPATH is never set")
    func noPythonPath() {
        // `-I` implies `-E`, so the interpreter ignores PYTHON* path variables
        // entirely; setting them would be a comment that looks like a mechanism.
        // bootstrap.py builds sys.path in code instead.
        let variables = BackendEnvironment(layout: layout)
            .variables(port: 1, token: SessionToken(value: "t"))
        #expect(variables["PYTHONPATH"] == nil)
        #expect(variables["PYTHONHOME"] == nil)
    }
}

@Suite("PortAllocator: remembered port")
struct RememberedPortTests {
    private func memoryURL() -> URL {
        FileManager.default.temporaryDirectory.appending(path: "port-\(UUID().uuidString)")
    }

    @Test("the first call allocates and records a port")
    func recordsOnFirstUse() throws {
        let url = memoryURL()
        defer { try? FileManager.default.removeItem(at: url) }

        let port = try PortAllocator.preferredLoopbackPort(rememberedAt: url)
        #expect(port > 1024)
        let recorded = try String(contentsOf: url, encoding: .utf8)
        #expect(UInt16(recorded.trimmingCharacters(in: .whitespacesAndNewlines)) == port)
    }

    @Test("a later call reuses the same port")
    func reusesRememberedPort() throws {
        // The whole point: a stable port means a stable origin, and a stable origin means
        // the frontend's localStorage survives a relaunch. A fresh port every launch
        // reopened the twelve-step getting-started tour every launch.
        let url = memoryURL()
        defer { try? FileManager.default.removeItem(at: url) }

        let first = try PortAllocator.preferredLoopbackPort(rememberedAt: url)
        let second = try PortAllocator.preferredLoopbackPort(rememberedAt: url)
        #expect(first == second)
    }

    @Test("a taken port is replaced rather than refused")
    func fallsBackWhenTaken() throws {
        let url = memoryURL()
        defer { try? FileManager.default.removeItem(at: url) }

        let taken = try PortAllocator.freeLoopbackPort()
        try String(taken).write(to: url, atomically: true, encoding: .utf8)

        // Actually hold it, so the bindability check has something real to fail against.
        let holder = socket(AF_INET, SOCK_STREAM, 0)
        #expect(holder >= 0)
        defer { close(holder) }
        var address = sockaddr_in()
        address.sin_family = sa_family_t(AF_INET)
        address.sin_port = taken.bigEndian
        address.sin_addr.s_addr = INADDR_LOOPBACK.bigEndian
        address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        let bound = withUnsafePointer(to: &address) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.bind(holder, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        #expect(bound == 0)

        // Losing localStorage for one launch beats refusing to start.
        let allocated = try PortAllocator.preferredLoopbackPort(rememberedAt: url)
        #expect(allocated != taken)
    }

    @Test("a corrupt or privileged remembered value is ignored")
    func ignoresBadValues() throws {
        // A truncated write, or a hand-edited file. Binding below 1024 needs root, and 0
        // means "pick one" -- neither is a value this code ever wrote.
        for bad in ["", "not a port", "0", "80", "99999999"] {
            let url = memoryURL()
            defer { try? FileManager.default.removeItem(at: url) }
            try bad.write(to: url, atomically: true, encoding: .utf8)
            let port = try PortAllocator.preferredLoopbackPort(rememberedAt: url)
            #expect(port > 1024, "accepted \(bad.isEmpty ? "<empty>" : bad)")
        }
    }
}
