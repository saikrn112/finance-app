import Darwin
import Foundation

/// A live, ready backend.
public struct BackendEndpoint: Sendable, Equatable {
    public let port: UInt16
    public let token: SessionToken

    public init(port: UInt16, token: SessionToken) {
        self.port = port
        self.token = token
    }

    public var baseURL: URL { URL(string: "http://127.0.0.1:\(port)/")! }
}

/// Owns the backend process for the lifetime of the app: start, health, crash
/// restart, clean shutdown.
///
/// The shell has exactly one job that the web app never had to do -- keep a child
/// process alive without lying about it. Every state here is one the UI can show, and
/// `.failed` carries a reason and a log path, because a spinner that lasts forever is
/// the failure mode the plan calls out by name (plan §2 step 4).
@MainActor
public final class BackendSupervisor: ObservableObject {
    public enum State: Sendable, Equatable {
        case idle
        case starting(attempt: Int)
        case ready(BackendEndpoint)
        case waitingToRestart(until: Date, reason: String)
        /// Terminal. Only reached for a definitively broken payload, or after the
        /// restart policy's failure budget is spent.
        case failed(reason: String)
        case stopped

        public var endpoint: BackendEndpoint? {
            if case .ready(let endpoint) = self { return endpoint }
            return nil
        }
    }

    @Published public private(set) var state: State = .idle
    /// Every state transition, newest last. Shown in the diagnostics panel; this is
    /// what makes "it came back on its own" observable rather than folklore.
    @Published public private(set) var transitions: [String] = []

    public let layout: BundleLayout
    public let environment: BackendEnvironment
    public let policy: RestartPolicy

    private let classifier = StartupFailureClassifier()
    private let healthTimeout: TimeInterval
    private var supervisionTask: Task<Void, Never>?
    private var child: SpawnedProcess?
    private var recentFailures: [Date] = []
    private var intentionalStop = false

    public init(
        layout: BundleLayout,
        environment: BackendEnvironment,
        policy: RestartPolicy = RestartPolicy(),
        healthTimeout: TimeInterval = 45
    ) {
        self.layout = layout
        self.environment = environment
        self.policy = policy
        self.healthTimeout = healthTimeout
    }

    // MARK: - Public control

    public func start() {
        guard supervisionTask == nil else { return }
        intentionalStop = false
        supervisionTask = Task { [weak self] in
            await self?.supervise()
        }
    }

    /// Stop the backend and the supervision loop. Safe to call twice.
    public func stop() {
        intentionalStop = true
        supervisionTask?.cancel()
        supervisionTask = nil
        terminateChild()
        RuntimeState.clear(at: layout.runtimeStateURL)
        // A recorded failure is more useful to the user than "stopped", so it wins.
        if case .failed = state {} else { transition(to: .stopped) }
    }

    /// Synchronous teardown for `applicationWillTerminate`, where there is no time
    /// for an async task to unwind.
    public func stopSynchronously() {
        intentionalStop = true
        supervisionTask?.cancel()
        supervisionTask = nil
        terminateChild()
        RuntimeState.clear(at: layout.runtimeStateURL)
    }

    /// Re-derive whether the backend is actually alive, and restart it if not.
    ///
    /// Called on wake and periodically, rather than trusting a timer: a `Timer` does
    /// not fire while the machine is asleep, so anything scheduled for "later" may
    /// simply never happen (plan §11.2). The sweep and the crash path share
    /// `RestartPolicy`, so they cannot form different opinions about the same failure.
    public func revalidate() {
        guard !intentionalStop else { return }
        // `.failed` is terminal on purpose: it means the payload is broken or the
        // failure budget is spent, and a sweep that quietly restarted from there
        // would turn a visible problem into an invisible restart loop. Recovering
        // needs `retryAfterFailure`.
        if case .failed = state { return }
        // The loop owns exit detection, so the sweep's only job is to notice that the
        // loop itself is gone -- cancelled by a suspend, or never started.
        if supervisionTask == nil { start() }
    }

    /// Retry after `.failed`. Clears the failure history: this is an explicit human
    /// decision, and inheriting a spent budget would make the button do nothing.
    public func retryAfterFailure() {
        guard case .failed = state else { return }
        recentFailures.removeAll()
        supervisionTask = nil
        start()
    }

    // MARK: - Supervision loop

    private func supervise() async {
        var attempt = 0
        while !Task.isCancelled {
            attempt += 1
            transition(to: .starting(attempt: attempt))

            let exit: BackendExit
            do {
                exit = try await runOnce()
            } catch let error as BackendStartError {
                exit = .payloadInvalid(reason: error.description)
            } catch {
                exit = .failedBeforeReady(code: -1)
            }

            if intentionalStop || Task.isCancelled { return }

            if policy.countsAsFailure(exit) { recentFailures.append(Date()) }
            let decision = policy.decide(
                after: exit, recentFailures: recentFailures.dropLast(), now: Date()
            )

            switch decision {
            case .stayStopped:
                transition(to: .stopped)
                return
            case .giveUp(let reason):
                transition(to: .failed(reason: reason + describe(exit)))
                return
            case .restart(let delay):
                let until = Date().addingTimeInterval(delay)
                transition(to: .waitingToRestart(until: until, reason: describe(exit)))
                try? await Task.sleep(for: .seconds(delay))
            }
        }
    }

