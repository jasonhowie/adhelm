import ApprovalHostCore
@preconcurrency import Dispatch
import Foundation
@preconcurrency import LocalAuthentication
@preconcurrency import Security

/// The only fixed-slot signing operations available to the native host.  This
/// protocol deliberately has no `SecKey` getter or arbitrary-byte signing
/// method.  A typed receipt payload is the sole signing input.
protocol ProtectedKeyStore: AnyObject, Sendable {
  /// The local enrollment controller invokes this before it displays any UI.
  /// Every coordinator operation verifies it again before it touches a
  /// protected item, so a caller cannot turn a previous UI check into a
  /// store operation for different code.
  func validateConfiguredHostCodeIdentity() throws

  /// Creates the one configured slot through PENDING -> ACTIVE.  A record
  /// that already exists, an orphaned key, or any ambiguous result fails
  /// closed; this protocol does not rotate, replace, delete, or retry.
  func enrollInitialPrivateKey(
    authenticatedSession: LocalAuthenticationApprovalSession,
    shouldContinue: () -> Bool
  ) throws -> InitialEnrollmentActive

  /// Writes REVOKED for the one ACTIVE record.  It intentionally does not
  /// delete a Keychain key: deletion has separate recovery semantics and is
  /// not safe to infer from a local request.
  func revokeInitialPrivateKey(
    authenticatedSession: LocalAuthenticationApprovalSession,
    shouldContinue: () -> Bool
  ) throws -> InitialEnrollmentRevoked

  /// Claims the one-use receipt permit while holding the fixed-slot
  /// coordinator lease, immediately before its first protected provenance or
  /// key operation.  A later failure remains consumed and non-retryable.
  func signApprovedReceiptPayload(
    _ payload: ReceiptSigningPayload,
    authenticatedSession: LocalAuthenticationApprovalSession,
    claimPermit: () -> Bool
  ) throws -> Data
}

enum ProtectedKeyStoreError: Error, Equatable, Sendable {
  case unavailable
  case alreadyEnrolled
  case recoveryRequired
  case revoked
  case replacementUnsupported
  case codeIdentityInvalid
  case permitNotClaimed
}

/// The coordinator receives an opaque fresh session rather than creating or
/// evaluating an LAContext itself.  The production adapters require the
/// concrete LocalAuthentication session; unit tests use fakes and never touch
/// LocalAuthentication or Keychain.
protocol EnrollmentProtectedSession: AnyObject {}
extension LocalAuthenticationApprovalSession: EnrollmentProtectedSession {}

/// A narrow, fakeable code-identity check.  Its production implementation uses
/// SecCodeCopySelf and the requirement compiled into TrustedHostConfiguration;
/// it never accepts a requirement from an approval request or enrollment UI.
protocol HostCodeIdentityValidator: AnyObject, Sendable {
  func validateCurrentHost(requirement: String) throws
}

/// A narrow, fakeable protected provenance store.  It is intentionally unable
/// to delete a record or enumerate arbitrary Keychain items.
protocol EnrollmentProvenanceStore: AnyObject, Sendable {
  func load(session: any EnrollmentProtectedSession) throws -> InitialEnrollmentProvenance?
  func addPending(_ pending: InitialEnrollmentProvenance, session: any EnrollmentProtectedSession)
    throws
  func replace(_ provenance: InitialEnrollmentProvenance, session: any EnrollmentProtectedSession)
    throws
}

/// A narrow, fakeable fixed-key adapter.  It never exposes a key handle to a
/// caller.  The production adapter reloads and compares the observed public
/// fingerprint again immediately before SecKeyCreateSignature.
protocol FixedSlotKeyOperations: AnyObject, Sendable {
  func existingPublicKeyFingerprint(session: any EnrollmentProtectedSession) throws -> String?
  func createAndObservePublicKeyFingerprint(session: any EnrollmentProtectedSession) throws
    -> String
  func observeExistingPublicKeyFingerprint(session: any EnrollmentProtectedSession) throws -> String
  func signExistingKey(
    session: any EnrollmentProtectedSession,
    expectedPublicKeyFingerprint: String,
    payload: Data
  ) throws -> Data
}

/// Internal-only extension used to bind a receipt verifier to the current
/// fixed-slot public key.  It does not expose a key handle or a caller-selected
/// verification key: the coordinator consumes these bytes immediately to build
/// a verifier under its fixed configuration and lease.
protocol CurrentReceiptVerificationKeyOperations: FixedSlotKeyOperations {
  func observeExistingPublicKeyX963(session: any EnrollmentProtectedSession) throws -> Data
}

protocol EnrollmentIdentifierGenerator: AnyObject, Sendable {
  func nextEnrollmentIdentifier() throws -> String
}

/// A selector for the one physical provenance/key slot.  The registry rejects
/// a second incompatible complete configuration for this same slot rather
/// than giving it a separate lock.  That preserves in-process linearization
/// even if product construction is accidentally attempted twice with stale
/// build constants.
private struct FixedSlotIdentity: Equatable {
  let applicationTag: Data
  let accessGroup: String?
  let provenanceService: String
  let provenanceAccount: String

  init?(_ configuration: TrustedHostConfiguration) {
    guard let enrollment = configuration.initialEnrollment else { return nil }
    applicationTag = configuration.signingKeyApplicationTag
    accessGroup = configuration.signingKeyAccessGroup
    provenanceService = enrollment.provenanceService
    provenanceAccount = enrollment.provenanceAccount
  }

  func overlaps(_ other: FixedSlotIdentity) -> Bool {
    // A nil access-group query is broader than an explicitly scoped query.
    // Logical host/key references and intended storage are not disjoint
    // physical selectors. Either shared protected item needs the same lease.
    let groupOverlaps =
      accessGroup == nil || other.accessGroup == nil
      || accessGroup == other.accessGroup
    return groupOverlaps
      && (applicationTag == other.applicationTag
        || (provenanceService == other.provenanceService
          && provenanceAccount == other.provenanceAccount))
  }
}

/// Shares a coordinator only for one exact reviewed configuration of one
/// fixed slot.  It is intentionally process-local; Keychain access-group
/// policy, not this lock, governs other processes.
final class InitialEnrollmentCoordinatorRegistry: @unchecked Sendable {
  private struct Entry {
    let identity: FixedSlotIdentity
    let configuration: TrustedHostConfiguration
    let coordinator: InitialEnrollmentCoordinator
  }

  private let lock = NSLock()
  private var entries: [Entry] = []

  func coordinator(
    for configuration: TrustedHostConfiguration,
    make: () -> InitialEnrollmentCoordinator
  ) -> InitialEnrollmentCoordinator? {
    guard let identity = FixedSlotIdentity(configuration) else { return nil }
    lock.lock()
    defer { lock.unlock() }
    if let existing = entries.first(where: { $0.identity.overlaps(identity) }) {
      // An independently constructed product configuration cannot use a
      // second lease for the same fixed Keychain/provenance slot.  Refuse
      // before any protected store operation instead.
      guard existing.configuration == configuration else { return nil }
      return existing.coordinator
    }
    let coordinator = make()
    entries.append(
      Entry(identity: identity, configuration: configuration, coordinator: coordinator))
    return coordinator
  }
}

private enum ProcessInitialEnrollmentCoordinatorRegistry {
  static let shared = InitialEnrollmentCoordinatorRegistry()
}

/// A short-lived view of the existing enrollment coordinator while that
/// coordinator's fixed-slot lock is held. It intentionally exposes only
/// receipt verification against the current ACTIVE key. It has no key getter,
/// selector, signing operation, persistence operation, or public-key resolver.
///
/// The creator invalidates this object as the enclosing lease exits, including
/// when the body throws. Holding the object after the callback therefore cannot
/// turn it into an unlocked protected-store operation.
final class InitialEnrollmentReceiptVerificationLease: @unchecked Sendable {
  private let coordinator: InitialEnrollmentCoordinator
  private let sessionIdentifier: ObjectIdentifier
  private let protectedSession: any EnrollmentProtectedSession
  private let shouldContinue: () -> Bool
  private let ownerThreadIdentifier: ObjectIdentifier
  private let stateLock = NSLock()
  private var active = true

  fileprivate init(
    coordinator: InitialEnrollmentCoordinator,
    session: any EnrollmentProtectedSession,
    shouldContinue: @escaping () -> Bool
  ) {
    self.coordinator = coordinator
    sessionIdentifier = ObjectIdentifier(session)
    protectedSession = session
    self.shouldContinue = shouldContinue
    ownerThreadIdentifier = ObjectIdentifier(Thread.current)
  }

