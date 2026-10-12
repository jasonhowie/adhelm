import ApprovalHostCore
import Foundation
@preconcurrency import LocalAuthentication
@preconcurrency import Security

/// The only persistence surface available to the M31 receipt-redemption
/// coordinator when it is backed by the native fixed slot. This is deliberately
/// not a generic Keychain interface: it cannot enumerate, delete, migrate, or
/// accept a caller-selected service, account, query, or key. Its only add is
/// the exact schema-v2 genesis state used by the explicit local initializer.
///
/// Every method receives the adapter's captured enrollment session, never a
/// session supplied by a redemption request. Implementations must keep all
/// operations bounded and fail closed on a changed or absent readback.
protocol ReceiptRedemptionFixedSlotStore: AnyObject, Sendable {
  func loadGlobalState(session: any EnrollmentProtectedSession) throws -> Data?
  /// Adds only the explicit schema-v2 setup genesis global state. This is not
  /// a generic add operation and must fail if the item already exists.
  func addInitialGlobalState(
    _ canonicalState: Data,
    session: any EnrollmentProtectedSession
  ) throws
  func loadNonceState(
    nonceDigest: String,
    session: any EnrollmentProtectedSession
  ) throws -> Data?
  func addNonceState(
    nonceDigest: String,
    canonicalState: Data,
    session: any EnrollmentProtectedSession
  ) throws
  func replaceNonceState(
    nonceDigest: String,
    expected: Data,
    replacement: Data,
    session: any EnrollmentProtectedSession
  ) throws
  func replaceGlobalState(
    expected: Data,
    replacement: Data,
    session: any EnrollmentProtectedSession
  ) throws
}

/// Existing M31 test-only stores do not gain setup authority merely by
/// conforming to the fixed-slot protocol. The M36 initializer requires a
/// concrete implementation of this method; the default always fails closed.
extension ReceiptRedemptionFixedSlotStore {
  func addInitialGlobalState(
    _: Data,
    session _: any EnrollmentProtectedSession
  ) throws {
    throw ReceiptRedemptionFixedSlotError.unavailable
  }
}

/// A module-internal adapter from the M31 generic fixed-slot coordinator
/// protocol to one existing `InitialEnrollmentCoordinator` lock. Construction
/// binds a single reviewed configuration and a single object-identical fresh
/// session. It remains unwired to runtime, XPC, CLI, Keychain initialization,
/// or Python execution.
final class ReceiptRedemptionFixedSlotAdapter: ReceiptRedemptionFixedSlotCoordinator,
  @unchecked Sendable
{
  private let configuration: TrustedHostConfiguration
  private let enrollmentCoordinator: InitialEnrollmentCoordinator
  private let store: any ReceiptRedemptionFixedSlotStore
  private let enrollmentSession: any EnrollmentProtectedSession
  private let redemptionSessionIdentifier: ObjectIdentifier
  private let enrollmentSessionIdentifier: ObjectIdentifier
  private let shouldContinue: () -> Bool

  init(
    configuration: TrustedHostConfiguration,
    enrollmentCoordinator: InitialEnrollmentCoordinator,
    store: any ReceiptRedemptionFixedSlotStore,
    enrollmentSession: any EnrollmentProtectedSession,
    redemptionSession: any ReceiptRedemptionSession,
    shouldContinue: @escaping () -> Bool = { true }
  ) throws {
    guard enrollmentCoordinator.hasExactConfiguration(configuration),
      ObjectIdentifier(enrollmentSession) == ObjectIdentifier(redemptionSession)
    else { throw ReceiptRedemptionFixedSlotError.unavailable }
    self.configuration = configuration
    self.enrollmentCoordinator = enrollmentCoordinator
    self.store = store
    self.enrollmentSession = enrollmentSession
    enrollmentSessionIdentifier = ObjectIdentifier(enrollmentSession)
    redemptionSessionIdentifier = ObjectIdentifier(redemptionSession)
    self.shouldContinue = shouldContinue
  }

  func withRedemptionLease<T>(
    _ operation: (any ReceiptRedemptionLease) throws -> T
  ) throws -> T {
    guard shouldContinue() else { throw ReceiptRedemptionFixedSlotError.unavailable }
    do {
      return try enrollmentCoordinator.withReceiptRedemptionLease(
        session: enrollmentSession,
        shouldContinue: shouldContinue
      ) { verificationLease, setupEvidence in
        let lease = ReceiptRedemptionFixedSlotLease(
          configuration: configuration,
          verificationLease: verificationLease,
          setupEvidence: setupEvidence,
          store: store,
          enrollmentSession: enrollmentSession,
          enrollmentSessionIdentifier: enrollmentSessionIdentifier,
          redemptionSessionIdentifier: redemptionSessionIdentifier,
          shouldContinue: shouldContinue
        )
        defer { lease.invalidate() }
        return try operation(lease)
      }
    } catch let error as ReceiptRedemptionFixedSlotError {
      throw error
    } catch let error as ReceiptRedemptionError {
      // The M31 coordinator classifies failures after INTENT_PENDING as
      // inspect-before-repeat ambiguity. Preserve that domain result rather
      // than recasting it as a pre-reservation current-state failure.
      throw error
    } catch {
      // The enrollment/key boundary deliberately exposes no Security,
      // provenance, code-identity, or store detail to M31.
      throw ReceiptRedemptionFixedSlotError.unavailable
    }
  }
}

