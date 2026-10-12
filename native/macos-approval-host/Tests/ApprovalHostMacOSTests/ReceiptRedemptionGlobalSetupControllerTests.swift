import Foundation
import XCTest

@testable import ApprovalHostCore
@testable import ApprovalHostMacOS

/// Fake-only M37 coverage. These tests do not create AppKit windows,
/// LocalAuthentication contexts, Security/Keychain items, XPC objects, a
/// helper, an installed runtime, or a provider request.
final class ReceiptRedemptionGlobalSetupControllerTests: XCTestCase {
  func testExecutionUsesFixedSetupStoreAndClassifiesRetainedAmbiguity() {
    let store = M37SetupStore()
    let session = M37Session()

    store.nextResult = .success(.initialized)
    XCTAssertEqual(
      ReceiptRedemptionGlobalSetupExecution.perform(
        store: store,
        authenticatedSession: session,
        shouldContinue: { true }
      ),
      .success(.initialized)
    )
    XCTAssertEqual(store.setupCalls, 1)
    XCTAssertTrue(store.lastSession === session)

    store.nextResult = .failure(ProtectedKeyStoreError.recoveryRequired)
    XCTAssertEqual(
      ReceiptRedemptionGlobalSetupExecution.perform(
        store: store,
        authenticatedSession: session,
        shouldContinue: { true }
      ),
      .failure(.enrollmentStateUnverified)
    )

    store.nextResult = .failure(ProtectedKeyStoreError.unavailable)
    XCTAssertEqual(
      ReceiptRedemptionGlobalSetupExecution.perform(
        store: store,
        authenticatedSession: session,
        shouldContinue: { true }
      ),
      .failure(.signingUnavailable)
    )
  }

  func testExecutionNeverCallsStoreBeforeHandoffAndFailsClosedAfterIt() {
    let store = M37SetupStore()
    let session = M37Session()
    var calls = 0

    XCTAssertEqual(
      ReceiptRedemptionGlobalSetupExecution.perform(
        store: store,
        authenticatedSession: session,
        shouldContinue: { false }
      ),
      .failure(.enrollmentStateUnverified)
    )
    XCTAssertEqual(store.setupCalls, 0)

    XCTAssertEqual(
      ReceiptRedemptionGlobalSetupExecution.perform(
        store: store,
        authenticatedSession: session,
        shouldContinue: {
          calls += 1
          return calls == 1
        }
      ),
      .failure(.enrollmentStateUnverified)
    )
    XCTAssertEqual(store.setupCalls, 1)
  }

  func testOperationStateMakesCancellationWinDuringProtectedSetup() {
    let beforeHandoff = ReceiptRedemptionGlobalSetupOperationState()
    XCTAssertEqual(beforeHandoff.cancel(), .reviewCancelled)
    XCTAssertFalse(beforeHandoff.beginProtectedWork())

    let inFlight = ReceiptRedemptionGlobalSetupOperationState()
    XCTAssertTrue(inFlight.beginProtectedWork())
    XCTAssertTrue(inFlight.mayContinueProtectedWork)
    XCTAssertEqual(inFlight.cancel(), .enrollmentStateUnverified)
    XCTAssertFalse(inFlight.mayContinueProtectedWork)
    XCTAssertNil(inFlight.finish(.success(.initialized)))

    let completed = ReceiptRedemptionGlobalSetupOperationState()
    XCTAssertTrue(completed.beginProtectedWork())
    XCTAssertEqual(completed.finish(.success(.alreadyInitialized)), .success(.alreadyInitialized))
    XCTAssertNil(completed.cancel())
  }

