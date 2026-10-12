import Foundation
import XCTest

@testable import ApprovalHostCore

/// Independent state-machine checks with only fake review, authentication,
/// signing, and deadline adapters. They do not use XPC, AppKit,
/// LocalAuthentication, Security, Keychain, or provider APIs.
final class IndependentApprovalHostReviewTests: XCTestCase {
  private let initialNow = Date(timeIntervalSince1970: 1_893_456_060)

  func testSigningPermitCannotBeClaimedWhenClockCrossesExpiryAtConsumeBoundary() throws {
    let challengeData = try fixture("openai-pause-challenge-v1")
    let challenge = try ApprovalChallenge.decode(challengeData, now: initialNow)
    let reviewer = IndependentReviewer()
    let authenticator = IndependentAuthenticator()
    var clockAtClaim: [Date] = []
    let signer = BoundaryClaimSigner {
      // The first read is the request's phase/expiry guard. The second
      // occurs inside the engine consume closure immediately before the
      // protected signer would operate.
      clockAtClaim = [challenge.expiresAt.addingTimeInterval(-0.001), challenge.expiresAt]
    }
    let engine = ApprovalHostEngine(
      configuration: try configuration(),
      reviewer: reviewer,
      authenticator: authenticator,
      signer: signer,
      identifierGenerator: IndependentFixedIdentifierGenerator(),
      scheduler: IndependentScheduler(),
      clock: {
        if !clockAtClaim.isEmpty {
          return clockAtClaim.removeFirst()
        }
        return self.initialNow
      }
    )
    var result: Result<SignedApprovalReceipt, ApprovalHostError>?

    _ = engine.requestApproval(challengeData: challengeData) { result = $0 }
    reviewer.approve(at: 0)
    authenticator.succeed(IndependentSession())

    XCTAssertEqual(signer.claimResult, false)
    XCTAssertNotNil(failure(result))
  }

  func testChallengeExpiredBeforeActiveRegistrationNeverReachesReview() throws {
    let challengeData = try fixture("openai-pause-challenge-v1")
    let challenge = try ApprovalChallenge.decode(challengeData, now: initialNow)
    let reviewer = IndependentReviewer()
    let authenticator = IndependentAuthenticator()
    let signer = IndependentDeferredSigner()
    let clock = IndependentSequentialClock(
      values: [initialNow, challenge.expiresAt],
      fallback: challenge.expiresAt
    )
    let engine = ApprovalHostEngine(
      configuration: try configuration(),
      reviewer: reviewer,
      authenticator: authenticator,
      signer: signer,
      identifierGenerator: IndependentFixedIdentifierGenerator(),
      scheduler: IndependentScheduler(),
      clock: clock.next
    )
    var result: Result<SignedApprovalReceipt, ApprovalHostError>?

    _ = engine.requestApproval(challengeData: challengeData) { result = $0 }

    XCTAssertEqual(failure(result), .challengeExpired)
    XCTAssertEqual(reviewer.calls, 0)
    XCTAssertEqual(authenticator.calls, 0)
    XCTAssertEqual(signer.calls, 0)
  }

  func testChallengeExpiredBetweenRegistrationAndStartNeverReachesReview() throws {
    let challengeData = try fixture("openai-pause-challenge-v1")
    let challenge = try ApprovalChallenge.decode(challengeData, now: initialNow)
    let reviewer = IndependentReviewer()
    let authenticator = IndependentAuthenticator()
    let signer = IndependentDeferredSigner()
    let clock = IndependentSequentialClock(
      // Decode and the engine-lock guard pass. The request's own start
      // handoff reaches the exact expiry before a scheduler can run.
      values: [initialNow, initialNow, challenge.expiresAt],
      fallback: challenge.expiresAt
    )
    let engine = ApprovalHostEngine(
      configuration: try configuration(),
      reviewer: reviewer,
      authenticator: authenticator,
      signer: signer,
      identifierGenerator: IndependentFixedIdentifierGenerator(),
      scheduler: IndependentScheduler(),
      clock: clock.next
    )
    var result: Result<SignedApprovalReceipt, ApprovalHostError>?

    _ = engine.requestApproval(challengeData: challengeData) { result = $0 }

    XCTAssertEqual(failure(result), .challengeExpired)
    XCTAssertEqual(reviewer.calls, 0)
    XCTAssertEqual(authenticator.calls, 0)
    XCTAssertEqual(signer.calls, 0)
  }

  func testLateAuthenticationAfterTimeoutIsInvalidatedWithoutSigningAndDoesNotConsumeNonce() throws
  {
    let reviewer = IndependentReviewer()
    let authenticator = IndependentAuthenticator()
    let signer = IndependentDeferredSigner()
    let scheduler = IndependentScheduler()
    let engine = ApprovalHostEngine(
      configuration: try configuration(),
      reviewer: reviewer,
      authenticator: authenticator,
      signer: signer,
      identifierGenerator: IndependentFixedIdentifierGenerator(),
      scheduler: scheduler,
      clock: { self.initialNow }
    )
    let challenge = try fixture("openai-pause-challenge-v1")
    var timedOut: Result<SignedApprovalReceipt, ApprovalHostError>?

    _ = engine.requestApproval(challengeData: challenge) { timedOut = $0 }
    reviewer.approve(at: 0)
    scheduler.fire(at: 0)
    let lateSession = IndependentSession()
    authenticator.succeed(lateSession)

    XCTAssertEqual(failure(timedOut), .timedOut)
    XCTAssertTrue(lateSession.invalidated)
    XCTAssertEqual(signer.calls, 0)

    var retry: Result<SignedApprovalReceipt, ApprovalHostError>?
    _ = engine.requestApproval(challengeData: challenge) { retry = $0 }
    XCTAssertNil(retry)
    XCTAssertEqual(reviewer.calls, 2)
  }