/// A callback-scoped M31 lease. It is created only inside
/// `withRedemptionLease` and becomes unusable in that method's `defer`, even if
/// the callback throws. Its state operations always use the captured session.
private final class ReceiptRedemptionFixedSlotLease: ReceiptRedemptionLease,
  @unchecked Sendable
{
  private let configuration: TrustedHostConfiguration
  private let verificationLease: InitialEnrollmentReceiptVerificationLease
  private let setupEvidence: ReceiptRedemptionGlobalSetupEvidence
  private let store: any ReceiptRedemptionFixedSlotStore
  private let enrollmentSession: any EnrollmentProtectedSession
  private let enrollmentSessionIdentifier: ObjectIdentifier
  private let redemptionSessionIdentifier: ObjectIdentifier
  private let shouldContinue: () -> Bool
  private let ownerThreadIdentifier: ObjectIdentifier
  private let stateLock = NSLock()
  private var active = true

  init(
    configuration: TrustedHostConfiguration,
    verificationLease: InitialEnrollmentReceiptVerificationLease,
    setupEvidence: ReceiptRedemptionGlobalSetupEvidence,
    store: any ReceiptRedemptionFixedSlotStore,
    enrollmentSession: any EnrollmentProtectedSession,
    enrollmentSessionIdentifier: ObjectIdentifier,
    redemptionSessionIdentifier: ObjectIdentifier,
    shouldContinue: @escaping () -> Bool
  ) {
    self.configuration = configuration
    self.verificationLease = verificationLease
    self.setupEvidence = setupEvidence
    self.store = store
    self.enrollmentSession = enrollmentSession
    self.enrollmentSessionIdentifier = enrollmentSessionIdentifier
    self.redemptionSessionIdentifier = redemptionSessionIdentifier
    self.shouldContinue = shouldContinue
    ownerThreadIdentifier = ObjectIdentifier(Thread.current)
  }

  func verifyCurrentReceipt(
    _ receiptData: Data,
    expectedChallenge: ApprovalChallenge,
    session: any ReceiptRedemptionSession,
    now: Date
  ) throws -> ReceiptRedemptionCurrentReceipt {
    try withActiveOperation {
      guard ObjectIdentifier(session) == redemptionSessionIdentifier,
        let protectedSession = session as? any EnrollmentProtectedSession,
        ObjectIdentifier(protectedSession) == enrollmentSessionIdentifier
      else { throw ReceiptRedemptionFixedSlotError.unavailable }
      do {
        let receiptSnapshot = try snapshotReceipt(receiptData)
        let current = try verificationLease.verifyCurrentReceipt(
          receiptData,
          expectedChallenge: expectedChallenge,
          session: enrollmentSession,
          now: now
        )
        // The M31 coordinator also checks this pair before reservation and
        // promotion. Keeping the exact source binding here prevents a concrete
        // store/lease substitution from treating a syntactically valid receipt
        // as current-key evidence.
        guard
          try ReceiptRedemptionSourceBindingV1(
            configuration: configuration,
            expectedChallenge: expectedChallenge,
            canonicalReceipt: current.receipt.canonicalReceipt
          ).matchesVerifiedReceipt(
            current.receipt,
            configuration: configuration,
            expectedChallenge: expectedChallenge
          ),
          current.receipt.canonicalReceipt == receiptSnapshot
        else { throw ReceiptRedemptionFixedSlotError.unavailable }
        return current
      } catch let error as ReceiptRedemptionFixedSlotError {
        throw error
      } catch let error as ProtectedKeyStoreError {
        switch error {
        case .revoked:
          throw ReceiptRedemptionFixedSlotError.revoked
        default:
          throw ReceiptRedemptionFixedSlotError.unavailable
        }
      } catch {
        throw ReceiptRedemptionFixedSlotError.unavailable
      }
    }
  }

  func loadRedemptionState(nonceDigest: String) throws -> Data? {
    try withActiveOperation {
      let digest = try checkedNonceDigest(nonceDigest)
      do {
        return try store.loadNonceState(nonceDigest: digest, session: enrollmentSession)
          .map(snapshotState)
      } catch let error as ReceiptRedemptionFixedSlotError {
        throw error
      } catch {
        throw ReceiptRedemptionFixedSlotError.unavailable
      }
    }
  }

  func loadGlobalRedemptionState() throws -> Data? {
    try withActiveOperation {
      do {
        guard let raw = try store.loadGlobalState(session: enrollmentSession) else { return nil }
        let bytes = try snapshotState(raw)
        let state = try ReceiptRedemptionGlobalStateV1.decode(bytes)
        guard try state.canonicalBytes() == bytes else {
          throw ReceiptRedemptionFixedSlotError.unavailable
        }
        // Validate the provenance-bound READY baseline before M31 starts its
        // own READY -> INTENT_PENDING transition. During an in-flight M31
        // reservation the coordinator itself owns exact INTENT binding and
        // recovery classification, so this adapter must pass that state back
        // rather than falsely treating it as a replacement genesis record.
        if case .ready = state {
          _ = try ReceiptRedemptionGlobalInitialization.requirePresentCompleteGlobal(
            bytes,
            configuration: configuration,
            active: setupEvidence.active,
            observedPublicKeyX963SHA256: setupEvidence.observedPublicKeyX963SHA256
          )
        }
        return bytes
      } catch let error as ReceiptRedemptionFixedSlotError {
        throw error
      } catch {
        throw ReceiptRedemptionFixedSlotError.unavailable
      }
    }
  }

  func writePendingRedemption(_ canonicalState: Data) throws {
    try withActiveOperation {
      let (bytes, state) = try checkedNonceState(canonicalState)
      guard case .pending(let pending) = state else {
        throw ReceiptRedemptionFixedSlotError.unavailable
      }
      let nonceDigest = try checkedNonceDigest(pending.binding.challengeNonceDigest)
      do {
        try store.addNonceState(
          nonceDigest: nonceDigest,
          canonicalState: bytes,
          session: enrollmentSession
        )
        guard
          let actual = try store.loadNonceState(
            nonceDigest: nonceDigest,
            session: enrollmentSession
          ), try snapshotState(actual) == bytes
        else { throw ReceiptRedemptionFixedSlotError.unavailable }
      } catch let error as ReceiptRedemptionFixedSlotError {
        throw error
      } catch {
        throw ReceiptRedemptionFixedSlotError.unavailable
      }
    }
  }

  func replacePendingRedemption(
    _ expectedPending: Data,
    with canonicalVerified: Data
  ) throws {
    try withActiveOperation {
      let (expectedBytes, expectedState) = try checkedNonceState(expectedPending)
      let (verifiedBytes, verifiedState) = try checkedNonceState(canonicalVerified)
      guard case .pending(let pending) = expectedState,
        case .verified(let verified) = verifiedState,
        verified.pending.binding == pending.binding,
        verified.pending.reservedAtText == pending.reservedAtText
      else { throw ReceiptRedemptionFixedSlotError.unavailable }
      let nonceDigest = try checkedNonceDigest(pending.binding.challengeNonceDigest)
      do {
        try store.replaceNonceState(
          nonceDigest: nonceDigest,
          expected: expectedBytes,
          replacement: verifiedBytes,
          session: enrollmentSession
        )
        guard
          let actual = try store.loadNonceState(
            nonceDigest: nonceDigest,
            session: enrollmentSession
          ), try snapshotState(actual) == verifiedBytes
        else { throw ReceiptRedemptionFixedSlotError.unavailable }
      } catch let error as ReceiptRedemptionFixedSlotError {
        throw error
      } catch {
        throw ReceiptRedemptionFixedSlotError.unavailable
      }
    }
  }

  func replaceGlobalRedemptionState(_ expected: Data, with canonicalState: Data) throws {
    try withActiveOperation {
      let (expectedBytes, expectedState) = try checkedGlobalState(expected)
      let (replacementBytes, replacementState) = try checkedGlobalState(canonicalState)
      let legalTransition: Bool
      do {
        switch (expectedState, replacementState) {
        case (.ready, .intentPending(let intent)):
          legalTransition =
            try expectedState.nextIntent(
              binding: intent.binding,
              observedAtText: intent.observedAtText
            ) == replacementState
        case (.intentPending(let intent), .ready(let ready)):
          legalTransition =
            try expectedState.nextReady(
              expectedBinding: intent.binding,
              observedAtText: ready.observedAtText
            ) == replacementState
        default:
          legalTransition = false
        }
      } catch {
        throw ReceiptRedemptionFixedSlotError.unavailable
      }
      guard legalTransition else { throw ReceiptRedemptionFixedSlotError.unavailable }
      do {
        try store.replaceGlobalState(
          expected: expectedBytes,
          replacement: replacementBytes,
          session: enrollmentSession
        )
        guard let actual = try store.loadGlobalState(session: enrollmentSession),
          try snapshotState(actual) == replacementBytes
        else { throw ReceiptRedemptionFixedSlotError.unavailable }
      } catch let error as ReceiptRedemptionFixedSlotError {
        throw error
      } catch {
        throw ReceiptRedemptionFixedSlotError.unavailable
      }
    }
  }

  func invalidate() {
    stateLock.lock()
    active = false
    stateLock.unlock()
  }

  private func withActiveOperation<T>(_ operation: () throws -> T) throws -> T {
    stateLock.lock()
    defer { stateLock.unlock() }
    guard active,
      ObjectIdentifier(Thread.current) == ownerThreadIdentifier,
      shouldContinue()
    else { throw ReceiptRedemptionFixedSlotError.unavailable }
    return try operation()
  }

  private func checkedNonceState(_ value: Data) throws -> (Data, ReceiptRedemptionStateV1) {
    let bytes = try snapshotState(value)
    do {
      let state = try ReceiptRedemptionStateV1.decode(bytes)
      guard try state.canonicalBytes() == bytes else {
        throw ReceiptRedemptionFixedSlotError.unavailable
      }
      return (bytes, state)
    } catch let error as ReceiptRedemptionFixedSlotError {
      throw error
    } catch {
      throw ReceiptRedemptionFixedSlotError.unavailable
    }
  }

  private func checkedGlobalState(
    _ value: Data
  ) throws -> (Data, ReceiptRedemptionGlobalStateV1) {
    let bytes = try snapshotState(value)
    do {
      let state = try ReceiptRedemptionGlobalStateV1.decode(bytes)
      guard try state.canonicalBytes() == bytes else {
        throw ReceiptRedemptionFixedSlotError.unavailable
      }
      return (bytes, state)
    } catch let error as ReceiptRedemptionFixedSlotError {
      throw error
    } catch {
      throw ReceiptRedemptionFixedSlotError.unavailable
    }
  }

  private func checkedNonceDigest(_ value: String) throws -> String {
    guard value.utf8.count == 64,
      value.utf8.allSatisfy({ (48...57).contains($0) || (97...102).contains($0) })
    else { throw ReceiptRedemptionFixedSlotError.unavailable }
    return value
  }

  private func snapshotReceipt(_ value: Data) throws -> Data {
    guard !value.isEmpty, value.count <= 16_384,
      let snapshot = value.withUnsafeBytes({ bytes -> Data? in
        guard let base = bytes.baseAddress else { return nil }
        return Data(bytes: base, count: bytes.count)
      })
    else { throw ReceiptRedemptionFixedSlotError.unavailable }
    return snapshot
  }

  private func snapshotState(_ value: Data) throws -> Data {
    guard !value.isEmpty,
      value.count <= ReceiptRedemptionStateV1.maximumCanonicalBytes,
      let snapshot = value.withUnsafeBytes({ bytes -> Data? in
        guard let base = bytes.baseAddress else { return nil }
        return Data(bytes: base, count: bytes.count)
      })
    else { throw ReceiptRedemptionFixedSlotError.unavailable }
    return snapshot
  }
}

