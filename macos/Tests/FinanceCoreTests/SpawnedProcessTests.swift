import Darwin
import Foundation
import Testing

@testable import FinanceCore

@Suite("SpawnedProcess", .serialized)
struct SpawnedProcessTests {
    /// A throwaway file to collect the child's output.
    private func makeOutputFile() -> (URL, Int32) {
        let url = FileManager.default.temporaryDirectory
            .appending(path: "spawn-\(UUID().uuidString).log")
        let fd = open(url.path, O_WRONLY | O_CREAT | O_TRUNC, 0o600)
        return (url, fd)
    }

    @Test("stdout and stderr both reach the output descriptor")
    func capturesOutput() throws {
        let (url, fd) = makeOutputFile()
        defer {
            close(fd)
            try? FileManager.default.removeItem(at: url)
        }

        let process = try SpawnedProcess.spawn(
            executable: URL(filePath: "/bin/sh"),
            arguments: ["-c", "echo to-stdout; echo to-stderr 1>&2"],
            environment: ["PATH": "/usr/bin:/bin"],
            workingDirectory: nil,
            outputDescriptor: fd
        )
        #expect(process.wait() == 0)

        let contents = try String(contentsOf: url, encoding: .utf8)
        #expect(contents.contains("to-stdout"))
        #expect(contents.contains("to-stderr"))
    }

    @Test("the child is the leader of its own process group")
    func isGroupLeader() throws {
        let (url, fd) = makeOutputFile()
        defer {
            close(fd)
            try? FileManager.default.removeItem(at: url)
        }

        let process = try SpawnedProcess.spawn(
            executable: URL(filePath: "/bin/sh"),
            arguments: ["-c", "sleep 30"],
            environment: ["PATH": "/usr/bin:/bin"],
            workingDirectory: nil,
            outputDescriptor: fd
        )
        defer { process.terminateGroup(graceSeconds: 1) }

        // getpgid(pid) == pid is what makes `kill(-pid, ...)` reach the child and
        // everything it forks, and nothing else.
        #expect(getpgid(process.processIdentifier) == process.processIdentifier)
        #expect(process.processGroupIdentifier == process.processIdentifier)
        // ...and specifically NOT the test runner's group, which is the default
        // Foundation Process behaviour this type exists to avoid.
        #expect(process.processGroupIdentifier != getpgid(getpid()))
    }

    @Test("terminating the group also kills a grandchild")
    func killsGrandchild() throws {
        // This is the bug the repo already paid for once: `npx` forks a grandchild, so
        // killing the recorded pid leaves the real process running (AGENTS.md caveat
        // #10). A leaked uvicorn holding the SQLite file is the same failure.
        let (url, fd) = makeOutputFile()
        defer {
            close(fd)
            try? FileManager.default.removeItem(at: url)
        }

        // The shell backgrounds a long sleep, prints its pid, then waits on its own
        // sleep. The backgrounded one is the grandchild.
        let process = try SpawnedProcess.spawn(
            executable: URL(filePath: "/bin/sh"),
            arguments: ["-c", "sleep 60 & echo $!; sleep 60"],
            environment: ["PATH": "/usr/bin:/bin"],
            workingDirectory: nil,
            outputDescriptor: fd
        )

        var grandchild: pid_t = 0
        for _ in 0..<100 {
            if let text = try? String(contentsOf: url, encoding: .utf8),
               let first = text.split(separator: "\n").first,
               let parsed = pid_t(first.trimmingCharacters(in: .whitespaces))
            {
                grandchild = parsed
                break
            }
            usleep(50_000)
        }
        #expect(grandchild > 0, "the child never reported its grandchild's pid")
        #expect(kill(grandchild, 0) == 0, "the grandchild should be alive before the kill")

        process.terminateGroup(graceSeconds: 2)

        // Give the kernel a moment to deliver.
        var stillAlive = true
        for _ in 0..<40 {
            if kill(grandchild, 0) != 0 {
                stillAlive = false
                break
            }
            usleep(50_000)
        }
        #expect(stillAlive == false, "the grandchild survived terminateGroup")
    }

    @Test("nothing is inherited from the parent environment")
    func environmentIsNotInherited() throws {
        let (url, fd) = makeOutputFile()
        defer {
            close(fd)
            try? FileManager.default.removeItem(at: url)
        }

        setenv("FINANCE_TEST_LEAK", "leaked", 1)
        defer { unsetenv("FINANCE_TEST_LEAK") }

        let process = try SpawnedProcess.spawn(
            executable: URL(filePath: "/bin/sh"),
            arguments: ["-c", "echo \"leak=[${FINANCE_TEST_LEAK:-}]\"; echo \"kept=[$KEPT]\""],
            environment: ["PATH": "/usr/bin:/bin", "KEPT": "yes"],
            workingDirectory: nil,
            outputDescriptor: fd
        )
        #expect(process.wait() == 0)

        let contents = try String(contentsOf: url, encoding: .utf8)
        #expect(contents.contains("leak=[]"), "the parent's environment leaked into the child")
        #expect(contents.contains("kept=[yes]"))
    }

