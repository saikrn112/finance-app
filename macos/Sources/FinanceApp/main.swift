import AppKit

// A plain executable rather than an @main App: the app has to be able to refuse to
// launch (single-instance guard) before any window or SwiftUI scene exists, and it
// needs an NSApplication whose activation policy and delegate are set explicitly.

let application = NSApplication.shared
let delegate = AppDelegate()
application.delegate = delegate
application.setActivationPolicy(.regular)
application.run()
