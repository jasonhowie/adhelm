import ApprovalHostCore
import Foundation

/// An opaque fresh-session boundary for the future protected redemption path.
/// The coordinator invalidates the supplied session on every exit. Test fakes
/// implement this protocol without constructing an LAContext.
protocol ReceiptRedemptionSession: AnyObject, Sendable {
  func invalidate()
}

extension LocalAuthenticationApprovalSession: ReceiptRedemptionSession {}

/// Fixed, sanitized outcomes from the future already-leased current-key
/// verifier. No raw Security, Keychain, or cryptographic error crosses this
/// protocol.
enum ReceiptRedemptionFixedSlotError: Error, Equatable, Sendable {
  case unavailable
  case revoked
}

/// The exact key observed with an ACTIVE enrollment during an already-held
/// verification lease. This is internal transport between protected native
/// collaborators, never a general key lookup or an installed trust object.
struct ReceiptRedemptionCurrentReceipt: Sendable, CustomStringConvertible,
  CustomDebugStringConvertible
{
  let receipt: SignedApprovalReceipt
  let activePublicKeyX963: Data
  let currentVerificationObservedAtText: String

  var description: String { "ReceiptRedemptionCurrentReceipt(redacted)" }
  var debugDescription: String { description }

  init(
    receipt: SignedApprovalReceipt, activePublicKeyX963: Data,
    currentVerificationObservedAt: Date
  ) throws {
    guard activePublicKeyX963.count == 65, activePublicKeyX963.first == 0x04,
      let snapshot = activePublicKeyX963.withUnsafeBytes({ bytes -> Data? in
        guard let base = bytes.baseAddress else { return nil }
        return Data(bytes: base, count: bytes.count)
      })
    else { throw ReceiptRedemptionFixedSlotError.unavailable }
    self.receipt = receipt
    self.activePublicKeyX963 = snapshot
    currentVerificationObservedAtText = try ReceiptRedemptionGlobalReadyV1(
      generation: 1, observedAt: currentVerificationObservedAt
    ).observedAtText
  }

  func currentVerificationObservationDate() throws -> Date {
    try ReceiptRedemptionGlobalReadyV1(
      generation: 1, observedAtText: currentVerificationObservedAtText
    ).date()
  }
}

/// The only protected persistence operations available to receipt redemption.
/// A future real adapter selects one fixed nonce-state item and one fixed
/// global-state item from reviewed build metadata; callers cannot pass a
/// Keychain service, account, key, query, or arbitrary storage location.
///
/// The collaborator's global replace is a compare-and-replace contract while
/// this lease is held. It does not claim a real Keychain CAS implementation or
/// atomicity across the global and nonce items.
protocol ReceiptRedemptionLease: AnyObject, Sendable {
  /// Must verify the exact receipt under the current ACTIVE fixed-slot key
  /// while the enclosing fixed-slot lease is already held. A future concrete
  /// adapter must not recursively call the existing lock-taking verifier.
  func verifyCurrentReceipt(
    _ receiptData: Data,
    expectedChallenge: ApprovalChallenge,
    session: any ReceiptRedemptionSession,
    now: Date
  ) throws -> ReceiptRedemptionCurrentReceipt

  func loadRedemptionState(nonceDigest: String) throws -> Data?
  func loadGlobalRedemptionState() throws -> Data?
  func writePendingRedemption(_ canonicalState: Data) throws
  func replacePendingRedemption(_ expectedPending: Data, with canonicalVerified: Data) throws
  func replaceGlobalRedemptionState(_ expected: Data, with canonicalState: Data) throws
}

/// Owns exactly one already-held fixed-slot lease for a whole redemption. It
/// is module-internal so neither XPC nor a generic caller can obtain a lease.
protocol ReceiptRedemptionFixedSlotCoordinator: AnyObject, Sendable {
  func withRedemptionLease<T>(
    _ operation: (any ReceiptRedemptionLease) throws -> T
  ) throws -> T
}

/// Closed outcomes for the module-internal redemption coordinator. A global
/// intent written or possibly written at reservation is intentionally never
/// classified as absent or retryable.
enum ReceiptRedemptionError: Error, Equatable, Sendable {
  case unavailable
  case expired
  case clockRollback
  case currentStateUnavailable
  case currentStateRevoked
  case globalStateRequired
  case pendingRecoveryRequired
  case replayed
  case postReservationUnverified
}

/// A one-use, in-process source-binding token. It is deliberately not Codable,
/// not an XPC object, and not a Python authority token. Only the coordinator
/// can consume its private facts into a fresh, exact-bound bridge candidate.
/// No installed runtime or Python acceptance path currently uses that producer.
final class ReceiptRedemptionPermit: @unchecked Sendable {
  private let binding: ReceiptRedemptionSourceBindingV1
  private let canonicalReceipt: Data
  private let activePublicKeyX963: Data
  private let nativeObservedAtText: String
  private let lock = NSLock()
  private var claimed = false