  func testRuntimeBoundStoreForwardsOnlyWhileItsGenerationIsLive() throws {
    let rawStore = M37SetupStore()
    let gate = InstalledApprovalHostOperationGate()
    let store = RuntimeBoundProtectedKeyStore(store: rawStore, gate: gate)
    let session = M37Session()

    XCTAssertTrue(store.hasReceiptRedemptionGlobalSetupCapability)
    XCTAssertThrowsError(
      try store.initializeReceiptRedemptionGlobalState(
        authenticatedSession: session,
        shouldContinue: { true }
      )
    ) { error in
      XCTAssertEqual(error as? ProtectedKeyStoreError, .unavailable)
    }
    XCTAssertEqual(rawStore.setupCalls, 0)

    gate.activate()
    let initialized: ReceiptRedemptionGlobalInitializationResult =
      try store
      .initializeReceiptRedemptionGlobalState(
        authenticatedSession: session,
        shouldContinue: { true }
      )
    XCTAssertEqual(initialized, .initialized)
    XCTAssertEqual(rawStore.setupCalls, 1)

    gate.invalidate()
    XCTAssertThrowsError(
      try store.initializeReceiptRedemptionGlobalState(
        authenticatedSession: session,
        shouldContinue: { true }
      )
    ) { error in
      XCTAssertEqual(error as? ProtectedKeyStoreError, .unavailable)
    }
    XCTAssertEqual(rawStore.setupCalls, 1)

    let noCapability = RuntimeBoundProtectedKeyStore(
      store: M37ProtectedOnlyStore(),
      gate: InstalledApprovalHostOperationGate()
    )
    XCTAssertFalse(noCapability.hasReceiptRedemptionGlobalSetupCapability)
  }

  @MainActor
  func testRuntimeConstructsSetupControllerOnlyForItsLiveCapableStore() throws {
    let capableStore = M37SetupStore()
    let capableRuntime = try makeRuntime(store: capableStore)
    defer { capableRuntime.shutdown() }

    let capableLease = try capableRuntime.acquireLocalInteraction()
    _ = try capableRuntime.makeReceiptRedemptionGlobalSetupController(
      interactionLease: capableLease
    )

    capableRuntime.shutdown()
    XCTAssertThrowsError(
      try capableRuntime.makeReceiptRedemptionGlobalSetupController(
        interactionLease: capableLease
      )
    ) { error in
      XCTAssertEqual(error as? ApprovalHostError, .hostNotInstalled)
    }

    let incapableRuntime = try makeRuntime(store: M37ProtectedOnlyStore())
    defer { incapableRuntime.shutdown() }
    let incapableLease = try incapableRuntime.acquireLocalInteraction()
    XCTAssertThrowsError(
      try incapableRuntime.makeReceiptRedemptionGlobalSetupController(
        interactionLease: incapableLease
      )
    ) { error in
      XCTAssertEqual(error as? ApprovalHostError, .hostNotInstalled)
    }
  }

  @MainActor
  func testSetupControllerRejectsStaleInteractionBeforeStoreOrAuthenticator() {
    let store = M37SetupStore()
    let authenticator = M37Authenticator()
    let controller = AppKitReceiptRedemptionGlobalSetupController(
      store: store,
      authenticator: authenticator,
      requireInteraction: { throw ApprovalHostError.hostNotInstalled }
    )
    var result: Result<ReceiptRedemptionGlobalInitializationResult, ApprovalHostError>?

    _ = controller.requestExplicitSetup { result = $0 }

    XCTAssertEqual(failure(result), .hostNotInstalled)
    XCTAssertEqual(store.identityCalls, 0)
    XCTAssertEqual(authenticator.calls, 0)
  }

  private func failure(
    _ result: Result<ReceiptRedemptionGlobalInitializationResult, ApprovalHostError>?
  ) -> ApprovalHostError? {
    guard case .failure(let error)? = result else { return nil }
    return error
  }

  private func makeRuntime(store: any ProtectedKeyStore) throws -> InstalledApprovalHostRuntime {
    let configuration = try TrustedHostConfiguration(
      hostReference: "host:m37-test",
      signingKeyReference: "key:m37-test",
      machServiceName: "com.example.m37-test",
      expectedClientCodeSigningRequirement: "identifier com.example.m37-client",
      expectedHostCodeSigningRequirement: "identifier com.example.m37-host",
      signingKeyApplicationTag: Data("m37-test".utf8),
      keyStorage: .keychainP256,
      initialEnrollment: try InitialEnrollmentConfiguration(
        provenanceService: "com.example.m37.enrollment",
        provenanceAccount: "initial"
      )
    )
    let operationGate = InstalledApprovalHostOperationGate()
    let interactionGate = InstalledApprovalHostInteractionGate(performRelease: { $0() })
    let runtime = InstalledApprovalHostRuntime(
      configuration: configuration,
      store: RuntimeBoundProtectedKeyStore(store: store, gate: operationGate),
      engine: ApprovalHostEngine(
        configuration: configuration,
        reviewer: M37Reviewer(),
        authenticator: M37Authenticator(),
        signer: M37Signer()
      ),
      listener: M37Listener(),
      operationGate: operationGate,
      interactionGate: interactionGate
    )
    runtime.activateBeforeReturn()
    return runtime
  }
}

