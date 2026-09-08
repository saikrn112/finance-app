import Darwin
import Foundation

/// Asks the kernel for a free loopback port by binding to port 0 and reading back
/// what it chose.
///
/// This is inherently a race: the port is free when we close the socket, not when
/// the backend binds it. That is acceptable and the alternative is worse -- handing
/// the child a fixed port means a second app, a leaked backend, or an unrelated dev
/// server silently wins. `BackendSupervisor` treats "address already in use" as a
/// retryable start failure, which closes the race in the only place it can be
/// closed.
public enum PortAllocator {
    public enum Failure: Error, CustomStringConvertible {
        case socketUnavailable(errno: Int32)
        case bindFailed(errno: Int32)
        case nameLookupFailed(errno: Int32)

        public var description: String {
            switch self {
            case .socketUnavailable(let code): "socket() failed: \(String(cString: strerror(code)))"
            case .bindFailed(let code): "bind() failed: \(String(cString: strerror(code)))"
            case .nameLookupFailed(let code): "getsockname() failed: \(String(cString: strerror(code)))"
            }
        }
    }

    /// The same port as last time if it is still free, otherwise a new one.
    ///
    /// A random port per launch changes the page's **origin**, and an origin change means a
    /// fresh `localStorage`. That is not cosmetic: the frontend keeps its theme and its
    /// "getting started tour finished" flag there, so a new port every launch reopened the
    /// twelve-step tour every launch, forever.
    ///
    /// Preferring a remembered port keeps one origin across launches. Falling back to a
    /// fresh one keeps the collision-safety that picking a free port was for -- losing
    /// `localStorage` for one launch is a far better failure than refusing to start.
    ///
    /// - Parameter memoryURL: a small file holding the last port used.
    public static func preferredLoopbackPort(rememberedAt memoryURL: URL) throws -> UInt16 {
        if let remembered = readRememberedPort(at: memoryURL), isBindable(remembered) {
            return remembered
        }
        let fresh = try freeLoopbackPort()
        try? String(fresh).write(to: memoryURL, atomically: true, encoding: .utf8)
        return fresh
    }

    private static func readRememberedPort(at url: URL) -> UInt16? {
        guard
            let text = try? String(contentsOf: url, encoding: .utf8),
            let value = UInt16(text.trimmingCharacters(in: .whitespacesAndNewlines)),
            // Below 1024 needs root, and 0 means "pick one" -- neither is a value we wrote.
            value >= 1024
        else { return nil }
        return value
    }

    /// Whether we can bind `port` on loopback right now.
    static func isBindable(_ port: UInt16) -> Bool {
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { return false }
        defer { close(fd) }

        // SO_REUSEADDR, because this probe has to match what the *server* can do, not what
        // a fresh socket can. After the app quits, the webview's closed connections sit in
        // TIME_WAIT for a couple of minutes, and binding their port without SO_REUSEADDR
        // fails -- while uvicorn, which sets it, binds happily. Without this the probe
        // rejected the remembered port on every quick relaunch and the whole
        // stable-origin scheme silently did nothing: measured 53698 then 53771.
        var reuse: Int32 = 1
        setsockopt(
            fd, SOL_SOCKET, SO_REUSEADDR, &reuse, socklen_t(MemoryLayout<Int32>.size)
        )

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
        return result == 0
    }

    /// A port the kernel reported as free on 127.0.0.1.
    public static func freeLoopbackPort() throws -> UInt16 {
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { throw Failure.socketUnavailable(errno: errno) }
        defer { close(fd) }

        var address = sockaddr_in()
        address.sin_family = sa_family_t(AF_INET)
        address.sin_port = 0  // let the kernel pick
        address.sin_addr.s_addr = INADDR_LOOPBACK.bigEndian
        address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)

        let bindResult = withUnsafePointer(to: &address) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.bind(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        guard bindResult == 0 else { throw Failure.bindFailed(errno: errno) }

        var bound = sockaddr_in()
        var length = socklen_t(MemoryLayout<sockaddr_in>.size)
        let nameResult = withUnsafeMutablePointer(to: &bound) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                getsockname(fd, $0, &length)
            }
        }
        guard nameResult == 0 else { throw Failure.nameLookupFailed(errno: errno) }

        return UInt16(bigEndian: bound.sin_port)
    }
}