  fileprivate init(
    binding: ReceiptRedemptionSourceBindingV1,
    canonicalReceipt: Data,
    activePublicKeyX963: Data,
    nativeObservedAtText: String
  ) {
    self.binding = binding
    self.canonicalReceipt = canonicalReceipt
    self.activePublicKeyX963 = activePublicKeyX963
    self.nativeObservedAtText = nativeObservedAtText
  }

  /// This only consumes the in-process token for an exact later protected
  /// installed acceptance integration. It neither grants provider authority
  /// nor performs an egress.
  func claimPendingProtectedExecution(
    binding expectedBinding: ReceiptRedemptionSourceBindingV1
  ) -> Bool {
    lock.lock()
    defer { lock.unlock() }
    guard !claimed, binding == expectedBinding else { return false }
    claimed = true
    return true
  }

  /// Only the coordinator can consume native evidence into a private bridge
  /// response. Failure burns this process-local handoff; native consumption
  /// has already happened and is never retried through this token.
  fileprivate func consumeBridgeCandidate(
    request: NativeApprovalBridgeRequestV1,
    metadata: ApprovalHostBuildMetadataV1,
    exchangeStartedAt: Date,
    currentNow: Date
  ) throws -> NativeApprovalBridgeCandidateV1 {
    lock.lock()
    defer { lock.unlock() }
    guard !claimed else { throw ReceiptRedemptionError.postReservationUnverified }
    claimed = true
    let challenge = try request.boundChallenge(
      metadata: metadata, exchangeStartedAt: exchangeStartedAt)
    let expectedBinding = try ReceiptRedemptionSourceBindingV1(
      configuration: metadata.makeTrustedHostConfiguration(),
      expectedChallenge: challenge, canonicalReceipt: canonicalReceipt)
    guard expectedBinding == binding else {
      throw ReceiptRedemptionError.postReservationUnverified
    }
    func encoded(_ bytes: Data) -> String {
      bytes.base64EncodedString().replacingOccurrences(of: "+", with: "-")
        .replacingOccurrences(of: "/", with: "_")
        .replacingOccurrences(of: "=", with: "")
    }
    let bytes = try CanonicalJSON.encode(
      .object([
        "schema_version": .integer(1),
        "message_type": .string(NativeApprovalBridgeCandidateV1.messageType),
        "native_observation_status": .string(NativeApprovalBridgeCandidateV1.observationStatus),
        "request_id_digest": .string(CanonicalJSON.sha256Hex(request.requestID)),
        "request_digest": .string(request.digest),
        "source_context_digest": .string(request.sourceContextDigest),
        "metadata_digest": .string(request.metadataDigest),
        "protocol_configuration_digest": .string(request.protocolConfigurationDigest),
        "canonical_challenge_b64url": .string(encoded(challenge.canonicalBytes)),
        "challenge_digest": .string(binding.challengeDigest),
        "challenge_nonce_digest": .string(binding.challengeNonceDigest),
        "receipt_kind": .string(binding.kind.rawValue),
        "expires_at": .string(binding.expiresAtText),
        "canonical_receipt_b64url": .string(encoded(canonicalReceipt)),
        "receipt_digest": .string(binding.receiptDigest),
        "receipt_id": .string(binding.receiptIdentifier),
        "active_public_key_x963_b64url": .string(encoded(activePublicKeyX963)),
        "active_public_key_fingerprint": .string(CanonicalJSON.sha256Hex(activePublicKeyX963)),
        "native_observed_at": .string(nativeObservedAtText),
      ]))
    return try NativeApprovalBridgeCandidateV1.decode(
      bytes, expectedRequest: request, metadata: metadata,
      exchangeStartedAt: exchangeStartedAt, currentNow: currentNow)
  }

