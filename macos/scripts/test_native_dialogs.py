#!/usr/bin/env python3
"""Exercise the real WKUIDelegate against generated HTML, without a finance backend."""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile

parser = argparse.ArgumentParser()
parser.add_argument('--output', type=Path)
args = parser.parse_args()
source = Path(__file__).resolve().parents[1] / 'Sources/FinanceApp/AppWebView.swift'
delegate = source.read_text().split('@MainActor\nfinal class ExternalNavigationHandler', 1)[1]
with tempfile.TemporaryDirectory(prefix='finance-dialog-tests-') as scratch:
    root = Path(scratch)
    harness = '''import AppKit
import WebKit
final class ShellLog {
    func write(_ value: String) {}
    static func safeOrigin(_ url: URL?) -> String { url?.scheme ?? "nil" }
}
enum OAuthBridge {
    enum Provider { case synthetic }
    static func provider(for url: URL) -> Provider? { nil }
}
@MainActor
final class ExternalNavigationHandler''' + delegate + '''
@MainActor
func runTests() {
    let app = NSApplication.shared
    app.setActivationPolicy(.prohibited)
    let config = WKWebViewConfiguration()
    config.websiteDataStore = .nonPersistent()
    let web = WKWebView(frame: NSRect(x:0,y:0,width:800,height:600), configuration: config)
    let window = NSWindow(contentRect:web.frame,styleMask:[.borderless],backing:.buffered,defer:false)
    window.contentView = web
    let handler = ExternalNavigationHandler(allowedPort:65530, log:ShellLog())
    web.uiDelegate = handler
    web.navigationDelegate = handler
    let scripts = ["confirm('Synthetic cancel')", "confirm('Synthetic continue')",
                   "alert('Synthetic alert'); true", "prompt('Synthetic prompt','synthetic text')"]
    let expected = ["false", "true", "true", "synthetic text"]
    let responses: [NSApplication.ModalResponse] = [.alertFirstButtonReturn, .alertSecondButtonReturn,
                                                   .alertFirstButtonReturn, .alertSecondButtonReturn]
    var index = 0
    var sheetCount = 0
    var completed = 0
    func next() {
        guard index < scripts.count else {
            print("PASS: cancel, continue, alert, prompt; callbacks=\\(completed), sheets=\\(sheetCount)")
            guard completed == 4 && sheetCount == 4 else { exit(2) }
            exit(0)
        }
        web.evaluateJavaScript(scripts[index]) { value, error in
            guard error == nil else { print("FAIL:",error!);exit(2) }
            let result: String
            if let text = value as? String { result = text }
            else if let boolean = value as? NSNumber { result = boolean.boolValue ? "true" : "false" }
            else { result = "nil" }
            guard result == expected[index] else { print("FAIL: unexpected result", result);exit(2) }
            completed += 1
            index += 1
            next()
        }
    }
    handler.onLoadFinished = { next() }
    var handled: NSWindow?
    let timer = Timer(timeInterval:0.05,repeats:true) { _ in
        MainActor.assumeIsolated {
            if let sheet = window.attachedSheet {
                if sheet !== handled && index < responses.count {
                    handled = sheet
                    sheetCount += 1
                    window.endSheet(sheet, returnCode: responses[index])
                }
            } else { handled = nil }
        }
    }
    RunLoop.main.add(timer, forMode:.common)
    Timer.scheduledTimer(withTimeInterval:15,repeats:false) { _ in print("FAIL: timeout");exit(3) }
    web.loadHTMLString("<!doctype html><html><body>Synthetic dialogs only</body></html>",baseURL:nil)
    withExtendedLifetime((web,window,handler,timer)) { app.run() }
}
MainActor.assumeIsolated { runTests() }
'''
    (root / 'main.swift').write_text(harness)
    subprocess.run(['swiftc', '-module-cache-path', str(root/'module-cache'), str(root/'main.swift'), '-o', str(root/'probe')], check=True, timeout=90)
    result = subprocess.run([str(root/'probe')], text=True, capture_output=True, timeout=30)
    print(result.stdout)
    if args.output:
        args.output.write_text(result.stdout + result.stderr)
    if result.returncode:
        print(result.stderr)
        raise SystemExit(result.returncode)
