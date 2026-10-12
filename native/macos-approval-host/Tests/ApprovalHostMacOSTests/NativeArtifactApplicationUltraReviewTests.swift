import Foundation
import XCTest

@testable import ApprovalHostCore
@testable import ApprovalHostMacOS

/// These tests use only in-memory review, authentication, cancellation, XPC
/// service, and gate fakes. They do not create an NSApplication, an XPC
/// connection/listener, an LAContext, a Keychain item, or an installed host.
final class NativeArtifactApplicationUltraReviewTests: XCTestCase {
  private let now = Date(timeIntervalSince1970: 1_893_456_060)

  func testInteractionGateDefersReleaseAndStaleReleaseCannotFreeLaterLease() throws {
    let deferred = DeferredReleaseQueue()
    let gate = InstalledApprovalHostInteractionGate { action in deferred.enqueue(action) }
    gate.activate()

    let first = try gate.acquire()
    first.finish()
    XCTAssertFalse(gate.isAvailable)
    XCTAssertThrowsError(try gate.acquire()) { error in
      XCTAssertEqual(error as? ApprovalHostError, .requestAlreadyActive)
    }

    deferred.runAll()
    let second = try gate.acquire()
    XCTAssertFalse(gate.isAvailable)

    // The queued release carries the first generation and cannot clear the
    // current occupant after a new interaction has acquired the gate.
    first.finish()
    deferred.runAll()
    XCTAssertFalse(gate.isAvailable)
    try gate.requireLease(second)

    second.finish()
    deferred.runAll()
    XCTAssertTrue(gate.isAvailable)
  }

  func testInteractionGateRejectsCrossGateAndFinishedLeases() throws {
    let firstGate = InstalledApprovalHostInteractionGate { $0() }
    let secondGate = InstalledApprovalHostInteractionGate { $0() }
    firstGate.activate()
    secondGate.activate()
    let lease = try firstGate.acquire()

    XCTAssertThrowsError(try secondGate.requireLease(lease)) { error in
      XCTAssertEqual(error as? ApprovalHostError, .hostNotInstalled)
    }

    lease.finish()
    XCTAssertThrowsError(try firstGate.requireLease(lease)) { error in
      XCTAssertEqual(error as? ApprovalHostError, .hostNotInstalled)
    }
  }

  @MainActor
  func testStaleEnrollmentControllerRejectsBeforeStoreOrAuthenticator() {
    let store = CountingProtectedKeyStore()
    let authenticator = CountingAuthenticator()
    let controller = AppKitProtectedKeyEnrollmentController(
      store: store,
      authenticator: authenticator,
      requireInteraction: { throw ApprovalHostError.hostNotInstalled }
    )
    var result: Result<Void, ApprovalHostError>?

    _ = controller.requestExplicitEnrollment { result = $0 }

    XCTAssertEqual(failure(result), .hostNotInstalled)
    XCTAssertEqual(store.codeIdentityCalls, 0)
    XCTAssertEqual(authenticator.calls, 0)
  }

  func testRetainedXPCServiceRejectsBusyAndInvalidatedGateBeforeEngine() throws {
    let deferred = DeferredReleaseQueue()
    let gate = InstalledApprovalHostInteractionGate { action in deferred.enqueue(action) }
    gate.activate()
    let reviewer = CountingReviewer()
    let service = ApprovalHostXPCService(
      engine: engine(reviewer: reviewer, authenticator: CountingAuthenticator()),
      interactionGate: gate
    )
    let localLease = try gate.acquire()
    var busyCode: String?

    service.requestApproval(Data("{}".utf8)) { _, code, _ in busyCode = code }

    XCTAssertEqual(busyCode, ApprovalHostError.requestAlreadyActive.code)
    XCTAssertEqual(reviewer.calls, 0)
    localLease.finish()
    deferred.runAll()

    gate.invalidate()
    var stoppedCode: String?
    service.requestApproval(Data("{}".utf8)) { _, code, _ in stoppedCode = code }

    XCTAssertEqual(stoppedCode, ApprovalHostError.hostNotInstalled.code)
    XCTAssertEqual(reviewer.calls, 0)
  }