  /// Produces only the distinct Meta campaign PAUSE V2 candidate after this
  /// permit has been obtained by the durable current-key/global/nonce path.
  /// It is deliberately separate from the V1 producer: no V1 candidate can
  /// carry Meta evidence, and this private token remains one-use even if
  /// candidate construction or final validation fails.
  fileprivate func consumeMetaCampaignPauseBridgeCandidate(
    request: NativeMetaCampaignPauseApprovalBridgeRequestV2,
    metadata: ApprovalHostBuildMetadataV1,
    exchangeStartedAt: Date,
    currentNow: Date
  ) throws -> NativeMetaCampaignPauseApprovalBridgeCandidateV2 {
    lock.lock()
    defer { lock.unlock() }
    guard !claimed else { throw ReceiptRedemptionError.postReservationUnverified }
    claimed = true
    let challenge = try request.boundChallenge(
      metadata: metadata, exchangeStartedAt: exchangeStartedAt)
    guard challenge.kind == .metaCampaignPausePlan else {
      throw ReceiptRedemptionError.postReservationUnverified
    }
    let expectedBinding = try ReceiptRedemptionSourceBindingV1(
      configuration: metadata.makeTrustedHostConfiguration(),
      expectedChallenge: challenge,
      canonicalReceipt: canonicalReceipt
    )
    guard expectedBinding == binding else {
      throw ReceiptRedemptionError.postReservationUnverified
    }
    func encoded(_ bytes: Data) -> String {
      bytes.base64EncodedString().replacingOccurrences(of: "+", with: "-")
        .replacingOccurrences(of: "/", with: "_")
        .replacingOccurrences(of: "=", with: "")
    }
    let bytes = try CanonicalJSON.encode(
      .object([
        "schema_version": .integer(2),
        "message_type": .string(NativeMetaCampaignPauseApprovalBridgeCandidateV2.messageType),
        "native_observation_status": .string(
          NativeMetaCampaignPauseApprovalBridgeCandidateV2.observationStatus),
        "request_id_digest": .string(CanonicalJSON.sha256Hex(request.requestID)),
        "request_digest": .string(request.digest),
        "source_context_digest": .string(request.sourceContextDigest),
        "metadata_digest": .string(request.metadataDigest),
        "protocol_configuration_digest": .string(request.protocolConfigurationDigest),
        "canonical_challenge_b64url": .string(encoded(challenge.canonicalBytes)),
        "challenge_digest": .string(binding.challengeDigest),
        "challenge_nonce_digest": .string(binding.challengeNonceDigest),
        "receipt_kind": .string(binding.kind.rawValue),
        "expires_at": .string(binding.expiresAtText),
        "canonical_receipt_b64url": .string(encoded(canonicalReceipt)),
        "receipt_digest": .string(binding.receiptDigest),
        "receipt_id": .string(binding.receiptIdentifier),
        "active_public_key_x963_b64url": .string(encoded(activePublicKeyX963)),
        "active_public_key_fingerprint": .string(CanonicalJSON.sha256Hex(activePublicKeyX963)),
        "native_observed_at": .string(nativeObservedAtText),
      ]))
    return try NativeMetaCampaignPauseApprovalBridgeCandidateV2.decode(
      bytes,
      expectedRequest: request,
      metadata: metadata,
      exchangeStartedAt: exchangeStartedAt,
      currentNow: currentNow
    )
  }
}

/// Module-internal coordinator for the future native receipt-redemption bridge.
/// It is intentionally unwired to the runtime, XPC methods, CLI, Python trust
/// loader and provider adapters. The private bridge producer below preserves
/// the same native verification/consumption ordering.
final class ReceiptRedemptionCoordinator: @unchecked Sendable {
  private let configuration: TrustedHostConfiguration
  private let fixedSlot: any ReceiptRedemptionFixedSlotCoordinator
  private let clock: () -> Date

  init(
    configuration: TrustedHostConfiguration,
    fixedSlot: any ReceiptRedemptionFixedSlotCoordinator,
    clock: @escaping () -> Date = Date.init
  ) {
    self.configuration = configuration
    self.fixedSlot = fixedSlot
    self.clock = clock
  }

  /// Internal post-consumption producer. No public receipt-only XPC endpoint
  /// calls this method. A future installed request path must provide reviewed
  /// static metadata and a receipt produced by its explicit review/auth flow.
  func redeemBridgeCandidate(
    request: NativeApprovalBridgeRequestV1,
    metadata: ApprovalHostBuildMetadataV1,
    exchangeStartedAt: Date,
    receiptData: Data,
    session: any ReceiptRedemptionSession,
    shouldContinue: @escaping () -> Bool = { true }
  ) throws -> NativeApprovalBridgeCandidateV1 {
    defer { session.invalidate() }
    return try redeemBridgeCandidateUsingSession(
      request: request,
      metadata: metadata,
      exchangeStartedAt: exchangeStartedAt,
      receiptData: receiptData,
      session: session,
      shouldContinue: shouldContinue
    )
  }