  /// Verifies a receipt only while the original coordinator callback is live
  /// and only for the exact session captured at lease creation. A different
  /// session, a retained lease, or a cancelled callback fails before a
  /// protected operation.
  func verifyCurrentReceipt(
    _ receiptData: Data,
    expectedChallenge: ApprovalChallenge,
    session: any EnrollmentProtectedSession,
    now: Date
  ) throws -> ReceiptRedemptionCurrentReceipt {
    stateLock.lock()
    defer { stateLock.unlock() }
    guard active,
      ObjectIdentifier(Thread.current) == ownerThreadIdentifier,
      ObjectIdentifier(session) == sessionIdentifier,
      ObjectIdentifier(protectedSession) == sessionIdentifier,
      shouldContinue()
    else { throw ProtectedKeyStoreError.unavailable }
    return try coordinator.verifyCurrentReceiptAlreadyLeased(
      receiptData: receiptData,
      expectedChallenge: expectedChallenge,
      session: protectedSession,
      now: now,
      shouldContinue: shouldContinue
    )
  }

  fileprivate func invalidate() {
    stateLock.lock()
    active = false
    stateLock.unlock()
  }
}

/// One process owns the fixed slot.  `NSLock` defines in-process
/// linearization only; this does not claim cross-process exclusion.  Every
/// operation re-reads protected provenance and observable key facts while the
/// local lease is held, but another entitled process can still modify either
/// item after that read.  This coordinator does not claim an atomic
/// cross-process snapshot or a cross-process revocation barrier.
final class InitialEnrollmentCoordinator: @unchecked Sendable {
  private let configuration: TrustedHostConfiguration
  private let codeIdentityValidator: any HostCodeIdentityValidator
  private let provenanceStore: any EnrollmentProvenanceStore
  private let keyOperations: any FixedSlotKeyOperations
  private let identifierGenerator: any EnrollmentIdentifierGenerator
  private let timestampGenerator: () -> String
  private let clock: () -> Date
  private let lock = NSLock()
  // A separate admission flag turns accidental re-entry into a closed error
  // before it can recurse into the non-recursive fixed-slot lock. The lock
  // below remains the one shared enrollment/redemption serialization point.
  private let redemptionAdmissionLock = NSLock()
  private var redemptionLeaseActive = false

  init(
    configuration: TrustedHostConfiguration,
    codeIdentityValidator: any HostCodeIdentityValidator,
    provenanceStore: any EnrollmentProvenanceStore,
    keyOperations: any FixedSlotKeyOperations,
    identifierGenerator: any EnrollmentIdentifierGenerator,
    timestampGenerator: @escaping () -> String = InitialEnrollmentTimestamp.now,
    clock: @escaping () -> Date = { Date() }
  ) {
    self.configuration = configuration
    self.codeIdentityValidator = codeIdentityValidator
    self.provenanceStore = provenanceStore
    self.keyOperations = keyOperations
    self.identifierGenerator = identifierGenerator
    self.timestampGenerator = timestampGenerator
    self.clock = clock
  }

  func validateConfiguredHostCodeIdentity() throws {
    do {
      try codeIdentityValidator.validateCurrentHost(
        requirement: configuration.expectedHostCodeSigningRequirement
      )
    } catch {
      throw ProtectedKeyStoreError.codeIdentityInvalid
    }
  }

  /// Returns only whether this exact coordinator was created for the supplied
  /// reviewed configuration. It does not reveal selectors or make a caller
  /// chosen configuration usable for a protected operation.
  func hasExactConfiguration(_ expected: TrustedHostConfiguration) -> Bool {
    configuration == expected
  }

  /// Runs one receipt-redemption operation under the same non-recursive lock
  /// used by enrollment, revocation, and signing. The callback receives a
  /// short-lived verifier only; it cannot acquire a second coordinator lock or
  /// retain protected access after this method returns.
  func withReceiptRedemptionLease<T>(
    session: any EnrollmentProtectedSession,
    shouldContinue: @escaping () -> Bool = { true },
    _ operation: (
      InitialEnrollmentReceiptVerificationLease,
      ReceiptRedemptionGlobalSetupEvidence
    ) throws -> T
  ) throws -> T {
    redemptionAdmissionLock.lock()
    guard !redemptionLeaseActive else {
      redemptionAdmissionLock.unlock()
      throw ProtectedKeyStoreError.unavailable
    }
    redemptionLeaseActive = true
    redemptionAdmissionLock.unlock()
    defer {
      redemptionAdmissionLock.lock()
      redemptionLeaseActive = false
      redemptionAdmissionLock.unlock()
    }

    lock.lock()
    defer { lock.unlock() }
    guard shouldContinue() else { throw ProtectedKeyStoreError.unavailable }
    try validateConfiguredHostCodeIdentity()
    let setupEvidence = try requireCompletedRedemptionGlobalSetupUnderHeldLock(
      session: session,
      shouldContinue: shouldContinue
    )
    let lease = InitialEnrollmentReceiptVerificationLease(
      coordinator: self,
      session: session,
      shouldContinue: shouldContinue
    )
    defer { lease.invalidate() }
    return try operation(lease, setupEvidence)
  }