    /// One attempt: allocate, spawn, wait for health, then watch until it exits.
    private func runOnce() async throws -> BackendExit {
        try validatePayload()
        try layout.createSupportDirectories()

        let port = try PortAllocator.preferredLoopbackPort(rememberedAt: layout.portMemoryURL)
        let token = SessionToken()

        let logFile = LogFile(url: layout.backendLogURL)
        let descriptor = try logFile.openForAppending()
        defer { close(descriptor) }

        let process = try SpawnedProcess.spawn(
            executable: layout.interpreterURL,
            arguments: ["-I", layout.bootstrapURL.path],
            environment: environment.variables(port: port, token: token),
            // bootstrap.py chdirs into the app directory itself; passing it here too
            // would be a second place to keep in sync.
            workingDirectory: nil,
            outputDescriptor: descriptor
        )
        child = process

        try? RuntimeState(
            processGroupIdentifier: process.processGroupIdentifier,
            startedAt: Date(),
            executablePath: layout.interpreterURL.resolvingSymlinksInPath().path
        ).write(to: layout.runtimeStateURL)

        let probe = HealthProbe(port: port, timeout: healthTimeout)
        let outcome = await probe.waitUntilReady { process.isRunning }

        switch outcome {
        case .ready:
            transition(to: .ready(BackendEndpoint(port: port, token: token)))
            let code = await waitForExit(process)
            if intentionalStop { return .stoppedIntentionally }
            return classifier.classify(
                exitCode: code, logTail: tail(of: layout.backendLogURL), wasReady: true
            )

        case .processExited:
            let code = process.reapIfExited() ?? -1
            return classifier.classify(
                exitCode: code, logTail: tail(of: layout.backendLogURL), wasReady: false
            )

        case .timedOut:
            // Alive but not answering. Kill it and treat it as a retryable failure --
            // "slow" and "wedged" are indistinguishable from out here, and neither
            // justifies declaring the payload broken.
            process.terminateGroup()
            return .failedBeforeReady(code: -1)
        }
    }

    /// Poll rather than use a `DispatchSource` process source, so the exit path is the
    /// same one `revalidate` uses and there is only one way to learn the child died.
    private func waitForExit(_ process: SpawnedProcess) async -> Int32 {
        while !Task.isCancelled {
            if let code = process.reapIfExited() { return code }
            try? await Task.sleep(for: .milliseconds(400))
        }
        return 0
    }

    private func terminateChild() {
        guard let child else { return }
        child.terminateGroup()
        self.child = nil
    }

    // MARK: - Payload validation

    private func validatePayload() throws {
        let fileManager = FileManager.default
        guard fileManager.isExecutableFile(atPath: layout.interpreterURL.path) else {
            throw BackendStartError.missingInterpreter(path: layout.interpreterURL.path)
        }
        guard fileManager.fileExists(atPath: layout.bootstrapURL.path) else {
            throw BackendStartError.missingBootstrap(path: layout.bootstrapURL.path)
        }
    }

    // MARK: - State bookkeeping

    private func transition(to newState: State) {
        state = newState
        transitions.append("\(Self.timestamp()) \(Self.describe(newState))")
        if transitions.count > 200 { transitions.removeFirst(transitions.count - 200) }
    }

    private func describe(_ exit: BackendExit) -> String {
        switch exit {
        case .crashedAfterReady(let code): " (backend exited with \(code) after being ready)"
        case .failedBeforeReady(let code): " (backend exited with \(code) before becoming ready)"
        case .portCollision: " (the allocated port was taken)"
        case .payloadInvalid(let reason): " — \(reason)"
        case .stoppedIntentionally: ""
        }
    }

    private static func describe(_ state: State) -> String {
        switch state {
        case .idle: "idle"
        case .starting(let attempt): "starting (attempt \(attempt))"
        case .ready(let endpoint): "ready on port \(endpoint.port), token \(endpoint.token.fingerprint)"
        case .waitingToRestart(let until, let reason):
            "restarting in \(String(format: "%.1f", until.timeIntervalSinceNow))s\(reason)"
        case .failed(let reason): "failed: \(reason)"
        case .stopped: "stopped"
        }
    }

    private static func timestamp() -> String {
        let formatter = DateFormatter()
        formatter.dateFormat = "HH:mm:ss"
        return formatter.string(from: Date())
    }
}

public enum BackendStartError: Error, CustomStringConvertible {
    case missingInterpreter(path: String)
    case missingBootstrap(path: String)

    public var description: String {
        switch self {
        case .missingInterpreter(let path):
            "the bundled Python interpreter is missing or not executable: \(path)"
        case .missingBootstrap(let path):
            "the backend entry point is missing: \(path)"
        }
    }
}