  /// The caller must hold an engine-created post-signing session lease for the
  /// exact `session` through this synchronous operation. This helper never
  /// invalidates the session; its sole caller is the callback-scoped bridge
  /// producer below, while the existing public-internal wrappers retain their
  /// own `defer` invalidation behavior.
  func redeemBridgeCandidateUsingSession(
    request: NativeApprovalBridgeRequestV1,
    metadata: ApprovalHostBuildMetadataV1,
    exchangeStartedAt: Date,
    receiptData: Data,
    session: any ReceiptRedemptionSession,
    shouldContinue: @escaping () -> Bool
  ) throws -> NativeApprovalBridgeCandidateV1 {
    let challenge: ApprovalChallenge
    do {
      let expectedConfiguration = try metadata.makeTrustedHostConfiguration()
      guard expectedConfiguration == configuration, shouldContinue() else {
        throw ReceiptRedemptionError.unavailable
      }
      challenge = try request.boundChallenge(
        metadata: metadata, exchangeStartedAt: exchangeStartedAt)
      let beforeLeaseNow = try currentObservation().date()
      guard beforeLeaseNow >= exchangeStartedAt, beforeLeaseNow < challenge.expiresAt,
        shouldContinue()
      else { throw ReceiptRedemptionError.unavailable }
    } catch {
      throw ReceiptRedemptionError.unavailable
    }
    let permit = try redeemUsingSession(
      receiptData: receiptData, expectedChallenge: challenge,
      session: session, shouldContinue: shouldContinue)
    do {
      guard shouldContinue() else { throw ReceiptRedemptionError.postReservationUnverified }
      let now = try currentObservation().date()
      let candidate = try permit.consumeBridgeCandidate(
        request: request, metadata: metadata,
        exchangeStartedAt: exchangeStartedAt, currentNow: now)
      // Candidate validation may itself be delayed. Recheck the trusted clock
      // and cancellation immediately before returning the private evidence.
      let finalNow = try currentObservation(after: now).date()
      guard finalNow < challenge.expiresAt, shouldContinue() else {
        throw ReceiptRedemptionError.postReservationUnverified
      }
      return candidate
    } catch {
      throw ReceiptRedemptionError.postReservationUnverified
    }
  }

  /// The Meta V2 counterpart of the package-only V1 producer. This method
  /// remains synchronous under the engine's callback-scoped authenticated
  /// session; it cannot open an XPC route or issue a provider grant.
  func redeemMetaCampaignPauseBridgeCandidate(
    request: NativeMetaCampaignPauseApprovalBridgeRequestV2,
    metadata: ApprovalHostBuildMetadataV1,
    exchangeStartedAt: Date,
    receiptData: Data,
    session: any ReceiptRedemptionSession,
    shouldContinue: @escaping () -> Bool = { true }
  ) throws -> NativeMetaCampaignPauseApprovalBridgeCandidateV2 {
    defer { session.invalidate() }
    return try redeemMetaCampaignPauseBridgeCandidateUsingSession(
      request: request,
      metadata: metadata,
      exchangeStartedAt: exchangeStartedAt,
      receiptData: receiptData,
      session: session,
      shouldContinue: shouldContinue
    )
  }

  /// The caller holds the exact engine-created session lease for this whole
  /// synchronous operation. The durable receipt/nonce transition happens in
  /// `redeemUsingSession` before a V2 candidate can be constructed. Any
  /// cancellation or expiry after that boundary returns no candidate and is
  /// deliberately classified as inspect-before-repeat ambiguity.
  func redeemMetaCampaignPauseBridgeCandidateUsingSession(
    request: NativeMetaCampaignPauseApprovalBridgeRequestV2,
    metadata: ApprovalHostBuildMetadataV1,
    exchangeStartedAt: Date,
    receiptData: Data,
    session: any ReceiptRedemptionSession,
    shouldContinue: @escaping () -> Bool
  ) throws -> NativeMetaCampaignPauseApprovalBridgeCandidateV2 {
    let challenge: ApprovalChallenge
    do {
      let expectedConfiguration = try metadata.makeTrustedHostConfiguration()
      guard expectedConfiguration == configuration, shouldContinue() else {
        throw ReceiptRedemptionError.unavailable
      }
      challenge = try request.boundChallenge(
        metadata: metadata, exchangeStartedAt: exchangeStartedAt)
      guard challenge.kind == .metaCampaignPausePlan else {
        throw ReceiptRedemptionError.unavailable
      }
      let beforeLeaseNow = try currentObservation().date()
      guard beforeLeaseNow >= exchangeStartedAt, beforeLeaseNow < challenge.expiresAt,
        shouldContinue()
      else { throw ReceiptRedemptionError.unavailable }
    } catch {
      throw ReceiptRedemptionError.unavailable
    }
    let permit = try redeemUsingSession(
      receiptData: receiptData,
      expectedChallenge: challenge,
      session: session,
      shouldContinue: shouldContinue
    )
    do {
      guard shouldContinue() else { throw ReceiptRedemptionError.postReservationUnverified }
      let now = try currentObservation().date()
      let candidate = try permit.consumeMetaCampaignPauseBridgeCandidate(
        request: request,
        metadata: metadata,
        exchangeStartedAt: exchangeStartedAt,
        currentNow: now
      )
      let finalNow = try currentObservation(after: now).date()
      guard finalNow < challenge.expiresAt, shouldContinue() else {
        throw ReceiptRedemptionError.postReservationUnverified
      }
      return candidate
    } catch {
      throw ReceiptRedemptionError.postReservationUnverified
    }
  }