  /// Performs the one explicit schema-v2 setup sequence while holding the
  /// same fixed-slot lock as enrollment, signing, revocation, and redemption.
  /// Nothing calls this from a runtime, XPC, helper, CLI, or automatic setup
  /// path in this increment. In particular, an absent global slot is never
  /// initialized by receipt redemption itself.
  func initializeReceiptRedemptionGlobalState(
    store: any ReceiptRedemptionFixedSlotStore,
    session: any EnrollmentProtectedSession,
    shouldContinue: () -> Bool = { true }
  ) throws -> ReceiptRedemptionGlobalInitializationResult {
    // Share the redemption admission gate as well as the fixed-slot lock.
    // This turns an accidental call from a redemption callback into a closed
    // error before it can recurse into `lock` and deadlock.
    redemptionAdmissionLock.lock()
    guard !redemptionLeaseActive else {
      redemptionAdmissionLock.unlock()
      throw ProtectedKeyStoreError.unavailable
    }
    redemptionLeaseActive = true
    redemptionAdmissionLock.unlock()
    defer {
      redemptionAdmissionLock.lock()
      redemptionLeaseActive = false
      redemptionAdmissionLock.unlock()
    }
    lock.lock()
    defer { lock.unlock() }
    try validateConfiguredHostCodeIdentity()
    guard shouldContinue() else { throw ProtectedKeyStoreError.unavailable }
    let intent = try fixedIntent()
    guard let current = try loadProvenance(session: session), current.intent == intent else {
      throw ProtectedKeyStoreError.unavailable
    }
    guard case .activeV2(let initialActive) = current else {
      // A schema-v1 ACTIVE record is intentionally readable for legacy
      // signing/current verification, but it has no durable setup lineage.
      throw ProtectedKeyStoreError.recoveryRequired
    }

    switch initialActive.redemptionGlobalSetup {
    case .intentPending:
      // An intent might already have reached the independent global slot. Do
      // not infer whether it did, clear it, or start another attempt.
      throw ProtectedKeyStoreError.recoveryRequired

    case .complete:
      let evidence = try requireActiveV2UnderHeldLock(
        session: session,
        shouldContinue: shouldContinue
      )
      guard shouldContinue() else { throw ProtectedKeyStoreError.recoveryRequired }
      do {
        guard let global = try store.loadGlobalState(session: session) else {
          throw ProtectedKeyStoreError.recoveryRequired
        }
        _ = try ReceiptRedemptionGlobalInitialization.requirePresentCompleteGlobal(
          global,
          configuration: configuration,
          active: evidence.active,
          observedPublicKeyX963SHA256: evidence.observedPublicKeyX963SHA256
        )
        return .alreadyInitialized
      } catch let error as ProtectedKeyStoreError {
        throw error
      } catch {
        // COMPLETE plus a missing, malformed, reset, or changed global is a
        // durable recovery condition, never a fresh initialization state.
        throw ProtectedKeyStoreError.recoveryRequired
      }

    case .notStarted:
      // Before the durable INTENT_PENDING write, an existing item is already
      // ambiguous: it may be a previous setup or unrelated mutation. Do not
      // overwrite, adopt, delete, or reinterpret it.
      do {
        guard shouldContinue(), try store.loadGlobalState(session: session) == nil else {
          throw ProtectedKeyStoreError.recoveryRequired
        }
      } catch let error as ProtectedKeyStoreError {
        throw error
      } catch {
        throw ProtectedKeyStoreError.unavailable
      }

      let observedAt = try trustedNow(after: nil)
      // Re-load and re-observe immediately before the first V2 setup write.
      // A retained ACTIVE fingerprint alone cannot make a swapped or unusable
      // key eligible for durable setup evidence.
      let evidence = try requireActiveV2UnderHeldLock(
        session: session,
        shouldContinue: shouldContinue
      )
      guard case .notStarted = evidence.active.redemptionGlobalSetup else {
        throw ProtectedKeyStoreError.recoveryRequired
      }
      let genesis:
        (
          state: ReceiptRedemptionGlobalStateV1,
          canonicalBytes: Data,
          binding: InitialEnrollmentRedemptionGlobalSetupBinding
        )
      do {
        genesis = try ReceiptRedemptionGlobalInitialization.makeGenesis(
          configuration: configuration,
          active: evidence.active,
          observedAt: observedAt
        )
      } catch {
        throw ProtectedKeyStoreError.unavailable
      }
      let intentActive: InitialEnrollmentActiveV2
      do {
        intentActive = try InitialEnrollmentTransitions.beginRedemptionGlobalSetup(
          active: evidence.active,
          binding: genesis.binding
        )
      } catch {
        throw ProtectedKeyStoreError.unavailable
      }
      guard shouldContinue() else { throw ProtectedKeyStoreError.unavailable }

      // From this exact attempted INTENT_PENDING replacement onward, every
      // failure is ambiguous. The caller must inspect retained protected state
      // through a later recovery procedure; no retry or auto-completion exists.
      do {
        try provenanceStore.replace(.activeV2(intentActive), session: session)
        guard try loadProvenance(session: session) == .activeV2(intentActive) else {
          throw ProtectedKeyStoreError.recoveryRequired
        }
        guard shouldContinue() else { throw ProtectedKeyStoreError.recoveryRequired }

        try store.addInitialGlobalState(genesis.canonicalBytes, session: session)
        guard let persistedGlobal = try store.loadGlobalState(session: session) else {
          throw ProtectedKeyStoreError.recoveryRequired
        }
        try ReceiptRedemptionGlobalInitialization.requireExactGenesis(
          persistedGlobal,
          expectedState: genesis.state,
          expectedCanonicalBytes: genesis.canonicalBytes
        )
        guard shouldContinue() else { throw ProtectedKeyStoreError.recoveryRequired }

        let completeActive = try InitialEnrollmentTransitions.completeRedemptionGlobalSetup(
          active: intentActive,
          binding: genesis.binding
        )
        try provenanceStore.replace(.activeV2(completeActive), session: session)
        guard try loadProvenance(session: session) == .activeV2(completeActive) else {
          throw ProtectedKeyStoreError.recoveryRequired
        }
        return .initialized
      } catch {
        throw ProtectedKeyStoreError.recoveryRequired
      }
    }
  }

  func enroll(
    session: any EnrollmentProtectedSession,
    shouldContinue: () -> Bool = { true }
  ) throws -> InitialEnrollmentActive {
    lock.lock()
    defer { lock.unlock() }
    try validateConfiguredHostCodeIdentity()
    guard shouldContinue() else { throw ProtectedKeyStoreError.unavailable }
    let intent = try fixedIntent()
    guard shouldContinue() else { throw ProtectedKeyStoreError.unavailable }
    let current = try loadProvenance(session: session)
    if let current {
      guard current.intent == intent else { throw ProtectedKeyStoreError.unavailable }
      switch current {
      case .pending: throw ProtectedKeyStoreError.recoveryRequired
      case .active, .activeV2: throw ProtectedKeyStoreError.alreadyEnrolled
      case .revoked: throw ProtectedKeyStoreError.revoked
      }
    }
    // An unrecorded existing key is an ambiguous prior enrollment.  Never
    // adopt, overwrite, delete, or turn it into ACTIVE provenance.
    guard try keyOperations.existingPublicKeyFingerprint(session: session) == nil else {
      throw ProtectedKeyStoreError.recoveryRequired
    }
    guard shouldContinue() else { throw ProtectedKeyStoreError.unavailable }

    let identifier: String
    do {
      identifier = try identifierGenerator.nextEnrollmentIdentifier()
    } catch {
      throw ProtectedKeyStoreError.unavailable
    }
    let pending: InitialEnrollmentPending
    do {
      pending = try InitialEnrollmentPending(enrollmentID: identifier, intent: intent)
      // Identifier generation is deliberately outside the durable record
      // write.  Cancellation can arrive during that bounded local step, so
      // check again at the actual PENDING persistence handoff.
      guard shouldContinue() else { throw ProtectedKeyStoreError.unavailable }
      try provenanceStore.addPending(.pending(pending), session: session)
    } catch {
      throw ProtectedKeyStoreError.unavailable
    }

    // From this point a durable PENDING record exists.  Any error is an
    // indeterminate recovery state: do not retry creation, promote a key,
    // or delete either item automatically.
    do {
      guard shouldContinue() else { throw ProtectedKeyStoreError.recoveryRequired }
      let fingerprint = try keyOperations.createAndObservePublicKeyFingerprint(session: session)
      let active = try InitialEnrollmentTransitions.activateForRedemptionGlobalSetup(
        pending: pending,
        observedPublicKeyX963SHA256: fingerprint,
        createdAtText: timestampGenerator()
      )
      // A cancellation after creation cannot safely delete the key.  Preserve
      // PENDING rather than promoting it to ACTIVE after this checked
      // cancellation boundary.
      guard shouldContinue() else { throw ProtectedKeyStoreError.recoveryRequired }
      try provenanceStore.replace(.activeV2(active), session: session)
      return active.identity
    } catch {
      throw ProtectedKeyStoreError.recoveryRequired
    }
  }

  func revoke(
    session: any EnrollmentProtectedSession,
    shouldContinue: () -> Bool = { true }
  ) throws -> InitialEnrollmentRevoked {
    lock.lock()
    defer { lock.unlock() }
    try validateConfiguredHostCodeIdentity()
    guard shouldContinue() else { throw ProtectedKeyStoreError.unavailable }
    let intent = try fixedIntent()
    guard let current = try loadProvenance(session: session) else {
      throw ProtectedKeyStoreError.unavailable
    }
    guard current.intent == intent else { throw ProtectedKeyStoreError.unavailable }
    switch current {
    case .pending:
      throw ProtectedKeyStoreError.recoveryRequired
    case .revoked:
      throw ProtectedKeyStoreError.revoked
    case .active(let active):
      do {
        guard shouldContinue() else { throw ProtectedKeyStoreError.unavailable }
        let revoked = try InitialEnrollmentTransitions.revoke(
          active: active,
          revokedAtText: timestampGenerator()
        )
        // Timestamp generation is local but can still be delayed.  Recheck
        // cancellation at the REVOKED persistence handoff just as enrollment
        // does for PENDING and ACTIVE writes.
        guard shouldContinue() else { throw ProtectedKeyStoreError.unavailable }
        try provenanceStore.replace(.revoked(revoked), session: session)
        return revoked
      } catch {
        // Do not delete a key after an uncertain record update.  A
        // caller must treat the result as unavailable and recover
        // locally with a separately reviewed procedure.
        throw ProtectedKeyStoreError.unavailable
      }
    case .activeV2(let active):
      do {
        guard shouldContinue() else { throw ProtectedKeyStoreError.unavailable }
        let revoked = try InitialEnrollmentTransitions.revoke(
          active: active.identity,
          revokedAtText: timestampGenerator()
        )
        guard shouldContinue() else { throw ProtectedKeyStoreError.unavailable }
        try provenanceStore.replace(.revoked(revoked), session: session)
        return revoked
      } catch {
        throw ProtectedKeyStoreError.unavailable
      }
    }
  }

