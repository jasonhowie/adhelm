import ApprovalHostCore
@preconcurrency import Dispatch
import Foundation

/// One in-process admission slot shared by authenticated XPC approval and the
/// local key settings UI. It conveys no authorization and never queues work.
/// Release happens on a later main-queue turn, after modal cancellation work
/// already scheduled by the terminal flow. Invalidation is terminal.
final class InstalledApprovalHostInteractionGate: @unchecked Sendable {
  private let lock = NSLock()
  private var active = false
  private var invalidated = false
  private var occupant: UUID?
  private let identity = UUID()
  private let performRelease: (@escaping @Sendable () -> Void) -> Void

  init(
    performRelease: @escaping (@escaping @Sendable () -> Void) -> Void = {
      DispatchQueue.main.async(execute: $0)
    }
  ) {
    self.performRelease = performRelease
  }

  func activate() {
    lock.lock()
    if !invalidated { active = true }
    lock.unlock()
  }

  func invalidate() {
    lock.lock()
    invalidated = true
    active = false
    lock.unlock()
  }

  var isAvailable: Bool {
    lock.lock()
    defer { lock.unlock() }
    return active && !invalidated && occupant == nil
  }

  func acquire() throws -> InstalledApprovalHostInteractionLease {
    lock.lock()
    defer { lock.unlock() }
    guard active && !invalidated else { throw ApprovalHostError.hostNotInstalled }
    guard occupant == nil else { throw ApprovalHostError.requestAlreadyActive }
    let identifier = UUID()
    occupant = identifier
    return InstalledApprovalHostInteractionLease(gateIdentity: identity, identifier: identifier) {
      [self] in
      performRelease { [self] in release(identifier) }
    }
  }

  func requireLease(_ lease: InstalledApprovalHostInteractionLease) throws {
    lock.lock()
    defer { lock.unlock() }
    guard active, !invalidated, lease.gateIdentity == identity,
      lease.identifier == occupant, !lease.isFinished
    else { throw ApprovalHostError.hostNotInstalled }
  }

  private func release(_ identifier: UUID) {
    lock.lock()
    if occupant == identifier { occupant = nil }
    lock.unlock()
  }
}

/// A generation-bound, idempotent terminal handoff. A stale callback cannot
/// release a subsequent interaction, and dropping a handle releases its slot.
final class InstalledApprovalHostInteractionLease: @unchecked Sendable {
  private let lock = NSLock()
  private var release: (() -> Void)?
  fileprivate let gateIdentity: UUID
  fileprivate let identifier: UUID

  fileprivate init(gateIdentity: UUID, identifier: UUID, release: @escaping () -> Void) {
    self.gateIdentity = gateIdentity
    self.identifier = identifier
    self.release = release
  }

  fileprivate var isFinished: Bool {
    lock.lock()
    defer { lock.unlock() }
    return release == nil
  }

  func finish() {
    lock.lock()
    let release = release
    self.release = nil
    lock.unlock()
    release?()
  }

  deinit { finish() }
}