  /// Reserves and verifies one exact receipt under one fixed-slot lease. A
  /// current-key/signature failure before the global INTENT_PENDING transition
  /// leaves both protected records unchanged. Once that transition is attempted,
  /// every later failure is an inspect-before-repeat ambiguity.
  func redeem(
    receiptData: Data,
    expectedChallenge: ApprovalChallenge,
    session: any ReceiptRedemptionSession,
    shouldContinue: @escaping () -> Bool = { true }
  ) throws -> ReceiptRedemptionPermit {
    defer { session.invalidate() }
    return try redeemUsingSession(
      receiptData: receiptData,
      expectedChallenge: expectedChallenge,
      session: session,
      shouldContinue: shouldContinue
    )
  }

  private func redeemUsingSession(
    receiptData: Data,
    expectedChallenge: ApprovalChallenge,
    session: any ReceiptRedemptionSession,
    shouldContinue: @escaping () -> Bool
  ) throws -> ReceiptRedemptionPermit {
    guard !receiptData.isEmpty, receiptData.count <= 16_384, shouldContinue() else {
      throw ReceiptRedemptionError.unavailable
    }
    guard
      let receiptSnapshot = receiptData.withUnsafeBytes({ bytes -> Data? in
        guard let baseAddress = bytes.baseAddress else { return nil }
        return Data(bytes: baseAddress, count: bytes.count)
      })
    else { throw ReceiptRedemptionError.unavailable }

    // Reject malformed, rebound, or already-expired snapshot bytes before they
    // can acquire the protected-slot lease or select/read a nonce record. The
    // same immutable bytes are preflighted again under the lease below.
    let externalObservation: ReceiptRedemptionGlobalReadyV1
    let binding: ReceiptRedemptionSourceBindingV1
    do {
      externalObservation = try currentObservation()
      try preflightReceipt(
        receiptSnapshot,
        expectedChallenge: expectedChallenge,
        now: try externalObservation.date()
      )
      binding = try ReceiptRedemptionSourceBindingV1(
        configuration: configuration,
        expectedChallenge: expectedChallenge,
        canonicalReceipt: receiptSnapshot
      )
    } catch let error as ReceiptRedemptionError {
      throw error
    } catch {
      throw ReceiptRedemptionError.currentStateUnavailable
    }

    do {
      return try fixedSlot.withRedemptionLease { lease in
        try self.redeemUnderLease(
          lease: lease,
          receiptData: receiptSnapshot,
          expectedChallenge: expectedChallenge,
          binding: binding,
          externalObservation: externalObservation,
          session: session,
          shouldContinue: shouldContinue
        )
      }
    } catch let error as ReceiptRedemptionError {
      throw error
    } catch {
      throw ReceiptRedemptionError.currentStateUnavailable
    }
  }

