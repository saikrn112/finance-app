import AppKit
import Combine
import FinanceCore
import SwiftUI

/// Swaps between the backend status view and the webview as the backend comes and goes.
///
/// The status view is not a splash screen that gets dismissed once: a backend that
/// crashes an hour in has to take the window back, because a webview pointed at a dead
/// port shows a WebKit error page that says nothing useful about what actually happened.
@MainActor
final class RootViewController: NSViewController {
    private let supervisor: BackendSupervisor
    private let layout: BundleLayout

    private var currentChild: NSViewController?
    private var webController: AppWebViewController?
    /// The endpoint the current webview was built for. The port and token both change
    /// on every backend restart, so a new endpoint means a new webview rather than a
    /// reload -- the old one's cookie is for a token that no longer exists.
    private var shownEndpoint: BackendEndpoint?
    private var stateObserver: AnyCancellable?

    private let log: ShellLog

    init(supervisor: BackendSupervisor, layout: BundleLayout, log: ShellLog) {
        self.supervisor = supervisor
        self.layout = layout
        self.log = log
        super.init(nibName: nil, bundle: nil)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("not supported") }

    override func loadView() {
        view = NSView(frame: NSRect(x: 0, y: 0, width: 1160, height: 780))
    }

    override func viewDidLoad() {
        super.viewDidLoad()
        apply(supervisor.state)
        stateObserver = supervisor.$state
            .receive(on: RunLoop.main)
            .sink { [weak self] state in self?.apply(state) }
    }

    /// The live webview, for the zoom and reload menu items. `nil` unless ready.
    var activeWebController: AppWebViewController? { webController }

    private func apply(_ state: BackendSupervisor.State) {
        if case .ready(let endpoint) = state {
            guard shownEndpoint != endpoint else { return }
            shownEndpoint = endpoint
            log.write("backend ready on port \(endpoint.port); showing the app")
            let controller = AppWebViewController(endpoint: endpoint, log: log)
            webController = controller
            show(controller)
            // Debug-only; see runDispatchTour.
            if let list = ProcessInfo.processInfo.environment["FINANCE_APP_DISPATCH"] {
                let interval = Double(
                    ProcessInfo.processInfo.environment["FINANCE_APP_DISPATCH_INTERVAL"] ?? ""
                ) ?? 3
                controller.runDispatchTour(
                    commands: list.split(separator: ",").map {
                        $0.trimmingCharacters(in: .whitespaces)
                    },
                    interval: interval
                )
            }
        } else {
            guard shownEndpoint != nil || currentChild == nil else { return }
            if shownEndpoint != nil {
                log.write("backend no longer ready; showing the status view")
            }
            shownEndpoint = nil
            webController = nil
            show(
                NSHostingController(
                    rootView: BackendStatusView(supervisor: supervisor, layout: layout)
                )
            )
        }
    }

    private func show(_ controller: NSViewController) {
        if let currentChild {
            currentChild.view.removeFromSuperview()
            currentChild.removeFromParent()
        }
        addChild(controller)
        controller.view.frame = view.bounds
        controller.view.autoresizingMask = [.width, .height]
        view.addSubview(controller.view)
        currentChild = controller
    }
}