private final class M37Session: AuthenticatedApprovalSession, @unchecked Sendable {
  private(set) var invalidations = 0
  func invalidate() { invalidations += 1 }
}

private final class M37SetupStore: ProtectedKeyStore, ReceiptRedemptionGlobalSetupStore,
  @unchecked Sendable
{
  var nextResult: Result<ReceiptRedemptionGlobalInitializationResult, Error> = .success(
    .initialized)
  private(set) var identityCalls = 0
  private(set) var setupCalls = 0
  private(set) weak var lastSession: (any AuthenticatedApprovalSession)?

  func validateConfiguredHostCodeIdentity() throws { identityCalls += 1 }

  func enrollInitialPrivateKey(
    authenticatedSession _: LocalAuthenticationApprovalSession,
    shouldContinue _: () -> Bool
  ) throws -> InitialEnrollmentActive {
    throw ProtectedKeyStoreError.unavailable
  }

  func revokeInitialPrivateKey(
    authenticatedSession _: LocalAuthenticationApprovalSession,
    shouldContinue _: () -> Bool
  ) throws -> InitialEnrollmentRevoked {
    throw ProtectedKeyStoreError.unavailable
  }

  func signApprovedReceiptPayload(
    _: ReceiptSigningPayload,
    authenticatedSession _: LocalAuthenticationApprovalSession,
    claimPermit _: () -> Bool
  ) throws -> Data {
    throw ProtectedKeyStoreError.unavailable
  }

  func initializeReceiptRedemptionGlobalState(
    authenticatedSession: any AuthenticatedApprovalSession,
    shouldContinue _: @escaping () -> Bool
  ) throws -> ReceiptRedemptionGlobalInitializationResult {
    setupCalls += 1
    lastSession = authenticatedSession
    return try nextResult.get()
  }
}

private final class M37ProtectedOnlyStore: ProtectedKeyStore, @unchecked Sendable {
  func validateConfiguredHostCodeIdentity() throws {}

  func enrollInitialPrivateKey(
    authenticatedSession _: LocalAuthenticationApprovalSession,
    shouldContinue _: () -> Bool
  ) throws -> InitialEnrollmentActive {
    throw ProtectedKeyStoreError.unavailable
  }

  func revokeInitialPrivateKey(
    authenticatedSession _: LocalAuthenticationApprovalSession,
    shouldContinue _: () -> Bool
  ) throws -> InitialEnrollmentRevoked {
    throw ProtectedKeyStoreError.unavailable
  }

  func signApprovedReceiptPayload(
    _: ReceiptSigningPayload,
    authenticatedSession _: LocalAuthenticationApprovalSession,
    claimPermit _: () -> Bool
  ) throws -> Data {
    throw ProtectedKeyStoreError.unavailable
  }
}

private final class M37Authenticator: FreshAuthenticator {
  private(set) var calls = 0

  func authenticate(
    reason _: String,
    completion _: @escaping (ApprovalAuthenticationResult) -> Void
  ) -> any ApprovalCancellation {
    calls += 1
    return NoopApprovalCancellation()
  }
}

private final class M37Reviewer: ApprovalReviewer {
  func present(
    review _: DeterministicApprovalReview,
    completion _: @escaping (ApprovalReviewDecision) -> Void
  ) -> any ApprovalCancellation {
    NoopApprovalCancellation()
  }
}

private final class M37Signer: ReceiptSigner {
  func sign(
    _: ReceiptSigningPayload,
    authenticatedSession _: any AuthenticatedApprovalSession,
    permit _: any ReceiptSigningPermit,
    completion _: @escaping (Result<Data, ApprovalHostError>) -> Void
  ) -> any ApprovalCancellation {
    NoopApprovalCancellation()
  }
}

private final class M37Listener: InstalledApprovalHostListener {
  func activate() {}
  func invalidate() {}
}