  private func redeemUnderLease(
    lease: any ReceiptRedemptionLease,
    receiptData: Data,
    expectedChallenge: ApprovalChallenge,
    binding: ReceiptRedemptionSourceBindingV1,
    externalObservation: ReceiptRedemptionGlobalReadyV1,
    session: any ReceiptRedemptionSession,
    shouldContinue: @escaping () -> Bool
  ) throws -> ReceiptRedemptionPermit {
    guard shouldContinue() else { throw ReceiptRedemptionError.unavailable }
    let expiry: Date
    do {
      expiry = try bindingExpiryDate(binding)
    } catch let error as ReceiptRedemptionError {
      throw error
    } catch {
      throw ReceiptRedemptionError.currentStateUnavailable
    }
    let initialObservation: ReceiptRedemptionGlobalReadyV1
    do {
      initialObservation = try currentObservation(after: try externalObservation.date())
      guard try initialObservation.date() < expiry else {
        throw ReceiptRedemptionError.expired
      }
    } catch let error as ReceiptRedemptionError {
      throw error
    } catch {
      throw ReceiptRedemptionError.currentStateUnavailable
    }

    // Re-run bounded receipt preflight under the lease and a fresh clock before
    // any protected read.
    try preflightReceipt(
      receiptData,
      expectedChallenge: expectedChallenge,
      now: try initialObservation.date()
    )

    // A global record must be installed by a future protected setup path. Nil
    // is never inferred as a safe first use. An outstanding intent quarantines
    // every nonce until explicit future recovery, even when its binding matches.
    let retainedGlobal = try requiredGlobalState(lease: lease)
    guard !retainedGlobal.isIntentPending else {
      throw ReceiptRedemptionError.pendingRecoveryRequired
    }
    guard try initialObservation.date() >= retainedGlobal.date() else {
      throw ReceiptRedemptionError.clockRollback
    }

    // A nonce PENDING paired with READY is recovery-required. A matching
    // VERIFIED record is only replay evidence and never yields a permit.
    let existing = try existingState(lease: lease, expectedBinding: binding)
    if let existing {
      if existing.isPending { throw ReceiptRedemptionError.pendingRecoveryRequired }
      throw ReceiptRedemptionError.replayed
    }

    // Current-ACTIVE cryptographic verification is always complete before the
    // first global intent mutation. A forged or revoked receipt cannot reserve
    // a nonce merely because it has plausible canonical shape.
    let firstVerified: ReceiptRedemptionCurrentReceipt
    do {
      firstVerified = try lease.verifyCurrentReceipt(
        receiptData,
        expectedChallenge: expectedChallenge,
        session: session,
        now: try initialObservation.date()
      )
    } catch let error as ReceiptRedemptionFixedSlotError {
      throw currentStateError(error)
    } catch {
      throw ReceiptRedemptionError.currentStateUnavailable
    }
    guard
      binding.matchesVerifiedReceipt(
        firstVerified.receipt,
        configuration: configuration,
        expectedChallenge: expectedChallenge
      )
    else {
      throw ReceiptRedemptionError.currentStateUnavailable
    }
    do {
      try requireExactCurrentReceipt(
        firstVerified, receiptData: receiptData, challenge: expectedChallenge,
        now: try initialObservation.date())
    } catch {
      throw ReceiptRedemptionError.currentStateUnavailable
    }

    // Verification itself may move the clock. A rollback or expiry here is
    // still pre-reservation and writes neither global intent nor nonce state.
    let postVerificationObservation: ReceiptRedemptionGlobalReadyV1
    do {
      postVerificationObservation = try currentObservation(
        after: try firstVerified.currentVerificationObservationDate())
      guard try postVerificationObservation.date() < expiry else {
        throw ReceiptRedemptionError.expired
      }
    } catch let error as ReceiptRedemptionError {
      throw error
    } catch {
      throw ReceiptRedemptionError.currentStateUnavailable
    }

    // Sample immediately before the first durable reservation transition.
    guard shouldContinue() else { throw ReceiptRedemptionError.unavailable }
    let reservationObservation: ReceiptRedemptionGlobalReadyV1
    do {
      reservationObservation = try currentObservation(
        after: try postVerificationObservation.date())
      guard try reservationObservation.date() < expiry else {
        throw ReceiptRedemptionError.expired
      }
    } catch let error as ReceiptRedemptionError {
      throw error
    } catch {
      throw ReceiptRedemptionError.currentStateUnavailable
    }

    let pending: ReceiptRedemptionStateV1
    let pendingBytes: Data
    let intent: ReceiptRedemptionGlobalStateV1
    let retainedGlobalBytes: Data
    let intentBytes: Data
    do {
      pending = .pending(
        try ReceiptRedemptionPendingV1(
          binding: binding,
          reservedAtText: reservationObservation.observedAtText
        )
      )
      pendingBytes = try pending.canonicalBytes()
      retainedGlobalBytes = try retainedGlobal.canonicalBytes()
      intent = try retainedGlobal.nextIntent(
        binding: binding,
        observedAtText: reservationObservation.observedAtText
      )
      intentBytes = try intent.canonicalBytes()
    } catch ReceiptRedemptionContractError.expired {
      throw ReceiptRedemptionError.expired
    } catch {
      throw ReceiptRedemptionError.currentStateUnavailable
    }

    // The ambiguity boundary starts before the global intent compare/replace.
    // Exact intent readback is mandatory before an independent nonce PENDING
    // write can start. The collaborator's compare/replace is only a contract
    // under this held lease; this slice does not claim a cross-store transaction.
    do {
      try lease.replaceGlobalRedemptionState(retainedGlobalBytes, with: intentBytes)
      try requireExactGlobalReadback(
        lease: lease,
        expectedBytes: intentBytes,
        expectedState: intent
      )
    } catch {
      throw ReceiptRedemptionError.postReservationUnverified
    }

    do {
      try lease.writePendingRedemption(pendingBytes)
      try requireExactStateReadback(
        lease: lease,
        nonceDigest: binding.challengeNonceDigest,
        expectedBytes: pendingBytes,
        expectedState: pending
      )
    } catch {
      throw ReceiptRedemptionError.postReservationUnverified
    }

    // Once intent/PENDING exist, any cancellation, expiry, current-key change,
    // or readback failure remains inspect-before-repeat.
    guard shouldContinue() else { throw ReceiptRedemptionError.postReservationUnverified }
    let secondVerificationObservation: ReceiptRedemptionGlobalReadyV1
    let secondVerified: ReceiptRedemptionCurrentReceipt
    do {
      secondVerificationObservation = try currentObservation(
        after: try reservationObservation.date())
      guard try secondVerificationObservation.date() < expiry else {
        throw ReceiptRedemptionContractError.expired
      }
      secondVerified = try lease.verifyCurrentReceipt(
        receiptData,
        expectedChallenge: expectedChallenge,
        session: session,
        now: try secondVerificationObservation.date()
      )
      guard
        binding.matchesVerifiedReceipt(
          secondVerified.receipt,
          configuration: configuration,
          expectedChallenge: expectedChallenge
        )
      else {
        throw ReceiptRedemptionContractError.malformed
      }
      guard secondVerified.activePublicKeyX963 == firstVerified.activePublicKeyX963 else {
        throw ReceiptRedemptionContractError.malformed
      }
      try requireExactCurrentReceipt(
        secondVerified, receiptData: receiptData, challenge: expectedChallenge,
        now: try secondVerificationObservation.date())
    } catch {
      throw ReceiptRedemptionError.postReservationUnverified
    }

    // A cancellation that arrives during verify #2 must retain the intent and
    // nonce PENDING without a promotion clock/state write.
    guard shouldContinue() else { throw ReceiptRedemptionError.postReservationUnverified }
    let promotionObservation: ReceiptRedemptionGlobalReadyV1
    do {
      promotionObservation = try currentObservation(
        after: try secondVerified.currentVerificationObservationDate())
      guard try promotionObservation.date() < expiry else {
        throw ReceiptRedemptionContractError.expired
      }
    } catch {
      throw ReceiptRedemptionError.postReservationUnverified
    }
    guard shouldContinue() else { throw ReceiptRedemptionError.postReservationUnverified }

    let verified: ReceiptRedemptionStateV1
    let verifiedBytes: Data
    let ready: ReceiptRedemptionGlobalStateV1
    let readyBytes: Data
    do {
      verified = .verified(
        try ReceiptRedemptionVerifiedV1(
          pending: try pendingRecord(from: pending),
          verifiedAtText: promotionObservation.observedAtText
        )
      )
      verifiedBytes = try verified.canonicalBytes()
      ready = try intent.nextReady(
        expectedBinding: binding,
        observedAtText: promotionObservation.observedAtText
      )
      readyBytes = try ready.canonicalBytes()

      try lease.replacePendingRedemption(pendingBytes, with: verifiedBytes)
      try requireExactStateReadback(
        lease: lease,
        nonceDigest: binding.challengeNonceDigest,
        expectedBytes: verifiedBytes,
        expectedState: verified
      )
      try lease.replaceGlobalRedemptionState(intentBytes, with: readyBytes)
      try requireExactGlobalReadback(
        lease: lease,
        expectedBytes: readyBytes,
        expectedState: ready
      )
    } catch {
      throw ReceiptRedemptionError.postReservationUnverified
    }

    // The final exact readback itself can block. Before a permit exists, take
    // one last trusted sample and cancellation gate. VERIFIED remains consumed
    // if this fails; a future installed acceptance integration must still
    // recheck at its own later handoff boundary.
    guard shouldContinue() else { throw ReceiptRedemptionError.postReservationUnverified }
    let finalObservation: ReceiptRedemptionGlobalReadyV1
    do {
      finalObservation = try currentObservation(after: try promotionObservation.date())
      guard try finalObservation.date() < expiry, shouldContinue() else {
        throw ReceiptRedemptionError.postReservationUnverified
      }
    } catch let error as ReceiptRedemptionError {
      throw error == .postReservationUnverified
        ? error
        : ReceiptRedemptionError.postReservationUnverified
    } catch {
      throw ReceiptRedemptionError.postReservationUnverified
    }

    return ReceiptRedemptionPermit(
      binding: binding,
      canonicalReceipt: receiptData,
      activePublicKeyX963: secondVerified.activePublicKeyX963,
      nativeObservedAtText: finalObservation.observedAtText
    )
  }

