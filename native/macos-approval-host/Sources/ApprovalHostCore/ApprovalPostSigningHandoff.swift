import Foundation

/// A package-only consumer for the one narrow path that may examine a signed
/// receipt before the engine invalidates its fresh authenticated session. The
/// raw receipt API never constructs this collaborator. It has no provider,
/// ledger, XPC, serialization, or generic signing surface.
package protocol ApprovalPostSigningConsumer: AnyObject {
  func consume(
    signedReceipt: SignedApprovalReceipt,
    expectedChallenge: ApprovalChallenge,
    sessionLease: ApprovalPostSigningSessionLease
  ) -> Result<NativeApprovalBridgeCandidateV1, ApprovalHostError>
}

/// The separate package-only M46 consumer surface for one exact Meta campaign
/// PAUSE V2 request. It deliberately does not share the V1 consumer result:
/// V1 bytes and routes remain closed to OpenAI PAUSE and GA4 CREATE, while a
/// later Meta-specific route must opt in explicitly.
///
/// This protocol is not an XPC contract and has no generic receipt, grant,
/// provider, transport, or serialization surface. The engine retains the
/// fresh session only for this synchronous callback, then invalidates it.
package protocol NativeMetaCampaignPauseApprovalBridgePostSigningConsumer: AnyObject {
  func consume(
    signedReceipt: SignedApprovalReceipt,
    expectedChallenge: ApprovalChallenge,
    sessionLease: ApprovalPostSigningSessionLease
  ) -> Result<NativeMetaCampaignPauseApprovalBridgeCandidateV2, ApprovalHostError>
}

/// Callback-scoped access to the session that completed fresh authentication.
/// The engine creates this only after it has finalized one typed receipt, and
/// invalidates the session only after the synchronous consumer returns. The
/// lease is neither Codable nor an XPC object and exposes no session getter.
///
/// Its continuation gate closes immediately when cancellation, expiry, or a
/// terminal request result wins. A consumer must keep all protected work in
/// `withAuthenticatedSession`; retaining the lease after the callback, using
/// it on another thread, or consuming it twice fails closed.
package final class ApprovalPostSigningSessionLease: @unchecked Sendable {
  private let session: any AuthenticatedApprovalSession
  private let requestMayContinue: () -> Bool
  private let ownerThreadIdentifier: ObjectIdentifier
  private let stateLock = NSLock()
  private var active = true
  private var used = false

  init(
    session: any AuthenticatedApprovalSession,
    requestMayContinue: @escaping () -> Bool
  ) {
    self.session = session
    self.requestMayContinue = requestMayContinue
    ownerThreadIdentifier = ObjectIdentifier(Thread.current)
  }

  /// Gives a consumer the opaque session for one synchronous protected
  /// handoff. The consumer reads `shouldContinue()` at each protected
  /// boundary, so a cancellation that arrives while it runs closes its next
  /// boundary before it can create a candidate.
  package func withAuthenticatedSession<T>(
    _ operation: (any AuthenticatedApprovalSession) throws -> T
  ) throws -> T {
    stateLock.lock()
    guard active, !used, ObjectIdentifier(Thread.current) == ownerThreadIdentifier else {
      stateLock.unlock()
      throw ApprovalHostError.authenticationCancelled
    }
    used = true
    stateLock.unlock()

    // `requestMayContinue` takes the owning request's phase lock. Never call
    // it while holding `stateLock`: terminal finish takes the phase lock and
    // then closes this lease. The one-use claim above is still atomic; a
    // terminal result that wins after this check closes `shouldContinue()`,
    // which the protected consumer must recheck at every store boundary.
    guard requestMayContinue() else {
      throw ApprovalHostError.authenticationCancelled
    }
    return try operation(session)
  }

  package func shouldContinue() -> Bool {
    stateLock.lock()
    let usable = active
    stateLock.unlock()
    return usable && requestMayContinue()
  }

  var wasUsed: Bool {
    stateLock.lock()
    defer { stateLock.unlock() }
    return used
  }

  /// Closing the gate does not invalidate the session directly. The owning
  /// request does that only after its synchronous consumer returns, avoiding a
  /// concurrent LAContext invalidation while M33 is at a protected boundary.
  func close() {
    stateLock.lock()
    active = false
    stateLock.unlock()
  }
}