  func testClosedXPCServiceRejectsItsFirstRequestBeforeGateOrEngine() {
    let gate = InstalledApprovalHostInteractionGate { $0() }
    gate.activate()
    let reviewer = CountingReviewer()
    let service = ApprovalHostXPCService(
      engine: engine(reviewer: reviewer, authenticator: CountingAuthenticator()),
      interactionGate: gate
    )

    // A connection can close before it submits its first request. Its retained
    // service object is terminal and must not later acquire the shared gate.
    service.cancelPending()
    var code: String?
    service.requestApproval(Data("{}".utf8)) { _, responseCode, _ in code = responseCode }

    XCTAssertEqual(code, ApprovalHostError.hostNotInstalled.code)
    XCTAssertTrue(gate.isAvailable)
    XCTAssertEqual(reviewer.calls, 0)
  }

  @MainActor
  func testApplicationRunRejectsNoDesktopBeforeRuntimePreparationOrUI() throws {
    var prepareCalls = 0
    var userInterfaceCalls = 0

    XCTAssertThrowsError(
      try InstalledApprovalHostApplication.run(
        metadata: metadata(),
        desktopAvailable: { false },
        prepareRuntime: { _ in
          prepareCalls += 1
          throw ApprovalHostError.hostNotInstalled
        },
        runUserInterface: { _ in userInterfaceCalls += 1 }
      )
    ) { error in
      XCTAssertEqual(error as? ApprovalHostError, .authenticationUnavailable)
    }

    XCTAssertEqual(prepareCalls, 0)
    XCTAssertEqual(userInterfaceCalls, 0)
  }

  func testCheckedInGeneratedMetadataRemainsExplicitlyUnconfigured() {
    XCTAssertThrowsError(try GeneratedApprovalHostBuildMetadata.make()) { error in
      XCTAssertEqual(error as? ApprovalHostError, .hostNotInstalled)
    }
  }

  @MainActor
  func testApplicationRunPreflightFailureNeverStartsUserInterface() throws {
    var prepareCalls = 0
    var userInterfaceCalls = 0

    XCTAssertThrowsError(
      try InstalledApprovalHostApplication.run(
        metadata: metadata(),
        desktopAvailable: { true },
        prepareRuntime: { _ in
          prepareCalls += 1
          throw ApprovalHostError.hostNotInstalled
        },
        runUserInterface: { _ in userInterfaceCalls += 1 }
      )
    ) { error in
      XCTAssertEqual(error as? ApprovalHostError, .hostNotInstalled)
    }

    XCTAssertEqual(prepareCalls, 1)
    XCTAssertEqual(userInterfaceCalls, 0)
  }

  @MainActor
  func testApplicationRunShutsRuntimeExactlyOnceAfterSuccessOrUIFailure() throws {
    let successListener = TestListener()
    let successRuntime = makeRuntime(listener: successListener)
    successRuntime.activateBeforeReturn()
    var successfulUI = 0

    try InstalledApprovalHostApplication.run(
      metadata: metadata(),
      desktopAvailable: { true },
      prepareRuntime: { _ in successRuntime },
      runUserInterface: { _ in successfulUI += 1 }
    )

    XCTAssertEqual(successfulUI, 1)
    XCTAssertEqual(successListener.activateCount, 1)
    XCTAssertEqual(successListener.invalidateCount, 1)

    let failedListener = TestListener()
    let failedRuntime = makeRuntime(listener: failedListener)
    failedRuntime.activateBeforeReturn()
    var failedUI = 0
    XCTAssertThrowsError(
      try InstalledApprovalHostApplication.run(
        metadata: metadata(),
        desktopAvailable: { true },
        prepareRuntime: { _ in failedRuntime },
        runUserInterface: { _ in
          failedUI += 1
          throw ApprovalHostError.signingUnavailable
        }
      )
    ) { error in
      XCTAssertEqual(error as? ApprovalHostError, .signingUnavailable)
    }

    XCTAssertEqual(failedUI, 1)
    XCTAssertEqual(failedListener.activateCount, 1)
    XCTAssertEqual(failedListener.invalidateCount, 1)
  }

  func testSynchronousReviewCancellationHoldsInteractionUntilPresenterReturns() {
    let reviewer = SynchronousCancelReviewer()
    let authenticator = CountingAuthenticator()
    let finished = TerminalObservation()
    var completions = 0
    let engine = engine(reviewer: reviewer, authenticator: authenticator)

    _ = engine.requestApproval(
      challengeData: fixture("openai-pause-challenge-v1"),
      completion: { result in
        completions += 1
        XCTAssertEqual(self.failure(result), .reviewCancelled)
      },
      interactionFinished: {
        finished.record(
          reviewerReturned: reviewer.didReturn,
          authenticatorReturned: true,
          reviewerCancellationCancelled: reviewer.cancellation.cancelled,
          authenticatorCancellationCancelled: true
        )
      }
    )

    XCTAssertEqual(completions, 1)
    XCTAssertEqual(authenticator.calls, 0)
    XCTAssertEqual(finished.count, 1)
    XCTAssertTrue(finished.allHandoffsReturned)
    XCTAssertTrue(finished.allCancellationsCancelled)
  }