  func testLateReviewCallbackCannotAdvanceANewerSameNonceGeneration() throws {
    let reviewer = IndependentReviewer()
    let authenticator = IndependentAuthenticator()
    let engine = ApprovalHostEngine(
      configuration: try configuration(),
      reviewer: reviewer,
      authenticator: authenticator,
      signer: IndependentDeferredSigner(),
      identifierGenerator: IndependentFixedIdentifierGenerator(),
      scheduler: IndependentScheduler(),
      clock: { self.initialNow }
    )
    let challenge = try fixture("openai-pause-challenge-v1")

    let first = engine.requestApproval(challengeData: challenge) { _ in }
    first.cancel()
    var second: Result<SignedApprovalReceipt, ApprovalHostError>?
    let secondHandle = engine.requestApproval(challengeData: challenge) { second = $0 }
    reviewer.approve(at: 0)

    XCTAssertEqual(authenticator.calls, 0)
    XCTAssertNil(second)

    reviewer.approve(at: 1)
    XCTAssertEqual(authenticator.calls, 1)
    secondHandle.cancel()
    XCTAssertEqual(failure(second), .authenticationCancelled)
  }

  private func fixture(_ name: String) throws -> Data {
    let url = try XCTUnwrap(
      Bundle.module.url(forResource: name, withExtension: "json", subdirectory: "Fixtures")
    )
    return try Data(contentsOf: url)
  }

  private func configuration() throws -> TrustedHostConfiguration {
    try TrustedHostConfiguration(
      hostReference: "host:adhelm-macos-approval-v1",
      signingKeyReference: "approval-key:independent-review",
      machServiceName: "invalid.independent.review",
      expectedClientCodeSigningRequirement: "never",
      expectedHostCodeSigningRequirement: "never",
      signingKeyApplicationTag: Data("independent-review".utf8),
      keyStorage: .keychainP256
    )
  }

  private func failure(
    _ result: Result<SignedApprovalReceipt, ApprovalHostError>?
  ) -> ApprovalHostError? {
    guard case .failure(let error)? = result else { return nil }
    return error
  }
}

private final class IndependentReviewer: ApprovalReviewer {
  private var completions: [(ApprovalReviewDecision) -> Void] = []
  private(set) var calls = 0

  func present(
    review _: DeterministicApprovalReview,
    completion: @escaping (ApprovalReviewDecision) -> Void
  ) -> any ApprovalCancellation {
    calls += 1
    completions.append(completion)
    return NoopApprovalCancellation()
  }

  func approve(at index: Int) {
    completions[index](.continueToAuthentication)
  }
}

private final class IndependentAuthenticator: FreshAuthenticator {
  private var completion: ((ApprovalAuthenticationResult) -> Void)?
  private(set) var calls = 0

  func authenticate(
    reason _: String,
    completion: @escaping (ApprovalAuthenticationResult) -> Void
  ) -> any ApprovalCancellation {
    calls += 1
    self.completion = completion
    return NoopApprovalCancellation()
  }

  func succeed(_ session: any AuthenticatedApprovalSession) {
    completion?(.authenticated(session))
  }
}

private final class BoundaryClaimSigner: ReceiptSigner {
  private let beforeClaim: () -> Void
  private(set) var claimResult: Bool?

  init(beforeClaim: @escaping () -> Void) {
    self.beforeClaim = beforeClaim
  }

  func sign(
    _: ReceiptSigningPayload,
    authenticatedSession _: any AuthenticatedApprovalSession,
    permit: any ReceiptSigningPermit,
    completion: @escaping (Result<Data, ApprovalHostError>) -> Void
  ) -> any ApprovalCancellation {
    beforeClaim()
    claimResult = permit.claimSigning()
    completion(.failure(.signingFailed))
    return NoopApprovalCancellation()
  }
}

private final class IndependentSession: AuthenticatedApprovalSession {
  private(set) var invalidated = false
  func invalidate() { invalidated = true }
}

private final class IndependentScheduler: ApprovalDeadlineScheduler {
  private var actions: [() -> Void] = []

  func schedule(
    after _: TimeInterval,
    action: @escaping () -> Void
  ) -> any ApprovalCancellation {
    actions.append(action)
    return NoopApprovalCancellation()
  }

  func fire(at index: Int) {
    actions[index]()
  }
}

private final class IndependentDeferredSigner: ReceiptSigner {
  private(set) var calls = 0

  func sign(
    _: ReceiptSigningPayload,
    authenticatedSession _: any AuthenticatedApprovalSession,
    permit _: any ReceiptSigningPermit,
    completion _: @escaping (Result<Data, ApprovalHostError>) -> Void
  ) -> any ApprovalCancellation {
    calls += 1
    return NoopApprovalCancellation()
  }
}

private final class IndependentFixedIdentifierGenerator: ReceiptIdentifierGenerator {
  func nextReceiptIdentifier() -> String {
    "approval-receipt:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
  }
}

private final class IndependentSequentialClock {
  private var values: [Date]
  private let fallback: Date

  init(values: [Date], fallback: Date) {
    self.values = values
    self.fallback = fallback
  }

  func next() -> Date {
    values.isEmpty ? fallback : values.removeFirst()
  }
}