  func sign(
    payload: ReceiptSigningPayload,
    session: any EnrollmentProtectedSession,
    claimPermit: () -> Bool
  ) throws -> Data {
    lock.lock()
    defer { lock.unlock() }
    try validateConfiguredHostCodeIdentity()
    let intent = try fixedIntent()
    // This is deliberately after code-identity validation but before the
    // first protected record/key operation.  A claimed permit is consumed
    // even if the subsequent read, pin check, or signature is ambiguous.
    guard claimPermit() else { throw ProtectedKeyStoreError.permitNotClaimed }
    guard let current = try loadProvenance(session: session) else {
      throw ProtectedKeyStoreError.unavailable
    }
    guard current.intent == intent else { throw ProtectedKeyStoreError.unavailable }
    switch current {
    case .pending:
      throw ProtectedKeyStoreError.recoveryRequired
    case .revoked:
      throw ProtectedKeyStoreError.revoked
    case .active, .activeV2:
      break
    }
    let observed = try keyOperations.observeExistingPublicKeyFingerprint(session: session)
    let active: InitialEnrollmentActive
    do {
      active = try InitialEnrollmentTransitions.requireUsableActive(
        current,
        expectedIntent: intent,
        observedPublicKeyX963SHA256: observed
      )
    } catch let error as InitialEnrollmentProvenanceError {
      throw Self.protectedError(error)
    } catch {
      throw ProtectedKeyStoreError.unavailable
    }
    do {
      // The production key adapter reloads and compares this exact
      // fingerprint again before SecKeyCreateSignature.
      return try keyOperations.signExistingKey(
        session: session,
        expectedPublicKeyFingerprint: active.publicKeyX963SHA256,
        payload: payload.domainSeparatedBytes
      )
    } catch {
      throw ProtectedKeyStoreError.unavailable
    }
  }

  /// Verifies a typed receipt against the presently observed key only while
  /// this coordinator owns the fixed-slot lease.  It is an internal trust
  /// boundary, not a public key lookup or a generic verification endpoint.
  /// The caller supplies the already typed expected challenge and protected
  /// session; the concrete façade owns its one-use session lifecycle.
  /// Malformed receipt bytes are rejected before any protected store read.
  func verifyCurrentReceipt(
    receiptData: Data,
    expectedChallenge: ApprovalChallenge,
    session: any EnrollmentProtectedSession,
    shouldContinue: () -> Bool = { true }
  ) throws -> SignedApprovalReceipt {
    guard shouldContinue() else { throw ProtectedKeyStoreError.unavailable }
    let receiptSnapshot = try Self.boundedReceiptSnapshot(receiptData)
    let preflightNow = try trustedNow(after: nil)
    do {
      // Reject expiry and malformed/unbound canonical receipt candidates
      // before code-identity, provenance, or key reads.  This is pure input
      // validation only; full cryptographic verification happens below after
      // the current ACTIVE key is observed under the fixed-slot lease.
      try PinnedReceiptVerifier.preflight(
        receiptSnapshot,
        expectedChallenge: expectedChallenge,
        configuration: configuration,
        now: preflightNow
      )
    } catch {
      throw ProtectedKeyStoreError.unavailable
    }

    lock.lock()
    defer { lock.unlock() }
    var previous = preflightNow
    return try verifyCurrentReceiptUnderHeldLock(
      receiptSnapshot: receiptSnapshot,
      expectedChallenge: expectedChallenge,
      session: session,
      shouldContinue: shouldContinue,
      nextNow: {
        let sampled = try self.trustedNow(after: previous)
        previous = sampled
        return sampled
      }
    ).receipt
  }

  /// The already-held counterpart used only by the fixed redemption adapter.
  /// It takes a fresh coordinator-verified time supplied by M31's monotonic
  /// state machine, snapshots the receipt before parsing, and never acquires
  /// `lock`. Callers get an exact receipt/key pair only through the ephemeral
  /// `InitialEnrollmentReceiptVerificationLease` above.
  fileprivate func verifyCurrentReceiptAlreadyLeased(
    receiptData: Data,
    expectedChallenge: ApprovalChallenge,
    session: any EnrollmentProtectedSession,
    now: Date,
    shouldContinue: @escaping () -> Bool
  ) throws -> ReceiptRedemptionCurrentReceipt {
    guard now.timeIntervalSince1970.isFinite, shouldContinue() else {
      throw ProtectedKeyStoreError.unavailable
    }
    let receiptSnapshot = try Self.boundedReceiptSnapshot(receiptData)
    do {
      try PinnedReceiptVerifier.preflight(
        receiptSnapshot,
        expectedChallenge: expectedChallenge,
        configuration: configuration,
        now: now
      )
    } catch {
      throw ProtectedKeyStoreError.unavailable
    }
    var previous = now
    return try verifyCurrentReceiptUnderHeldLock(
      receiptSnapshot: receiptSnapshot,
      expectedChallenge: expectedChallenge,
      session: session,
      shouldContinue: shouldContinue,
      nextNow: {
        let sampled = try self.trustedNow(after: previous)
        previous = sampled
        return sampled
      }
    )
  }

  /// Shared verification body. Its caller already owns `lock`; this method
  /// deliberately never calls the lock-taking facade. The facade supplies
  /// repeated coordinator clock samples while the M31 adapter supplies its
  /// separately monotonic protected-state observation for every check.
  private func verifyCurrentReceiptUnderHeldLock(
    receiptSnapshot: Data,
    expectedChallenge: ApprovalChallenge,
    session: any EnrollmentProtectedSession,
    shouldContinue: () -> Bool,
    nextNow: () throws -> Date
  ) throws -> ReceiptRedemptionCurrentReceipt {
    func currentTime() throws -> Date {
      let sampled = try nextNow()
      guard sampled.timeIntervalSince1970.isFinite, sampled < expectedChallenge.expiresAt else {
        throw ProtectedKeyStoreError.unavailable
      }
      return sampled
    }

    guard shouldContinue() else { throw ProtectedKeyStoreError.unavailable }
    _ = try currentTime()
    try validateConfiguredHostCodeIdentity()
    let intent = try fixedIntent()
    guard let verificationKeys = keyOperations as? any CurrentReceiptVerificationKeyOperations,
      shouldContinue()
    else { throw ProtectedKeyStoreError.unavailable }

    _ = try currentTime()
    guard let current = try loadProvenance(session: session), current.intent == intent else {
      throw ProtectedKeyStoreError.unavailable
    }
    switch current {
    case .pending:
      throw ProtectedKeyStoreError.recoveryRequired
    case .revoked:
      throw ProtectedKeyStoreError.revoked
    case .active, .activeV2:
      break
    }
    guard shouldContinue() else { throw ProtectedKeyStoreError.unavailable }
    _ = try currentTime()
    let x963: Data
    do {
      x963 = try Self.boundedCurrentPublicKey(
        verificationKeys.observeExistingPublicKeyX963(session: session))
    } catch {
      throw ProtectedKeyStoreError.unavailable
    }
    let active: InitialEnrollmentActive
    do {
      active = try InitialEnrollmentTransitions.requireUsableActive(
        current,
        expectedIntent: intent,
        observedPublicKeyX963SHA256: CanonicalJSON.sha256Hex(x963)
      )
    } catch let error as InitialEnrollmentProvenanceError {
      throw Self.protectedError(error)
    } catch {
      throw ProtectedKeyStoreError.unavailable
    }
    guard active.publicKeyX963SHA256 == CanonicalJSON.sha256Hex(x963) else {
      throw ProtectedKeyStoreError.unavailable
    }

    do {
      let verifier = try PinnedReceiptVerifier(
        configuration: configuration,
        trustedPublicKeyX963: x963
      )
      guard shouldContinue() else { throw ProtectedKeyStoreError.unavailable }
      let verificationNow = try currentTime()
      let verified = try verifier.verify(
        receiptSnapshot,
        expectedChallenge: expectedChallenge,
        now: verificationNow
      )
      guard shouldContinue() else { throw ProtectedKeyStoreError.unavailable }
      let finalObservation = try currentTime()
      return try ReceiptRedemptionCurrentReceipt(
        receipt: verified,
        activePublicKeyX963: x963,
        currentVerificationObservedAt: finalObservation
      )
    } catch let error as ProtectedKeyStoreError {
      throw error
    } catch {
      // Receipt parsing, binding, signature, and time failures remain local
      // closed failures. Do not surface cryptographic/parser details through
      // the protected-key boundary.
      throw ProtectedKeyStoreError.unavailable
    }
  }

