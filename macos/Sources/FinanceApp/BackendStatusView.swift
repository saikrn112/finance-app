import FinanceCore
import SwiftUI

/// Phase 1's entire UI: what the backend is doing, and where to look when it isn't
/// doing it.
///
/// This is deliberately not a splash screen. Phase 2 puts the webview in front of it,
/// but the states stay reachable, because "starting", "restarting" and "failed" are
/// each things the user may need to see rather than things to hide behind a spinner.
struct BackendStatusView: View {
    @ObservedObject var supervisor: BackendSupervisor
    let layout: BundleLayout

    @State private var showDiagnostics = false

    var body: some View {
        // h-screen/flex-col rather than a fixed height: the transitions list can grow
        // and must scroll internally instead of pushing the buttons off-window.
        VStack(alignment: .leading, spacing: 0) {
            header
            Divider()
            detail
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
            Divider()
            footer
        }
        .frame(minWidth: 520, minHeight: 360)
    }

    // MARK: - Header

    private var header: some View {
        HStack(spacing: 12) {
            statusIndicator
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.headline)
                // Reserve the subtitle's line unconditionally. A subtitle that appears
                // only in some states adds a line and shoves everything below it down
                // (plan §11.4).
                Text(subtitle)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
            Spacer()
        }
        .padding(16)
    }

    private var statusIndicator: some View {
        Group {
            switch supervisor.state {
            case .ready:
                Image(systemName: "checkmark.circle.fill").foregroundStyle(.green)
            case .failed:
                Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.red)
            case .waitingToRestart:
                Image(systemName: "arrow.clockwise.circle.fill").foregroundStyle(.orange)
            case .stopped, .idle:
                Image(systemName: "pause.circle.fill").foregroundStyle(.secondary)
            case .starting:
                ProgressView().controlSize(.small)
            }
        }
        .font(.system(size: 22))
        .frame(width: 24, height: 24)
    }

    private var title: String {
        switch supervisor.state {
        case .idle: "Idle"
        case .starting: "Starting the backend…"
        case .ready: "Backend ready"
        case .waitingToRestart: "Backend restarting"
        case .failed: "Backend failed"
        case .stopped: "Backend stopped"
        }
    }

    private var subtitle: String {
        switch supervisor.state {
        case .idle: " "
        case .starting(let attempt):
            attempt == 1 ? "First attempt" : "Attempt \(attempt)"
        case .ready(let endpoint):
            // The port is not a secret; the token is, so only its fingerprint shows.
            "127.0.0.1:\(endpoint.port) · session \(endpoint.token.fingerprint)"
        case .waitingToRestart(let until, _):
            "Retrying in \(max(0, Int(until.timeIntervalSinceNow.rounded(.up))))s"
        case .failed(let reason): reason
        case .stopped: " "
        }
    }

    // MARK: - Detail

    private var detail: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                if case .failed(let reason) = supervisor.state {
                    failureBox(reason)
                }
                pathRows
                if showDiagnostics { transitionsList }
            }
            .padding(16)
        }
        // A scrollable pane that hits its end otherwise passes the scroll to whatever
        // is behind it.
        .scrollBounceBehavior(.basedOnSize)
    }

    private func failureBox(_ reason: String) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("What went wrong").font(.subheadline.weight(.semibold))
            Text(reason)
                .font(.system(.callout, design: .monospaced))
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color(nsColor: .controlBackgroundColor), in: .rect(cornerRadius: 8))
    }

    private var pathRows: some View {
        VStack(alignment: .leading, spacing: 8) {
            pathRow("Database", layout.databaseURL)
            pathRow("Backend log", layout.backendLogURL)
            pathRow("Settings file", layout.configURL)
            pluginRow
        }
    }

    /// The private plugin repository, and why it is not being used if it is not.
    ///
    /// Shown here because "my statement will not parse" and "the uncategorised review returns an
    /// error" are both symptoms of this one setting, and neither points at it.
    private var pluginRow: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text("Plugins")
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
                .frame(width: 92, alignment: .leading)
            switch PluginDirectory.status() {
            case .notConfigured:
                Text("Not set — only the bundled templates are loaded")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            case .ready(let url):
                Text(url.path)
                    .font(.system(.caption, design: .monospaced))
                    .textSelection(.enabled)
                    .lineLimit(2)
                    .truncationMode(.middle)
            case .unusable(let url, let problem):
                VStack(alignment: .leading, spacing: 2) {
                    Text(url.path)
                        .font(.system(.caption, design: .monospaced))
                        .lineLimit(1)
                        .truncationMode(.middle)
                    Text(problem.explanation)
                        .font(.caption2)
                        .foregroundStyle(.red)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            Spacer(minLength: 4)
            Button("Choose…") {
                NSApp.sendAction(#selector(AppDelegate.choosePluginsFolder(_:)), to: nil, from: nil)
            }
            .buttonStyle(.link)
            .font(.caption)
        }
    }

    private func pathRow(_ label: String, _ url: URL) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text(label)
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
                .frame(width: 92, alignment: .leading)
            Text(url.path)
                .font(.system(.caption, design: .monospaced))
                .textSelection(.enabled)
                .lineLimit(2)
                .truncationMode(.middle)
            Spacer(minLength: 4)
            Button("Reveal") {
                NSWorkspace.shared.selectFile(
                    url.path, inFileViewerRootedAtPath: url.deletingLastPathComponent().path
                )
            }
            .buttonStyle(.link)
            .font(.caption)
        }
    }

    private var transitionsList: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("State history").font(.subheadline.weight(.semibold))
            ForEach(Array(supervisor.transitions.enumerated().reversed()), id: \.offset) { entry in
                Text(entry.element)
                    .font(.system(.caption2, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    // MARK: - Footer

    private var footer: some View {
        HStack(spacing: 10) {
            Toggle("Diagnostics", isOn: $showDiagnostics)
                .toggleStyle(.switch)
                .controlSize(.small)
            Spacer()
            if case .failed = supervisor.state {
                Button("Try Again") { supervisor.retryAfterFailure() }
                    .keyboardShortcut(.defaultAction)
            }
            Button("Open Log") {
                NSWorkspace.shared.open(layout.backendLogURL)
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 12)
    }
}