  private func requireExactCurrentReceipt(
    _ current: ReceiptRedemptionCurrentReceipt,
    receiptData: Data,
    challenge: ApprovalChallenge,
    now: Date
  ) throws {
    let observed = try current.currentVerificationObservationDate()
    guard current.receipt.canonicalReceipt == receiptData,
      observed >= now, observed < challenge.expiresAt
    else {
      throw ReceiptRedemptionFixedSlotError.unavailable
    }
    let verifier = try PinnedReceiptVerifier(
      configuration: configuration, trustedPublicKeyX963: current.activePublicKeyX963)
    guard
      try verifier.verify(receiptData, expectedChallenge: challenge, now: observed)
        == current.receipt
    else { throw ReceiptRedemptionFixedSlotError.unavailable }
  }

  /// Uses the global-ready timestamp encoder only as a bounded private clock
  /// wrapper. This value is never persisted by itself.
  private func currentObservation() throws -> ReceiptRedemptionGlobalReadyV1 {
    do {
      return try ReceiptRedemptionGlobalReadyV1(generation: 1, observedAt: clock())
    } catch {
      throw ReceiptRedemptionError.currentStateUnavailable
    }
  }

  private func currentObservation(after earlier: Date) throws -> ReceiptRedemptionGlobalReadyV1 {
    let current = try currentObservation()
    guard try current.date() >= earlier else { throw ReceiptRedemptionError.clockRollback }
    return current
  }