  private static func boundedReceiptSnapshot(_ receiptData: Data) throws -> Data {
    guard !receiptData.isEmpty, receiptData.count <= 16_384,
      let snapshot = receiptData.withUnsafeBytes({ bytes -> Data? in
        guard let baseAddress = bytes.baseAddress else { return nil }
        return Data(bytes: baseAddress, count: bytes.count)
      })
    else { throw ProtectedKeyStoreError.unavailable }
    return snapshot
  }

  private static func boundedCurrentPublicKey(_ value: Data) throws -> Data {
    guard value.count == 65, value.first == 0x04,
      let snapshot = value.withUnsafeBytes({ bytes -> Data? in
        guard let baseAddress = bytes.baseAddress else { return nil }
        return Data(bytes: baseAddress, count: bytes.count)
      })
    else { throw ProtectedKeyStoreError.unavailable }
    return snapshot
  }

  /// Revalidates the current durable V2 ACTIVE record and the currently
  /// observable fixed key while `lock` is already held. This is deliberately
  /// private: callers reach it only through the coordinator's setup or lease
  /// paths, never as a generic current-key resolver.
  private func requireActiveV2UnderHeldLock(
    session: any EnrollmentProtectedSession,
    shouldContinue: () -> Bool
  ) throws -> ReceiptRedemptionGlobalSetupEvidence {
    guard shouldContinue() else { throw ProtectedKeyStoreError.unavailable }
    let intent = try fixedIntent()
    guard let current = try loadProvenance(session: session), current.intent == intent,
      case .activeV2(let active) = current
    else { throw ProtectedKeyStoreError.recoveryRequired }
    guard shouldContinue() else { throw ProtectedKeyStoreError.unavailable }
    let observed: String
    do {
      observed = try keyOperations.observeExistingPublicKeyFingerprint(session: session)
      _ = try InitialEnrollmentTransitions.requireUsableActive(
        current,
        expectedIntent: intent,
        observedPublicKeyX963SHA256: observed
      )
    } catch let error as InitialEnrollmentProvenanceError {
      throw Self.protectedError(error)
    } catch {
      throw ProtectedKeyStoreError.unavailable
    }
    return ReceiptRedemptionGlobalSetupEvidence(
      active: active,
      observedPublicKeyX963SHA256: observed
    )
  }

  /// Admission for the receipt-redemption adapter. A legacy V1 ACTIVE record
  /// can still serve the pre-existing signing/current-verification behavior,
  /// but it cannot mint a bridge-capable fixed-slot lease.
  private func requireCompletedRedemptionGlobalSetupUnderHeldLock(
    session: any EnrollmentProtectedSession,
    shouldContinue: () -> Bool
  ) throws -> ReceiptRedemptionGlobalSetupEvidence {
    let evidence = try requireActiveV2UnderHeldLock(
      session: session,
      shouldContinue: shouldContinue
    )
    do {
      let derivation = try ReceiptRedemptionFixedSlotSelectorDerivation(
        configuration: configuration
      )
      _ = try InitialEnrollmentTransitions.requireCompletedRedemptionGlobalSetup(
        active: evidence.active,
        expectedSlotDigest: derivation.slotDigest,
        observedPublicKeyX963SHA256: evidence.observedPublicKeyX963SHA256
      )
      return evidence
    } catch let error as InitialEnrollmentProvenanceError {
      throw Self.protectedError(error)
    } catch {
      throw ProtectedKeyStoreError.unavailable
    }
  }

  private func fixedIntent() throws -> InitialEnrollmentIntent {
    guard configuration.initialEnrollment != nil else { throw ProtectedKeyStoreError.unavailable }
    do {
      return try InitialEnrollmentIntent(
        hostReference: configuration.hostReference,
        signingKeyReference: configuration.signingKeyReference,
        keyStorage: configuration.keyStorage
      )
    } catch {
      throw ProtectedKeyStoreError.unavailable
    }
  }

  private func trustedNow(after previous: Date?) throws -> Date {
    let sampled = clock()
    guard sampled.timeIntervalSince1970.isFinite,
      previous.map({ sampled >= $0 }) ?? true
    else { throw ProtectedKeyStoreError.unavailable }
    return sampled
  }

  private func loadProvenance(session: any EnrollmentProtectedSession) throws
    -> InitialEnrollmentProvenance?
  {
    do {
      return try provenanceStore.load(session: session)
    } catch {
      throw ProtectedKeyStoreError.unavailable
    }
  }

  private static func protectedError(_ error: InitialEnrollmentProvenanceError)
    -> ProtectedKeyStoreError
  {
    switch error {
    case .recoveryRequired: .recoveryRequired
    case .revoked: .revoked
    case .replacementUnsupported: .replacementUnsupported
    case .alreadyEnrolled, .configurationMismatch, .malformed: .unavailable
    }
  }
}