    @Test("no descriptor other than stdio and the log reaches the child")
    func descriptorsAreNotInherited() throws {
        // Half of the "already running" deadlock: posix_spawn inherits every open
        // descriptor unless told otherwise, so the shell's single-instance flock ended
        // up held by the backend. Defaulting to close-on-exec and opting in per
        // descriptor is the safe direction, and this is what proves it is on.
        let (url, fd) = makeOutputFile()
        defer {
            close(fd)
            try? FileManager.default.removeItem(at: url)
        }

        // An unrelated descriptor, exactly like the lock file, parked at a fixed high
        // number. A fixed number so the child can name it, and high so it cannot
        // collide with a descriptor the child's own `ls`/`test` happens to open --
        // an earlier version of this test probed `ls /dev/fd` and was confounded by
        // exactly that.
        //
        // dup2 clears FD_CLOEXEC on the new descriptor, which is deliberate: it means
        // this test measures POSIX_SPAWN_CLOEXEC_DEFAULT rather than re-testing the
        // O_CLOEXEC that SingleInstanceGuard sets.
        let extraURL = FileManager.default.temporaryDirectory
            .appending(path: "extra-\(UUID().uuidString)")
        let opened = open(extraURL.path, O_WRONLY | O_CREAT, 0o600)
        #expect(opened >= 0)
        let probeDescriptor: Int32 = 20
        #expect(dup2(opened, probeDescriptor) == probeDescriptor)
        defer {
            close(opened)
            close(probeDescriptor)
            try? FileManager.default.removeItem(at: extraURL)
        }

        let process = try SpawnedProcess.spawn(
            executable: URL(filePath: "/bin/sh"),
            arguments: [
                "-c",
                "if [ -e /dev/fd/\(probeDescriptor) ]; then echo LEAKED; else echo clean; fi; "
                    + "if [ -e /dev/fd/1 ]; then echo stdio-ok; fi",
            ],
            environment: ["PATH": "/usr/bin:/bin"],
            workingDirectory: nil,
            outputDescriptor: fd
        )
        #expect(process.wait() == 0)

        let output = try String(contentsOf: url, encoding: .utf8)
        #expect(
            output.contains("clean"),
            "descriptor \(probeDescriptor) leaked into the child: \(output)"
        )
        // stdio must still be there -- CLOEXEC_DEFAULT closing those too would be a
        // different bug with the same shape.
        #expect(output.contains("stdio-ok"))
    }

    @Test("a signalled death is reported distinctly from a nonzero exit")
    func signalledDeath() throws {
        let (url, fd) = makeOutputFile()
        defer {
            close(fd)
            try? FileManager.default.removeItem(at: url)
        }

        let process = try SpawnedProcess.spawn(
            executable: URL(filePath: "/bin/sh"),
            arguments: ["-c", "sleep 30"],
            environment: ["PATH": "/usr/bin:/bin"],
            workingDirectory: nil,
            outputDescriptor: fd
        )
        // Signalled from outside rather than `kill -TERM $$` inside the shell: sh's
        // own handling of a self-sent signal is shell-dependent and reported 0 here,
        // which would have tested the shell rather than the wait-status decoding.
        #expect(kill(process.processIdentifier, SIGKILL) == 0)
        // Negative means "died from signal N", so the supervisor can tell a crash
        // from a clean nonzero exit without a second field.
        #expect(process.wait() == -SIGKILL)
    }

    @Test("a nonzero exit status is reported as itself")
    func nonzeroExit() throws {
        let (url, fd) = makeOutputFile()
        defer {
            close(fd)
            try? FileManager.default.removeItem(at: url)
        }

        let process = try SpawnedProcess.spawn(
            executable: URL(filePath: "/bin/sh"),
            arguments: ["-c", "exit 42"],
            environment: ["PATH": "/usr/bin:/bin"],
            workingDirectory: nil,
            outputDescriptor: fd
        )
        #expect(process.wait() == 42)
    }

    @Test("a missing executable throws instead of spawning")
    func missingExecutable() {
        let (url, fd) = makeOutputFile()
        defer {
            close(fd)
            try? FileManager.default.removeItem(at: url)
        }

        #expect(throws: SpawnedProcess.Failure.self) {
            _ = try SpawnedProcess.spawn(
                executable: URL(filePath: "/nonexistent/python3"),
                arguments: [],
                environment: [:],
                workingDirectory: nil,
                outputDescriptor: fd
            )
        }
    }

    @Test("the working directory is applied")
    func workingDirectory() throws {
        let (url, fd) = makeOutputFile()
        defer {
            close(fd)
            try? FileManager.default.removeItem(at: url)
        }

        let directory = FileManager.default.temporaryDirectory
            .appending(path: "cwd-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }

        let process = try SpawnedProcess.spawn(
            executable: URL(filePath: "/bin/sh"),
            arguments: ["-c", "pwd"],
            environment: ["PATH": "/usr/bin:/bin"],
            workingDirectory: directory,
            outputDescriptor: fd
        )
        #expect(process.wait() == 0)

        let contents = try String(contentsOf: url, encoding: .utf8)
        // /var is a symlink to /private/var, so compare resolved paths.
        #expect(contents.contains(directory.resolvingSymlinksInPath().path))
    }

    @Test("reapIfExited does not block while the child is alive")
    func reapIsNonBlocking() throws {
        let (url, fd) = makeOutputFile()
        defer {
            close(fd)
            try? FileManager.default.removeItem(at: url)
        }

        let process = try SpawnedProcess.spawn(
            executable: URL(filePath: "/bin/sh"),
            arguments: ["-c", "sleep 30"],
            environment: ["PATH": "/usr/bin:/bin"],
            workingDirectory: nil,
            outputDescriptor: fd
        )
        defer { process.terminateGroup(graceSeconds: 1) }

        #expect(process.reapIfExited() == nil)
        #expect(process.isRunning)
    }
}