  func testNestedSynchronousAuthenticationCancellationHoldsInteractionUntilBothAdaptersReturn() {
    let reviewer = SynchronousContinueReviewer()
    let authenticator = SynchronousCancelAuthenticator()
    let finished = TerminalObservation()
    var completions = 0
    let engine = engine(reviewer: reviewer, authenticator: authenticator)

    _ = engine.requestApproval(
      challengeData: fixture("openai-pause-challenge-v1"),
      completion: { result in
        completions += 1
        XCTAssertEqual(self.failure(result), .authenticationCancelled)
      },
      interactionFinished: {
        finished.record(
          reviewerReturned: reviewer.didReturn,
          authenticatorReturned: authenticator.didReturn,
          reviewerCancellationCancelled: reviewer.cancellation.cancelled,
          authenticatorCancellationCancelled: authenticator.cancellation.cancelled
        )
      }
    )

    XCTAssertEqual(completions, 1)
    XCTAssertEqual(authenticator.calls, 1)
    XCTAssertEqual(finished.count, 1)
    XCTAssertTrue(finished.allHandoffsReturned)
    XCTAssertTrue(finished.allCancellationsCancelled)
  }

  private func engine(
    reviewer: any ApprovalReviewer,
    authenticator: any FreshAuthenticator
  ) -> ApprovalHostEngine {
    ApprovalHostEngine(
      configuration: configuration(),
      reviewer: reviewer,
      authenticator: authenticator,
      signer: NoopSigner(),
      identifierGenerator: FixedIdentifierGenerator(),
      scheduler: NoopScheduler(),
      clock: { self.now }
    )
  }

  private func configuration() -> TrustedHostConfiguration {
    try! TrustedHostConfiguration(
      hostReference: "host:adhelm-macos-approval-v1",
      signingKeyReference: "approval-key:fixture-v1",
      machServiceName: "com.aptsonic.adhelm.approvalhost",
      expectedClientCodeSigningRequirement: "identifier \"com.aptsonic.adhelm.client\"",
      expectedHostCodeSigningRequirement: "identifier \"com.aptsonic.adhelm.host\"",
      signingKeyApplicationTag: Data("fixture".utf8),
      keyStorage: .keychainP256
    )
  }

  private func metadata() -> ApprovalHostBuildMetadataV1 {
    try! ApprovalHostBuildMetadataV1(
      hostReference: "host:adhelm-macos-approval-v1",
      signingKeyReference: "approval-key:fixture-v1",
      machServiceName: "com.aptsonic.adhelm.approvalhost",
      clientCodeIdentity: try ApprovalHostCodeIdentitySelectorV1(
        role: .client,
        bundleIdentifier: "com.aptsonic.adhelm.client",
        teamIdentifier: "ABCDE12345"
      ),
      hostCodeIdentity: try ApprovalHostCodeIdentitySelectorV1(
        role: .host,
        bundleIdentifier: "com.aptsonic.adhelm.host",
        teamIdentifier: "ABCDE12345"
      ),
      signingKeyApplicationTag: Data("fixture".utf8),
      keyStorage: .keychainP256,
      provenanceService: "com.aptsonic.adhelm.enrollment",
      provenanceAccount: "initial-slot"
    )
  }

  private func makeRuntime(listener: TestListener) -> InstalledApprovalHostRuntime {
    InstalledApprovalHostRuntime(
      configuration: configuration(),
      store: CountingProtectedKeyStore(),
      engine: engine(reviewer: CountingReviewer(), authenticator: CountingAuthenticator()),
      listener: listener,
      operationGate: InstalledApprovalHostOperationGate(),
      interactionGate: InstalledApprovalHostInteractionGate { $0() }
    )
  }

  private func fixture(_ name: String) -> Data {
    let url = Bundle.module.url(
      forResource: name,
      withExtension: "json",
      subdirectory: "Fixtures"
    )!
    return try! Data(contentsOf: url)
  }