final class SecurityProtectedKeyStore: ProtectedKeyStore, ReceiptRedemptionGlobalSetupStore,
  @unchecked Sendable
{
  private let configuration: TrustedHostConfiguration
  private let coordinator: InitialEnrollmentCoordinator?

  init(configuration: TrustedHostConfiguration) {
    // The listener and the explicit enrollment controller can be constructed
    // independently by the installed product.  They must nevertheless share
    // the one lease for this complete fixed configuration, so sign/enroll/
    // revoke cannot overlap in one host process.
    self.configuration = configuration
    coordinator = ProcessInitialEnrollmentCoordinatorRegistry.shared.coordinator(
      for: configuration
    ) {
      InitialEnrollmentCoordinator(
        configuration: configuration,
        codeIdentityValidator: SecurityHostCodeIdentityValidator(),
        provenanceStore: SecurityEnrollmentProvenanceStore(configuration: configuration),
        keyOperations: SecurityFixedSlotKeyOperations(configuration: configuration),
        identifierGenerator: SecurityEnrollmentIdentifierGenerator()
      )
    }
  }

  /// Internal injection is intentionally available only to offline XCTest.
  /// Production construction above selects all fixed Security adapters.
  init(
    configuration: TrustedHostConfiguration,
    codeIdentityValidator: any HostCodeIdentityValidator,
    provenanceStore: any EnrollmentProvenanceStore,
    keyOperations: any FixedSlotKeyOperations,
    identifierGenerator: any EnrollmentIdentifierGenerator,
    timestampGenerator: @escaping () -> String,
    clock: @escaping () -> Date = { Date() }
  ) {
    self.configuration = configuration
    coordinator = InitialEnrollmentCoordinator(
      configuration: configuration,
      codeIdentityValidator: codeIdentityValidator,
      provenanceStore: provenanceStore,
      keyOperations: keyOperations,
      identifierGenerator: identifierGenerator,
      timestampGenerator: timestampGenerator,
      clock: clock
    )
  }

  func validateConfiguredHostCodeIdentity() throws {
    try requireCoordinator().validateConfiguredHostCodeIdentity()
  }

  func enrollInitialPrivateKey(
    authenticatedSession: LocalAuthenticationApprovalSession,
    shouldContinue: () -> Bool
  ) throws -> InitialEnrollmentActive {
    try requireCoordinator().enroll(session: authenticatedSession, shouldContinue: shouldContinue)
  }

  func revokeInitialPrivateKey(
    authenticatedSession: LocalAuthenticationApprovalSession,
    shouldContinue: () -> Bool
  ) throws -> InitialEnrollmentRevoked {
    try requireCoordinator().revoke(session: authenticatedSession, shouldContinue: shouldContinue)
  }

  func signApprovedReceiptPayload(
    _ payload: ReceiptSigningPayload,
    authenticatedSession: LocalAuthenticationApprovalSession,
    claimPermit: () -> Bool
  ) throws -> Data {
    try requireCoordinator().sign(
      payload: payload, session: authenticatedSession, claimPermit: claimPermit)
  }

  /// Internal installed-host seam only.  The public foundation offers no
  /// request-selected public-key lookup, and this method accepts a concrete
  /// LocalAuthentication session rather than constructing one itself.
  func verifyCurrentReceipt(
    _ receiptData: Data,
    expectedChallenge: ApprovalChallenge,
    authenticatedSession: LocalAuthenticationApprovalSession,
    shouldContinue: () -> Bool = { true }
  ) throws -> SignedApprovalReceipt {
    // This makes the concrete façade one-use even if a caller retained the
    // session.  It does not authenticate: an installed controller must pass
    // the session returned immediately by its fresh authenticator.
    defer { authenticatedSession.invalidate() }
    return try requireCoordinator().verifyCurrentReceipt(
      receiptData: receiptData,
      expectedChallenge: expectedChallenge,
      session: authenticatedSession,
      shouldContinue: shouldContinue
    )
  }

  /// Internal explicit setup seam. Its concrete entry point accepts only the
  /// fresh local-authentication session created by the status-menu controller.
  /// Construction derives only reviewed fixed selectors; it never initializes
  /// automatically during redemption.
  private func initializeReceiptRedemptionGlobalStateWithLocalSession(
    authenticatedSession: LocalAuthenticationApprovalSession,
    shouldContinue: () -> Bool
  ) throws -> ReceiptRedemptionGlobalInitializationResult {
    let store: SecurityReceiptRedemptionFixedSlotStore
    do {
      store = try SecurityReceiptRedemptionFixedSlotStore(configuration: configuration)
    } catch {
      throw ProtectedKeyStoreError.unavailable
    }
    return try requireCoordinator().initializeReceiptRedemptionGlobalState(
      store: store,
      session: authenticatedSession,
      shouldContinue: shouldContinue
    )
  }

  /// `ReceiptRedemptionGlobalSetupStore` deliberately has an erased session
  /// parameter so offline fakes can exercise the orchestration boundary. The
  /// installed Security store restores the concrete requirement immediately;
  /// no generic authenticated session can reach a protected operation.
  func initializeReceiptRedemptionGlobalState(
    authenticatedSession: any AuthenticatedApprovalSession,
    shouldContinue: @escaping () -> Bool
  ) throws -> ReceiptRedemptionGlobalInitializationResult {
    guard let localSession = authenticatedSession as? LocalAuthenticationApprovalSession else {
      authenticatedSession.invalidate()
      throw ProtectedKeyStoreError.unavailable
    }
    return try initializeReceiptRedemptionGlobalStateWithLocalSession(
      authenticatedSession: localSession,
      shouldContinue: shouldContinue
    )
  }

  /// Creates the real, statically bound M34 bridge consumer factory without
  /// touching a protected item. The factory retains this store's existing
  /// fixed-slot coordinator and derives only fixed Keychain selectors from the
  /// reviewed build metadata. Missing redemption state remains a later
  /// fail-closed result; this method never initializes, migrates, deletes, or
  /// recovers it.
  func makeNativeApprovalBridgePostSigningConsumerFactory(
    metadata: ApprovalHostBuildMetadataV1,
    operationGate: InstalledApprovalHostOperationGate
  ) throws -> NativeApprovalBridgePostSigningConsumerFactory {
    let configuration: TrustedHostConfiguration
    do {
      configuration = try metadata.makeTrustedHostConfiguration()
      return try makeNativeApprovalBridgePostSigningConsumerFactories(
        metadata: metadata,
        configuration: configuration,
        operationGate: operationGate,
        metaCampaignPauseRouteBinding: nil
      ).v1
    } catch {
      throw ProtectedKeyStoreError.unavailable
    }
  }

  /// Constructs the fixed V1 factory and, only for a separately generated
  /// exact V2 route binding, the Meta campaign PAUSE factory over one shared
  /// coordinator, fixed-slot adapter, and runtime operation gate. This is not
  /// a caller-selected capability: installed construction resolves the route
  /// binding from fixed generated source before calling this method.
  func makeNativeApprovalBridgePostSigningConsumerFactories(
    metadata: ApprovalHostBuildMetadataV1,
    configuration: TrustedHostConfiguration,
    operationGate: InstalledApprovalHostOperationGate,
    metaCampaignPauseRouteBinding: NativeMetaCampaignPauseApprovalRouteBindingV2?
  ) throws -> (
    v1: NativeApprovalBridgePostSigningConsumerFactory,
    metaCampaignPauseV2: NativeMetaCampaignPauseApprovalBridgePostSigningConsumerFactory?
  ) {
    let metadataConfiguration: TrustedHostConfiguration
    do {
      metadataConfiguration = try metadata.makeTrustedHostConfiguration()
    } catch {
      throw ProtectedKeyStoreError.unavailable
    }
    guard metadataConfiguration == configuration,
      configuration == self.configuration
    else {
      throw ProtectedKeyStoreError.unavailable
    }
    do {
      try metaCampaignPauseRouteBinding?.requireMatches(
        metadata: metadata,
        configuration: configuration
      )
    } catch {
      throw ProtectedKeyStoreError.unavailable
    }
    let coordinator = try requireCoordinator()
    guard coordinator.hasExactConfiguration(configuration) else {
      throw ProtectedKeyStoreError.unavailable
    }
    let fixedSlotStore: SecurityReceiptRedemptionFixedSlotStore
    do {
      fixedSlotStore = try SecurityReceiptRedemptionFixedSlotStore(configuration: configuration)
      let redemptionFactory = try SecurityNativeApprovalBridgeRedemptionFactory(
        configuration: configuration,
        enrollmentCoordinator: coordinator,
        fixedSlotStore: fixedSlotStore,
        operationGate: operationGate
      )
      let v1 = try NativeApprovalBridgePostSigningConsumerFactory(
        metadata: metadata,
        redemptionFactory: redemptionFactory
      )
      let metaCampaignPauseV2: NativeMetaCampaignPauseApprovalBridgePostSigningConsumerFactory?
      if metaCampaignPauseRouteBinding != nil {
        metaCampaignPauseV2 = try NativeMetaCampaignPauseApprovalBridgePostSigningConsumerFactory(
          metadata: metadata,
          redemptionFactory: redemptionFactory
        )
      } else {
        metaCampaignPauseV2 = nil
      }
      return (v1: v1, metaCampaignPauseV2: metaCampaignPauseV2)
    } catch {
      throw ProtectedKeyStoreError.unavailable
    }
  }

  private func requireCoordinator() throws -> InitialEnrollmentCoordinator {
    guard let coordinator else { throw ProtectedKeyStoreError.unavailable }
    return coordinator
  }
}

/// Production code-identity check.  The requirement is a reviewed build
/// constant from TrustedHostConfiguration, not data supplied by a challenge,
/// XPC caller, file, environment variable, or enrollment UI.
private final class SecurityHostCodeIdentityValidator: HostCodeIdentityValidator,
  @unchecked Sendable
{
  func validateCurrentHost(requirement: String) throws {
    var code: SecCode?
    guard SecCodeCopySelf([], &code) == errSecSuccess, let code else {
      throw ProtectedKeyStoreError.codeIdentityInvalid
    }
    var expected: SecRequirement?
    guard SecRequirementCreateWithString(requirement as CFString, [], &expected) == errSecSuccess,
      let expected,
      SecCodeCheckValidity(code, [], expected) == errSecSuccess
    else { throw ProtectedKeyStoreError.codeIdentityInvalid }
  }
}