  private func bindingExpiryDate(_ binding: ReceiptRedemptionSourceBindingV1) throws -> Date {
    do {
      return try binding.expiryDate()
    } catch {
      throw ReceiptRedemptionError.currentStateUnavailable
    }
  }

  private func existingState(
    lease: any ReceiptRedemptionLease,
    expectedBinding: ReceiptRedemptionSourceBindingV1
  ) throws -> ReceiptRedemptionStateV1? {
    do {
      guard
        let bytes = try lease.loadRedemptionState(
          nonceDigest: expectedBinding.challengeNonceDigest
        )
      else { return nil }
      let state = try ReceiptRedemptionStateV1.decode(bytes)
      // A fixed-slot lookup result is not trusted merely because it occupied
      // the requested location. It must bind every source fact, including
      // nonce and exact receipt digest/identifier, before it can be replayed.
      guard state.binding == expectedBinding else {
        throw ReceiptRedemptionError.pendingRecoveryRequired
      }
      return state
    } catch let error as ReceiptRedemptionError {
      throw error
    } catch {
      throw ReceiptRedemptionError.pendingRecoveryRequired
    }
  }

  private func requiredGlobalState(
    lease: any ReceiptRedemptionLease
  ) throws -> ReceiptRedemptionGlobalStateV1 {
    do {
      guard let bytes = try lease.loadGlobalRedemptionState() else {
        throw ReceiptRedemptionError.globalStateRequired
      }
      let state = try ReceiptRedemptionGlobalStateV1.decode(bytes)
      guard try state.canonicalBytes() == bytes else {
        throw ReceiptRedemptionError.pendingRecoveryRequired
      }
      return state
    } catch let error as ReceiptRedemptionError {
      throw error
    } catch {
      // A malformed fixed global record may be a lost intent. Treat it as
      // recovery rather than reinitializing or treating the nonce as fresh.
      throw ReceiptRedemptionError.pendingRecoveryRequired
    }
  }

  private func preflightReceipt(
    _ receiptData: Data,
    expectedChallenge: ApprovalChallenge,
    now: Date
  ) throws {
    do {
      try PinnedReceiptVerifier.preflight(
        receiptData,
        expectedChallenge: expectedChallenge,
        configuration: configuration,
        now: now
      )
    } catch ReceiptVerificationError.receiptExpired {
      throw ReceiptRedemptionError.expired
    } catch {
      throw ReceiptRedemptionError.currentStateUnavailable
    }
  }

  private func requireExactStateReadback(
    lease: any ReceiptRedemptionLease,
    nonceDigest: String,
    expectedBytes: Data,
    expectedState: ReceiptRedemptionStateV1
  ) throws {
    guard let actual = try lease.loadRedemptionState(nonceDigest: nonceDigest),
      actual == expectedBytes,
      try ReceiptRedemptionStateV1.decode(actual) == expectedState
    else { throw ReceiptRedemptionError.postReservationUnverified }
  }

  private func requireExactGlobalReadback(
    lease: any ReceiptRedemptionLease,
    expectedBytes: Data,
    expectedState: ReceiptRedemptionGlobalStateV1
  ) throws {
    guard let actual = try lease.loadGlobalRedemptionState(),
      actual == expectedBytes,
      try ReceiptRedemptionGlobalStateV1.decode(actual) == expectedState,
      try ReceiptRedemptionGlobalStateV1.decode(actual).canonicalBytes() == expectedBytes
    else { throw ReceiptRedemptionError.postReservationUnverified }
  }

  private func pendingRecord(
    from state: ReceiptRedemptionStateV1
  ) throws -> ReceiptRedemptionPendingV1 {
    guard case .pending(let record) = state else {
      throw ReceiptRedemptionContractError.malformed
    }
    return record
  }

  private func currentStateError(_ error: ReceiptRedemptionFixedSlotError) -> ReceiptRedemptionError
  {
    switch error {
    case .revoked: .currentStateRevoked
    case .unavailable: .currentStateUnavailable
    }
  }
}