  private func failure(
    _ result: Result<SignedApprovalReceipt, ApprovalHostError>?
  ) -> ApprovalHostError? {
    guard case let .failure(error)? = result else { return nil }
    return error
  }

  private func failure(_ result: Result<Void, ApprovalHostError>?) -> ApprovalHostError? {
    guard case let .failure(error)? = result else { return nil }
    return error
  }
}

private final class DeferredReleaseQueue: @unchecked Sendable {
  private var actions: [@Sendable () -> Void] = []

  func enqueue(_ action: @escaping @Sendable () -> Void) {
    actions.append(action)
  }

  func runAll() {
    while !actions.isEmpty {
      let action = actions.removeFirst()
      action()
    }
  }
}

private final class CountingProtectedKeyStore: ProtectedKeyStore, @unchecked Sendable {
  private(set) var codeIdentityCalls = 0

  func validateConfiguredHostCodeIdentity() throws {
    codeIdentityCalls += 1
  }

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

private final class CountingReviewer: ApprovalReviewer {
  private(set) var calls = 0

  func present(
    review _: DeterministicApprovalReview,
    completion _: @escaping (ApprovalReviewDecision) -> Void
  ) -> any ApprovalCancellation {
    calls += 1
    return NoopApprovalCancellation()
  }
}

private final class CountingAuthenticator: FreshAuthenticator {
  private(set) var calls = 0

  func authenticate(
    reason _: String,
    completion _: @escaping (ApprovalAuthenticationResult) -> Void
  ) -> any ApprovalCancellation {
    calls += 1
    return NoopApprovalCancellation()
  }
}

private final class TrackingCancellation: ApprovalCancellation {
  private(set) var cancelled = false
  func cancel() { cancelled = true }
}

private final class SynchronousCancelReviewer: ApprovalReviewer {
  let cancellation = TrackingCancellation()
  private(set) var didReturn = false

  func present(
    review _: DeterministicApprovalReview,
    completion: @escaping (ApprovalReviewDecision) -> Void
  ) -> any ApprovalCancellation {
    completion(.cancel)
    didReturn = true
    return cancellation
  }
}

private final class SynchronousContinueReviewer: ApprovalReviewer {
  let cancellation = TrackingCancellation()
  private(set) var didReturn = false

  func present(
    review _: DeterministicApprovalReview,
    completion: @escaping (ApprovalReviewDecision) -> Void
  ) -> any ApprovalCancellation {
    completion(.continueToAuthentication)
    didReturn = true
    return cancellation
  }
}

private final class SynchronousCancelAuthenticator: FreshAuthenticator {
  let cancellation = TrackingCancellation()
  private(set) var calls = 0
  private(set) var didReturn = false

  func authenticate(
    reason _: String,
    completion: @escaping (ApprovalAuthenticationResult) -> Void
  ) -> any ApprovalCancellation {
    calls += 1
    completion(.cancelled)
    didReturn = true
    return cancellation
  }
}

private final class NoopSigner: ReceiptSigner {
  func sign(
    _: ReceiptSigningPayload,
    authenticatedSession _: any AuthenticatedApprovalSession,
    permit _: any ReceiptSigningPermit,
    completion _: @escaping (Result<Data, ApprovalHostError>) -> Void
  ) -> any ApprovalCancellation {
    NoopApprovalCancellation()
  }
}

private final class NoopScheduler: ApprovalDeadlineScheduler {
  func schedule(
    after _: TimeInterval,
    action _: @escaping () -> Void
  ) -> any ApprovalCancellation {
    NoopApprovalCancellation()
  }
}

private final class FixedIdentifierGenerator: ReceiptIdentifierGenerator {
  func nextReceiptIdentifier() -> String {
    "approval-receipt:" + String(repeating: "A", count: 43)
  }
}

private final class TerminalObservation {
  private(set) var count = 0
  private(set) var allHandoffsReturned = false
  private(set) var allCancellationsCancelled = false

  func record(
    reviewerReturned: Bool,
    authenticatorReturned: Bool,
    reviewerCancellationCancelled: Bool,
    authenticatorCancellationCancelled: Bool
  ) {
    count += 1
    allHandoffsReturned = reviewerReturned && authenticatorReturned
    allCancellationsCancelled = reviewerCancellationCancelled && authenticatorCancellationCancelled
  }
}

private final class TestListener: InstalledApprovalHostListener {
  private(set) var activateCount = 0
  private(set) var invalidateCount = 0

  func activate() { activateCount += 1 }
  func invalidate() { invalidateCount += 1 }
}
