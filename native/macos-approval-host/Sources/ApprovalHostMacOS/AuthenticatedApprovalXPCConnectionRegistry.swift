import Foundation

/// Owns accepted listener connections until they are invalidated or the host
/// shuts down. It is deliberately independent of NSXPC so XCTest can exercise
/// its lifecycle ordering without allocating a Mach listener or connection.
///
/// This is process-local cleanup only. It does not attest a peer, authorize a
/// request, or replace the listener's fixed code-signing and UID checks.
final class AuthenticatedApprovalXPCConnectionRegistry: @unchecked Sendable {
  typealias Identifier = UUID

  private struct Entry {
    let service: AnyObject
    let connection: AnyObject
    let cancelService: () -> Void
    let invalidateConnection: () -> Void
  }

  private let lock = NSLock()
  private var accepting = true
  private var entries: [Identifier: Entry] = [:]

  /// Retains one accepted service and connection only while the listener still
  /// admits work. A closed registry never admits a late connection.
  func admit(
    service: AnyObject,
    connection: AnyObject,
    cancelService: @escaping () -> Void,
    invalidateConnection: @escaping () -> Void
  ) -> Identifier? {
    lock.lock()
    defer { lock.unlock() }
    guard accepting else { return nil }
    let identifier = Identifier()
    entries[identifier] = Entry(
      service: service,
      connection: connection,
      cancelService: cancelService,
      invalidateConnection: invalidateConnection
    )
    return identifier
  }

  /// Removes one peer that invalidated itself. Its service cancellation is
  /// still owned by the connection's invalidation handler.
  func remove(_ identifier: Identifier) {
    lock.lock()
    entries.removeValue(forKey: identifier)
    lock.unlock()
  }

  /// Closes admission once, cancels all active service flows, then invalidates
  /// their connections outside the lock. The return value lets the listener
  /// avoid calling `NSXPCListener.invalidate()` repeatedly.
  @discardableResult
  func invalidate() -> Bool {
    lock.lock()
    guard accepting else {
      lock.unlock()
      return false
    }
    accepting = false
    let active = Array(entries.values)
    entries.removeAll(keepingCapacity: false)
    lock.unlock()

    for entry in active {
      entry.cancelService()
      entry.invalidateConnection()
    }
    return true
  }

  /// Confirms that an admitted connection still belongs to an open listener
  /// immediately before it is activated. If shutdown wins afterward, the
  /// registry's terminal connection invalidation closes that connection.
  func canActivate(_ identifier: Identifier) -> Bool {
    lock.lock()
    defer { lock.unlock() }
    return accepting && entries[identifier] != nil
  }

  var canAdmit: Bool {
    lock.lock()
    defer { lock.unlock() }
    return accepting
  }

  var canActivateListener: Bool { canAdmit }

  var acceptedConnectionCount: Int {
    lock.lock()
    defer { lock.unlock() }
    return entries.count
  }
}
