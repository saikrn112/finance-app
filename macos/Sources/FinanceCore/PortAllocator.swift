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