/// Domain-separated private selectors for exactly one reviewed enrollment/key
/// configuration. They are never decoded from a request or returned to an
/// external caller. The Keychain implementation below is intentionally
/// unwired in this increment.
struct ReceiptRedemptionFixedSlotSelectorDerivation: Equatable, Sendable {
  let slotDigest: String
  let globalSelector: ReceiptRedemptionFixedSlotKeychainSelector
  private let nonceAccountPrefix: String

  init(configuration: TrustedHostConfiguration) throws {
    guard let enrollment = configuration.initialEnrollment else {
      throw ReceiptRedemptionFixedSlotError.unavailable
    }
    let value: CanonicalJSONValue = .object([
      "scope": .string("ADHELM_M33_RECEIPT_REDEMPTION_FIXED_SLOT_SELECTOR_V1"),
      "host_ref": .string(configuration.hostReference),
      "signing_key_ref": .string(configuration.signingKeyReference),
      "mach_service": .string(configuration.machServiceName),
      "client_requirement": .string(configuration.expectedClientCodeSigningRequirement),
      "host_requirement": .string(configuration.expectedHostCodeSigningRequirement),
      "key_application_tag_digest": .string(
        CanonicalJSON.sha256Hex(configuration.signingKeyApplicationTag)),
      "key_access_group": configuration.signingKeyAccessGroup.map(CanonicalJSONValue.string)
        ?? .null,
      "key_storage": .string(configuration.keyStorage.rawValue),
      "provenance_service": .string(enrollment.provenanceService),
      "provenance_account": .string(enrollment.provenanceAccount),
      "protocol_configuration_digest": .string(
        configuration.approvalProtocolConfigurationV1.configurationDigest),
    ])
    let bytes: Data
    do {
      bytes = try CanonicalJSON.encode(value)
    } catch {
      throw ReceiptRedemptionFixedSlotError.unavailable
    }
    let digest = CanonicalJSON.sha256Hex(bytes)
    slotDigest = digest
    globalSelector = ReceiptRedemptionFixedSlotKeychainSelector(
      service: "adhelm.native-approval-redemption-v1",
      account: "global:\(digest)",
      accessGroup: configuration.signingKeyAccessGroup
    )
    nonceAccountPrefix = "nonce:\(digest):"
  }