/// The protected generic-password item that persists closed provenance.  This
/// adapter never enumerates or deletes items.  Its selector is fixed in the
/// installed build configuration and every query/update receives the same
/// already-authenticated LAContext with interaction disabled.
private final class SecurityEnrollmentProvenanceStore: EnrollmentProvenanceStore,
  @unchecked Sendable
{
  private let configuration: TrustedHostConfiguration

  init(configuration: TrustedHostConfiguration) {
    self.configuration = configuration
  }

  func load(session: any EnrollmentProtectedSession) throws -> InitialEnrollmentProvenance? {
    let context = try localContext(from: session)
    var query = try baseQuery()
    query[kSecReturnData] = true
    query[kSecMatchLimit] = kSecMatchLimitOne
    query[kSecUseAuthenticationContext] = context
    var result: CFTypeRef?
    let status = SecItemCopyMatching(query as CFDictionary, &result)
    if status == errSecItemNotFound { return nil }
    guard status == errSecSuccess, let data = result as? Data else {
      throw ProtectedKeyStoreError.unavailable
    }
    do {
      return try InitialEnrollmentProvenance.decode(data)
    } catch {
      throw ProtectedKeyStoreError.unavailable
    }
  }

  func addPending(_ pending: InitialEnrollmentProvenance, session: any EnrollmentProtectedSession)
    throws
  {
    guard case .pending = pending else { throw ProtectedKeyStoreError.unavailable }
    let context = try localContext(from: session)
    let bytes = try canonicalBytes(pending)
    var accessControlError: Unmanaged<CFError>?
    guard
      let accessControl = SecAccessControlCreateWithFlags(
        nil,
        kSecAttrAccessibleWhenUnlockedThisDeviceOnly,
        [.userPresence],
        &accessControlError
      )
    else { throw ProtectedKeyStoreError.unavailable }
    var attributes = try baseQuery()
    attributes[kSecValueData] = bytes
    attributes[kSecAttrAccessControl] = accessControl
    attributes[kSecUseAuthenticationContext] = context
    guard SecItemAdd(attributes as CFDictionary, nil) == errSecSuccess else {
      throw ProtectedKeyStoreError.unavailable
    }
  }

  func replace(_ provenance: InitialEnrollmentProvenance, session: any EnrollmentProtectedSession)
    throws
  {
    let context = try localContext(from: session)
    var query = try baseQuery()
    query[kSecUseAuthenticationContext] = context
    let update: [CFString: Any] = [kSecValueData: try canonicalBytes(provenance)]
    guard SecItemUpdate(query as CFDictionary, update as CFDictionary) == errSecSuccess else {
      throw ProtectedKeyStoreError.unavailable
    }
  }

  private func baseQuery() throws -> [CFString: Any] {
    guard let enrollment = configuration.initialEnrollment else {
      throw ProtectedKeyStoreError.unavailable
    }
    var query: [CFString: Any] = [
      kSecClass: kSecClassGenericPassword,
      kSecAttrService: enrollment.provenanceService,
      kSecAttrAccount: enrollment.provenanceAccount,
    ]
    if let accessGroup = configuration.signingKeyAccessGroup {
      query[kSecAttrAccessGroup] = accessGroup
    }
    return query
  }

  private func localContext(from session: any EnrollmentProtectedSession) throws -> LAContext {
    guard let local = session as? LocalAuthenticationApprovalSession else {
      throw ProtectedKeyStoreError.unavailable
    }
    // The UI/authentication transition has already occurred.  Keychain
    // must reuse exactly that fresh context or fail rather than launch an
    // unreviewed second prompt.
    local.context.interactionNotAllowed = true
    return local.context
  }

  private func canonicalBytes(_ provenance: InitialEnrollmentProvenance) throws -> Data {
    do {
      return try provenance.canonicalBytes()
    } catch {
      throw ProtectedKeyStoreError.unavailable
    }
  }
}

/// The fixed P-256 key adapter.  It uses only the configured selector and
/// storage policy.  Its public-key fingerprint is continuity evidence from a
/// previously audited creation transaction; it is not a replacement for later
/// opaque-ACL introspection, which public macOS APIs do not expose.
private final class SecurityFixedSlotKeyOperations: CurrentReceiptVerificationKeyOperations,
  @unchecked Sendable
{
  private let configuration: TrustedHostConfiguration

  init(configuration: TrustedHostConfiguration) {
    self.configuration = configuration
  }

  func existingPublicKeyFingerprint(session: any EnrollmentProtectedSession) throws -> String? {
    try loadExistingPrivateKey(session: session, allowNotFound: true)?.fingerprint
  }

  func createAndObservePublicKeyFingerprint(session: any EnrollmentProtectedSession) throws
    -> String
  {
    _ = try localContext(from: session)
    var accessControlError: Unmanaged<CFError>?
    guard
      let accessControl = SecAccessControlCreateWithFlags(
        nil,
        kSecAttrAccessibleWhenUnlockedThisDeviceOnly,
        [.privateKeyUsage, .userPresence],
        &accessControlError
      )
    else { throw ProtectedKeyStoreError.unavailable }

    var privateAttributes: [CFString: Any] = [
      kSecAttrIsPermanent: true,
      kSecAttrApplicationTag: configuration.signingKeyApplicationTag,
      kSecAttrAccessControl: accessControl,
      kSecAttrCanSign: true,
    ]
    if let accessGroup = configuration.signingKeyAccessGroup {
      privateAttributes[kSecAttrAccessGroup] = accessGroup
    }
    var parameters: [CFString: Any] = [
      kSecAttrKeyType: kSecAttrKeyTypeECSECPrimeRandom,
      kSecAttrKeySizeInBits: 256,
      kSecPrivateKeyAttrs: privateAttributes,
    ]
    if configuration.keyStorage == .secureEnclaveP256 {
      parameters[kSecAttrTokenID] = kSecAttrTokenIDSecureEnclave
    }
    var error: Unmanaged<CFError>?
    guard let key = SecKeyCreateRandomKey(parameters as CFDictionary, &error),
      SecurityKeyAttributes.isExactConfiguredPrivateP256(
        SecKeyCopyAttributes(key) as? [CFString: Any],
        storage: configuration.keyStorage,
        accessGroup: configuration.signingKeyAccessGroup
      )
    else { throw ProtectedKeyStoreError.unavailable }
    return try SecurityKeyAttributes.publicKeyX963Fingerprint(for: key)
  }

  func observeExistingPublicKeyFingerprint(session: any EnrollmentProtectedSession) throws -> String
  {
    guard let loaded = try loadExistingPrivateKey(session: session, allowNotFound: false) else {
      throw ProtectedKeyStoreError.unavailable
    }
    return loaded.fingerprint
  }

  func observeExistingPublicKeyX963(session: any EnrollmentProtectedSession) throws -> Data {
    guard let loaded = try loadExistingPrivateKey(session: session, allowNotFound: false) else {
      throw ProtectedKeyStoreError.unavailable
    }
    return try SecurityKeyAttributes.publicKeyX963(for: loaded.key)
  }

  func signExistingKey(
    session: any EnrollmentProtectedSession,
    expectedPublicKeyFingerprint: String,
    payload: Data
  ) throws -> Data {
    guard let loaded = try loadExistingPrivateKey(session: session, allowNotFound: false),
      loaded.fingerprint == expectedPublicKeyFingerprint
    else { throw ProtectedKeyStoreError.unavailable }
    let algorithm = SecKeyAlgorithm.ecdsaSignatureMessageX962SHA256
    guard SecKeyIsAlgorithmSupported(loaded.key, .sign, algorithm) else {
      throw ProtectedKeyStoreError.unavailable
    }
    var signingError: Unmanaged<CFError>?
    guard
      let signature = SecKeyCreateSignature(
        loaded.key,
        algorithm,
        payload as CFData,
        &signingError
      )
    else { throw ProtectedKeyStoreError.unavailable }
    return signature as Data
  }

  /// Correctly ACTIVE creation provenance permits this exact helper to return
  /// a usable key inside the coordinator only.  It is never exposed as a
  /// public generic signing API.
  private func loadExistingPrivateKey(
    session: any EnrollmentProtectedSession,
    allowNotFound: Bool
  ) throws -> (key: SecKey, fingerprint: String)? {
    let context = try localContext(from: session)
    let attributes = try existingItemAttributes(context: context, allowNotFound: allowNotFound)
    guard let attributes else { return nil }
    guard
      SecurityKeyAttributes.isExactConfiguredPrivateP256(
        attributes,
        storage: configuration.keyStorage,
        accessGroup: configuration.signingKeyAccessGroup
      )
    else { throw ProtectedKeyStoreError.unavailable }
    var query = baseQuery()
    query[kSecReturnRef] = true
    query[kSecMatchLimit] = kSecMatchLimitOne
    query[kSecUseAuthenticationContext] = context
    var result: CFTypeRef?
    let status = SecItemCopyMatching(query as CFDictionary, &result)
    if status == errSecItemNotFound, allowNotFound { return nil }
    guard status == errSecSuccess, let result, CFGetTypeID(result) == SecKeyGetTypeID() else {
      throw ProtectedKeyStoreError.unavailable
    }
    let key = result as! SecKey
    guard
      SecurityKeyAttributes.isExactConfiguredPrivateP256(
        SecKeyCopyAttributes(key) as? [CFString: Any],
        storage: configuration.keyStorage,
        accessGroup: configuration.signingKeyAccessGroup
      )
    else { throw ProtectedKeyStoreError.unavailable }
    return (key, try SecurityKeyAttributes.publicKeyX963Fingerprint(for: key))
  }

  private func existingItemAttributes(
    context: LAContext,
    allowNotFound: Bool
  ) throws -> [CFString: Any]? {
    var query = baseQuery()
    query[kSecReturnAttributes] = true
    query[kSecMatchLimit] = kSecMatchLimitOne
    query[kSecUseAuthenticationContext] = context
    var result: CFTypeRef?
    let status = SecItemCopyMatching(query as CFDictionary, &result)
    if status == errSecItemNotFound, allowNotFound { return nil }
    guard status == errSecSuccess, let attributes = result as? [CFString: Any] else {
      throw ProtectedKeyStoreError.unavailable
    }
    return attributes
  }

  private func baseQuery() -> [CFString: Any] {
    var query: [CFString: Any] = [
      kSecClass: kSecClassKey,
      kSecAttrApplicationTag: configuration.signingKeyApplicationTag,
      kSecAttrKeyClass: kSecAttrKeyClassPrivate,
      kSecAttrKeyType: kSecAttrKeyTypeECSECPrimeRandom,
      kSecAttrKeySizeInBits: 256,
    ]
    if let accessGroup = configuration.signingKeyAccessGroup {
      query[kSecAttrAccessGroup] = accessGroup
    }
    if configuration.keyStorage == .secureEnclaveP256 {
      query[kSecAttrTokenID] = kSecAttrTokenIDSecureEnclave
    }
    return query
  }

  private func localContext(from session: any EnrollmentProtectedSession) throws -> LAContext {
    guard let local = session as? LocalAuthenticationApprovalSession else {
      throw ProtectedKeyStoreError.unavailable
    }
    // SecKeyCreateRandomKey has no documented LAContext parameter.  The
    // coordinator requires this already-authenticated session and writes
    // PENDING through that context before creation; subsequent key reads
    // and signing use the same context with UI disabled.  This is not a
    // claim that the key-generation C API independently attests the UI.
    local.context.interactionNotAllowed = true
    return local.context
  }
}

