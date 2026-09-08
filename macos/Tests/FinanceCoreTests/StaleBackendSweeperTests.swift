import Darwin
import Foundation
import Testing

@testable import FinanceCore

@Suite("RuntimeState")
struct RuntimeStateTests {
    @Test("round-trips through disk")
    func roundTrip() throws {
        let url = FileManager.default.temporaryDirectory
            .appending(path: "state-\(UUID().uuidString).json")
        defer { try? FileManager.default.removeItem(at: url) }

        let original = RuntimeState(
            processGroupIdentifier: 4242,
            startedAt: Date(timeIntervalSince1970: 1_800_000_000),
            executablePath: "/tmp/python3"
        )
        try original.write(to: url)
        #expect(RuntimeState.read(from: url) == original)
    }

    @Test("a missing or corrupt file reads as nil rather than throwing")
    func toleratesGarbage() throws {
        #expect(RuntimeState.read(from: URL(filePath: "/nonexistent/state.json")) == nil)

        let url = FileManager.default.temporaryDirectory
            .appending(path: "state-\(UUID().uuidString).json")
        defer { try? FileManager.default.removeItem(at: url) }
        try "not json".write(to: url, atomically: true, encoding: .utf8)
        // A truncated write from a crash must not stop the app launching.
        #expect(RuntimeState.read(from: url) == nil)
    }

    @Test("the state file is not world-readable")
    func permissions() throws {
        let url = FileManager.default.temporaryDirectory
            .appending(path: "state-\(UUID().uuidString).json")
        defer { try? FileManager.default.removeItem(at: url) }
        try RuntimeState(processGroupIdentifier: 1, startedAt: Date(), executablePath: "/x")
            .write(to: url)
        let mode = (try FileManager.default.attributesOfItem(atPath: url.path)[.posixPermissions]
            as? NSNumber)?.intValue ?? 0
        #expect(mode & 0o077 == 0)
    }
}

@Suite("StaleBackendSweeper", .serialized)
struct StaleBackendSweeperTests {
    private func stateURL() -> URL {
        FileManager.default.temporaryDirectory.appending(path: "sweep-\(UUID().uuidString).json")
    }

    @Test("no recorded state means nothing to do")
    func nothingRecorded() {
        let url = stateURL()
        #expect(
            StaleBackendSweeper.sweep(stateURL: url, expectedExecutable: "/tmp/python3")
                == .nothingRecorded
        )
    }

    @Test("a dead pid is reported as already gone and the state is cleared")
    func alreadyGone() throws {
        let url = stateURL()
        defer { try? FileManager.default.removeItem(at: url) }
        // A pid that cannot be running: 2 is launchd's neighbour on macOS and is not
        // ours, but a very high pid past the wrap limit is reliably absent.
        try RuntimeState(
            processGroupIdentifier: 900_000, startedAt: Date(), executablePath: "/tmp/python3"
        ).write(to: url)

        var terminated: [pid_t] = []
        let result = StaleBackendSweeper.sweep(
            stateURL: url,
            expectedExecutable: "/tmp/python3",
            runningExecutablePath: { _ in nil },
            terminate: { terminated.append($0) }
        )
        #expect(result == .alreadyGone)
        #expect(terminated.isEmpty)
        // Cleared, so a pid that later gets reused is never revisited.
        #expect(RuntimeState.read(from: url) == nil)
    }

    @Test("a live pid running something else is left completely alone")
    func pidReuseIsNotOurs() throws {
        // The failure this guards against is the worst a cleanup routine can have:
        // kill(-pid) on a stranger's process group. Pid reuse is ordinary on a
        // machine that has been up a while, so identity has to be confirmed, not
        // assumed.
        let url = stateURL()
        defer { try? FileManager.default.removeItem(at: url) }
        try RuntimeState(
            processGroupIdentifier: getpid(), startedAt: Date(), executablePath: "/tmp/python3"
        ).write(to: url)

        var terminated: [pid_t] = []
        let result = StaleBackendSweeper.sweep(
            stateURL: url,
            expectedExecutable: "/tmp/python3",
            runningExecutablePath: { _ in "/usr/bin/someone-elses-program" },
            terminate: { terminated.append($0) }
        )
        #expect(result == .notOurs(runningExecutable: "/usr/bin/someone-elses-program"))
        #expect(terminated.isEmpty, "signalled a process that was not ours")
    }

    @Test("a pid whose executable cannot be read is left alone")
    func unreadableExecutableIsLeftAlone() throws {
        let url = stateURL()
        defer { try? FileManager.default.removeItem(at: url) }
        try RuntimeState(
            processGroupIdentifier: getpid(), startedAt: Date(), executablePath: "/tmp/python3"
        ).write(to: url)

        var terminated: [pid_t] = []
        // proc_pidpath fails for processes we lack permission to inspect. "Unknown"
        // must mean "do nothing", not "assume it is ours".
        let result = StaleBackendSweeper.sweep(
            stateURL: url,
            expectedExecutable: "/tmp/python3",
            runningExecutablePath: { _ in nil },
            terminate: { terminated.append($0) }
        )
        #expect(result == .notOurs(runningExecutable: nil))
        #expect(terminated.isEmpty)
    }

    @Test("a live pid running our interpreter is terminated as a group")
    func terminatesOurs() throws {
        let url = stateURL()
        defer { try? FileManager.default.removeItem(at: url) }
        try RuntimeState(
            processGroupIdentifier: getpid(), startedAt: Date(), executablePath: "/tmp/python3"
        ).write(to: url)

        var terminated: [pid_t] = []
        let result = StaleBackendSweeper.sweep(
            stateURL: url,
            expectedExecutable: "/tmp/python3",
            runningExecutablePath: { _ in "/tmp/python3" },
            terminate: { terminated.append($0) }
        )
        #expect(result == .terminated(getpid()))
        #expect(terminated == [getpid()])
        #expect(RuntimeState.read(from: url) == nil)
    }

    @Test("pid 1 and below are never signalled")
    func refusesPrivilegedPids() throws {
        let url = stateURL()
        defer { try? FileManager.default.removeItem(at: url) }
        // kill(-1, SIGTERM) signals every process the user may signal. A corrupt or
        // zeroed state file must not be able to ask for that.
        for pid in [pid_t(0), pid_t(1), pid_t(-1)] {
            try RuntimeState(
                processGroupIdentifier: pid, startedAt: Date(), executablePath: "/tmp/python3"
            ).write(to: url)
            var terminated: [pid_t] = []
            let result = StaleBackendSweeper.sweep(
                stateURL: url,
                expectedExecutable: "/tmp/python3",
                runningExecutablePath: { _ in "/tmp/python3" },
                terminate: { terminated.append($0) }
            )
            #expect(result == .alreadyGone, "pid \(pid) should be refused")
            #expect(terminated.isEmpty, "pid \(pid) was signalled")
        }
    }

    @Test("executablePath reports this process's own path")
    func executablePathWorks() {
        // Sanity-checks the proc_pidpath wrapper itself, since every "is it ours?"
        // decision rests on it.
        let path = StaleBackendSweeper.executablePath(forProcess: getpid())
        #expect(path != nil)
        #expect(path?.isEmpty == false)
        #expect(path?.hasPrefix("/") == true)
    }
}