  func nonceSelector(nonceDigest: String) throws -> ReceiptRedemptionFixedSlotKeychainSelector {
    guard nonceDigest.utf8.count == 64,
      nonceDigest.utf8.allSatisfy({ (48...57).contains($0) || (97...102).contains($0) })
    else { throw ReceiptRedemptionFixedSlotError.unavailable }
    return ReceiptRedemptionFixedSlotKeychainSelector(
      service: globalSelector.service,
      account: nonceAccountPrefix + nonceDigest,
      accessGroup: globalSelector.accessGroup
    )
  }
}

/// A closed private Keychain selector. It has no runtime parser or public
/// constructor; only the domain-separated derivation above can make one.
struct ReceiptRedemptionFixedSlotKeychainSelector: Equatable, Sendable {
  fileprivate let service: String
  fileprivate let account: String
  fileprivate let accessGroup: String?
}

/// Concrete fixed-selector Keychain adapter, intentionally not constructed by
/// any runtime, XPC, CLI, or bootstrap path in M33. It is included only to make
/// the eventual protected-store contract reviewable. Its read/compare/update
/// sequence is serialized by the already-held enrollment coordinator lock; it
/// is not a Keychain CAS claim and says nothing about another entitled process.
final class SecurityReceiptRedemptionFixedSlotStore: ReceiptRedemptionFixedSlotStore,
  @unchecked Sendable
{
  private let selectors: ReceiptRedemptionFixedSlotSelectorDerivation

  init(configuration: TrustedHostConfiguration) throws {
    selectors = try ReceiptRedemptionFixedSlotSelectorDerivation(configuration: configuration)
  }

  func loadGlobalState(session: any EnrollmentProtectedSession) throws -> Data? {
    try read(selectors.globalSelector, session: session)
  }

  func addInitialGlobalState(
    _ canonicalState: Data,
    session: any EnrollmentProtectedSession
  ) throws {
    let state = try ReceiptRedemptionGlobalStateV1.decode(canonicalState)
    guard case .ready(let ready) = state,
      ready.generation == 1,
      try state.canonicalBytes() == canonicalState
    else { throw ReceiptRedemptionFixedSlotError.unavailable }
    let bytes = try boundedState(canonicalState)
    let context = try localContext(from: session)
    var accessControlError: Unmanaged<CFError>?
    guard
      let accessControl = SecAccessControlCreateWithFlags(
        nil,
        kSecAttrAccessibleWhenUnlockedThisDeviceOnly,
        [.userPresence],
        &accessControlError
      )
    else { throw ReceiptRedemptionFixedSlotError.unavailable }
    var attributes = baseQuery(selectors.globalSelector)
    attributes[kSecValueData] = bytes
    attributes[kSecAttrAccessControl] = accessControl
    attributes[kSecUseAuthenticationContext] = context
    guard SecItemAdd(attributes as CFDictionary, nil) == errSecSuccess,
      try read(selectors.globalSelector, session: session) == bytes
    else { throw ReceiptRedemptionFixedSlotError.unavailable }
  }

  func loadNonceState(
    nonceDigest: String,
    session: any EnrollmentProtectedSession
  ) throws -> Data? {
    try read(try selectors.nonceSelector(nonceDigest: nonceDigest), session: session)
  }

  func addNonceState(
    nonceDigest: String,
    canonicalState: Data,
    session: any EnrollmentProtectedSession
  ) throws {
    let selector = try selectors.nonceSelector(nonceDigest: nonceDigest)
    let state = try boundedState(canonicalState)
    let context = try localContext(from: session)
    var accessControlError: Unmanaged<CFError>?
    guard
      let accessControl = SecAccessControlCreateWithFlags(
        nil,
        kSecAttrAccessibleWhenUnlockedThisDeviceOnly,
        [.userPresence],
        &accessControlError
      )
    else { throw ReceiptRedemptionFixedSlotError.unavailable }
    var attributes = baseQuery(selector)
    attributes[kSecValueData] = state
    attributes[kSecAttrAccessControl] = accessControl
    attributes[kSecUseAuthenticationContext] = context
    guard SecItemAdd(attributes as CFDictionary, nil) == errSecSuccess,
      try read(selector, session: session) == state
    else { throw ReceiptRedemptionFixedSlotError.unavailable }
  }

  func replaceNonceState(
    nonceDigest: String,
    expected: Data,
    replacement: Data,
    session: any EnrollmentProtectedSession
  ) throws {
    try compareAndReplace(
      selector: try selectors.nonceSelector(nonceDigest: nonceDigest),
      expected: expected,
      replacement: replacement,
      session: session
    )
  }

  func replaceGlobalState(
    expected: Data,
    replacement: Data,
    session: any EnrollmentProtectedSession
  ) throws {
    try compareAndReplace(
      selector: selectors.globalSelector,
      expected: expected,
      replacement: replacement,
      session: session
    )
  }

  private func compareAndReplace(
    selector: ReceiptRedemptionFixedSlotKeychainSelector,
    expected: Data,
    replacement: Data,
    session: any EnrollmentProtectedSession
  ) throws {
    let expectedBytes = try boundedState(expected)
    let replacementBytes = try boundedState(replacement)
    guard try read(selector, session: session) == expectedBytes else {
      throw ReceiptRedemptionFixedSlotError.unavailable
    }
    let context = try localContext(from: session)
    var query = baseQuery(selector)
    query[kSecUseAuthenticationContext] = context
    let update: [CFString: Any] = [kSecValueData: replacementBytes]
    guard SecItemUpdate(query as CFDictionary, update as CFDictionary) == errSecSuccess,
      try read(selector, session: session) == replacementBytes
    else { throw ReceiptRedemptionFixedSlotError.unavailable }
  }

  private func read(
    _ selector: ReceiptRedemptionFixedSlotKeychainSelector,
    session: any EnrollmentProtectedSession
  ) throws -> Data? {
    let context = try localContext(from: session)
    var query = baseQuery(selector)
    query[kSecReturnData] = true
    query[kSecMatchLimit] = kSecMatchLimitOne
    query[kSecUseAuthenticationContext] = context
    var result: CFTypeRef?
    let status = SecItemCopyMatching(query as CFDictionary, &result)
    if status == errSecItemNotFound { return nil }
    guard status == errSecSuccess, let data = result as? Data else {
      throw ReceiptRedemptionFixedSlotError.unavailable
    }
    return try boundedState(data)
  }

  private func baseQuery(
    _ selector: ReceiptRedemptionFixedSlotKeychainSelector
  ) -> [CFString: Any] {
    var query: [CFString: Any] = [
      kSecClass: kSecClassGenericPassword,
      kSecAttrService: selector.service,
      kSecAttrAccount: selector.account,
    ]
    if let accessGroup = selector.accessGroup {
      query[kSecAttrAccessGroup] = accessGroup
    }
    return query
  }

  private func localContext(from session: any EnrollmentProtectedSession) throws -> LAContext {
    guard let local = session as? LocalAuthenticationApprovalSession else {
      throw ReceiptRedemptionFixedSlotError.unavailable
    }
    local.context.interactionNotAllowed = true
    return local.context
  }

  private func boundedState(_ value: Data) throws -> Data {
    guard !value.isEmpty,
      value.count <= ReceiptRedemptionStateV1.maximumCanonicalBytes,
      let snapshot = value.withUnsafeBytes({ bytes -> Data? in
        guard let base = bytes.baseAddress else { return nil }
        return Data(bytes: base, count: bytes.count)
      })
    else { throw ReceiptRedemptionFixedSlotError.unavailable }
    return snapshot
  }
}