enum SecurityKeyAttributes {
  /// Keychain lookup constraints are the first line of defense; inspect the
  /// returned key too so a mismatched legacy item cannot make a receipt claim
  /// the P-256 algorithm merely because it supports a similarly named API.
  static func isExactConfiguredPrivateP256(
    _ attributes: [CFString: Any]?,
    storage: ProtectedKeyStorage,
    accessGroup: String? = nil
  ) -> Bool {
    guard let attributes,
      let keyType = attributes[kSecAttrKeyType] as? String,
      keyType == (kSecAttrKeyTypeECSECPrimeRandom as String),
      let keySize = exactInteger(attributes[kSecAttrKeySizeInBits]), keySize == 256,
      let keyClass = attributes[kSecAttrKeyClass] as? String,
      keyClass == (kSecAttrKeyClassPrivate as String),
      exactBool(attributes[kSecAttrCanSign]) == true,
      hasExpectedAccessGroup(attributes[kSecAttrAccessGroup], expected: accessGroup)
    else { return false }
    switch storage {
    case .keychainP256:
      return attributes[kSecAttrTokenID] == nil
    case .secureEnclaveP256:
      return (attributes[kSecAttrTokenID] as? String) == (kSecAttrTokenIDSecureEnclave as String)
    }
  }

  static func publicKeyX963(for privateKey: SecKey) throws -> Data {
    guard let publicKey = SecKeyCopyPublicKey(privateKey) else {
      throw ProtectedKeyStoreError.unavailable
    }
    var error: Unmanaged<CFError>?
    guard let data = SecKeyCopyExternalRepresentation(publicKey, &error) as Data?,
      data.count == 65,
      data.first == 0x04
    else { throw ProtectedKeyStoreError.unavailable }
    return data
  }

  static func publicKeyX963Fingerprint(for privateKey: SecKey) throws -> String {
    let data = try publicKeyX963(for: privateKey)
    return CanonicalJSON.sha256Hex(data)
  }

  private static func hasExpectedAccessGroup(_ value: Any?, expected: String?) -> Bool {
    switch expected {
    case .some(let expected):
      (value as? String) == expected
    case .none:
      value == nil
    }
  }

  private static func exactInteger(_ value: Any?) -> Int? {
    guard let value else { return nil }
    let reference = value as CFTypeRef
    guard CFGetTypeID(reference) == CFNumberGetTypeID(),
      CFGetTypeID(reference) != CFBooleanGetTypeID()
    else { return nil }
    let number = value as! CFNumber
    // `NSNumber.intValue` would turn 256.5 into 256.  Security returns a
    // numeric key-size attribute, but this closed helper rejects every
    // floating representation before the Swift bridge can truncate it.
    guard !CFNumberIsFloatType(number) else { return nil }
    var integer: Int64 = 0
    guard CFNumberGetValue(number, .sInt64Type, &integer),
      integer >= Int64(Int.min), integer <= Int64(Int.max)
    else { return nil }
    return Int(integer)
  }

  private static func exactBool(_ value: Any?) -> Bool? {
    guard let value else { return nil }
    if CFGetTypeID(value as CFTypeRef) == CFBooleanGetTypeID() {
      return (value as! NSNumber).boolValue
    }
    return nil
  }
}

private final class SecurityEnrollmentIdentifierGenerator: EnrollmentIdentifierGenerator,
  @unchecked Sendable
{
  func nextEnrollmentIdentifier() throws -> String {
    var bytes = [UInt8](repeating: 0, count: 32)
    guard SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes) == errSecSuccess else {
      throw ProtectedKeyStoreError.unavailable
    }
    return Data(bytes).base64EncodedString()
      .replacingOccurrences(of: "+", with: "-")
      .replacingOccurrences(of: "/", with: "_")
      .replacingOccurrences(of: "=", with: "")
  }
}

private enum InitialEnrollmentTimestamp {
  static func now() -> String {
    let date = Date()
    var calendar = Calendar(identifier: .gregorian)
    calendar.locale = Locale(identifier: "en_US_POSIX")
    calendar.timeZone = TimeZone(secondsFromGMT: 0)!
    let components = calendar.dateComponents(
      [.year, .month, .day, .hour, .minute, .second, .nanosecond],
      from: date
    )
    let microseconds = max(0, min(999_999, (components.nanosecond ?? 0) / 1_000))
    return String(
      format: "%04d-%02d-%02dT%02d:%02d:%02d.%06dZ",
      components.year ?? 1,
      components.month ?? 1,
      components.day ?? 1,
      components.hour ?? 0,
      components.minute ?? 0,
      components.second ?? 0,
      microseconds
    )
  }
}

final class SecurityProtectedKeyReceiptSigner: ReceiptSigner {
  private let store: any ProtectedKeyStore

  init(store: any ProtectedKeyStore) {
    self.store = store
  }

  @discardableResult
  public func sign(
    _ payload: ReceiptSigningPayload,
    authenticatedSession: any AuthenticatedApprovalSession,
    permit: any ReceiptSigningPermit,
    completion: @escaping (Result<Data, ApprovalHostError>) -> Void
  ) -> any ApprovalCancellation {
    guard let localSession = authenticatedSession as? LocalAuthenticationApprovalSession else {
      completion(.failure(.signingUnavailable))
      return NoopApprovalCancellation()
    }
    guard permit.permitsSigning() else {
      completion(.failure(.authenticationCancelled))
      return NoopApprovalCancellation()
    }
    let cancellation = SecuritySigningCancellation()
    let callback = SigningCallback(completion)
    DispatchQueue.global(qos: .userInitiated).async { [store] in
      guard !cancellation.isCancelled, permit.permitsSigning() else {
        callback.call(.failure(.authenticationCancelled))
        return
      }
      do {
        let signature = try store.signApprovedReceiptPayload(
          payload,
          authenticatedSession: localSession,
          claimPermit: { permit.claimSigning() }
        )
        callback.call(.success(signature))
      } catch let error as ProtectedKeyStoreError {
        switch error {
        case .permitNotClaimed:
          callback.call(.failure(.authenticationCancelled))
        default:
          callback.call(.failure(.signingUnavailable))
        }
      } catch {
        callback.call(.failure(.signingUnavailable))
      }
    }
    return cancellation
  }
}

private final class SecuritySigningCancellation: ApprovalCancellation, @unchecked Sendable {
  private let lock = NSLock()
  private var cancelled = false

  var isCancelled: Bool {
    lock.lock()
    defer { lock.unlock() }
    return cancelled
  }

  func cancel() {
    lock.lock()
    cancelled = true
    lock.unlock()
  }
}

private final class SigningCallback: @unchecked Sendable {
  private let completion: (Result<Data, ApprovalHostError>) -> Void
  init(_ completion: @escaping (Result<Data, ApprovalHostError>) -> Void) {
    self.completion = completion
  }
  func call(_ result: Result<Data, ApprovalHostError>) { completion(result) }
}
