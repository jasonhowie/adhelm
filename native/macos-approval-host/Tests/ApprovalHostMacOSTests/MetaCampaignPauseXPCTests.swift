import Foundation
import XCTest

@testable import ApprovalHostCore
@testable import ApprovalHostMacOS

/// This exercises the raw receipt client's closed admission check with an
/// injected in-memory transport. It never creates an XPC connection, invokes
/// authentication, or accesses a protected key or provider.
final class MetaCampaignPauseXPCTests: XCTestCase {
  private let now = Date(timeIntervalSince1970: 1_893_456_000)

  func testRawReceiptRouteRefusesMetaSubjectBeforeTransportOrDeadline() throws {
    let challenge = try metaFixtureChallenge()
    XCTAssertEqual(
      try ApprovalChallenge.decode(challenge, now: now, limits: configuration().limits).kind,
      .metaCampaignPausePlan
    )

    let transport = MetaPauseRawXPCTransport()
    let scheduler = MetaPauseDeadlineScheduler()
    let client = AuthenticatedApprovalXPCClient(
      configuration: configuration(),
      transportFactory: { transport },
      scheduler: scheduler,
      clock: { self.now }
    )
    var response: ApprovalHostXPCResponse?
    client.requestApproval(canonicalChallenge: challenge) { response = $0 }

    XCTAssertEqual(publicError(response), .malformedChallenge)
    XCTAssertEqual(transport.sendCount, 0)
    XCTAssertEqual(scheduler.scheduledCount, 0)
  }

  func testRawServiceRefusesMetaBeforeInteractionLeaseReviewAuthenticationOrSigning() throws {
    let reviewer = MetaPauseServerReviewer()
    let authenticator = MetaPauseServerAuthenticator()
    let signer = MetaPauseServerSigner()
    let gate = InstalledApprovalHostInteractionGate(performRelease: { $0() })
    gate.activate()
    let engine = ApprovalHostEngine(
      configuration: configuration(),
      reviewer: reviewer,
      authenticator: authenticator,
      signer: signer,
      identifierGenerator: MetaPauseIdentifierGenerator(),
      scheduler: MetaPauseDeadlineScheduler(),
      clock: { self.now }
    )
    let service = ApprovalHostXPCService(
      engine: engine,
      interactionGate: gate,
      clock: { self.now }
    )
    var response: (Data?, String?, String?)?
    service.requestApproval(try metaFixtureChallenge()) { response = ($0, $1, $2) }

    XCTAssertNil(response?.0)
    XCTAssertEqual(response?.1, ApprovalHostError.malformedChallenge.code)
    XCTAssertEqual(response?.2, ApprovalHostError.malformedChallenge.publicMessage)
    XCTAssertEqual(reviewer.calls, 0)
    XCTAssertEqual(authenticator.calls, 0)
    XCTAssertEqual(signer.calls, 0)
    XCTAssertTrue(gate.isAvailable)
  }

  private func metaFixtureChallenge() throws -> Data {
    let testSource = URL(fileURLWithPath: #filePath)
    let packageRoot =
      testSource
      .deletingLastPathComponent()
      .deletingLastPathComponent()
      .deletingLastPathComponent()
    let fixtureURL =
      packageRoot
      .appendingPathComponent("Tests/ApprovalHostCoreTests/Fixtures/MetaPause")
      .appendingPathComponent("meta-campaign-pause-approval-v1.json")
    let root = try CanonicalJSON.parseCanonical(Data(contentsOf: fixtureURL))
    guard case .object(let fields) = root,
      case .object(let challenge)? = fields["challenge"]
    else { throw FixtureError.invalid }
    return try CanonicalJSON.encode(.object(challenge))
  }

  private func configuration() -> TrustedHostConfiguration {
    try! TrustedHostConfiguration(
      hostReference: "host:adhelm-macos-approval-v1",
      signingKeyReference: "approval-key:fixture-v1",
      machServiceName: "com.aptsonic.adhelm.approval.fixture",
      expectedClientCodeSigningRequirement: "identifier \"com.aptsonic.adhelm.client\"",
      expectedHostCodeSigningRequirement: "identifier \"com.aptsonic.adhelm.host\"",
      signingKeyApplicationTag: Data("fixture".utf8),
      keyStorage: .keychainP256
    )
  }

  private func publicError(_ response: ApprovalHostXPCResponse?) -> ApprovalHostError? {
    guard case .failure(let failure)? = response else { return nil }
    return ApprovalHostError.exactPublicFailure(code: failure.code, message: failure.message)
  }
}

private final class MetaPauseRawXPCTransport: ApprovalXPCTransport {
  private(set) var sendCount = 0

  func send(
    canonicalChallenge _: Data,
    completion _: @escaping (ApprovalXPCWireResponse) -> Void
  ) {
    sendCount += 1
  }

  func invalidate() {}
}

private final class MetaPauseDeadlineScheduler: ApprovalDeadlineScheduler {
  private(set) var scheduledCount = 0

  func schedule(
    after _: TimeInterval,
    action _: @escaping () -> Void
  ) -> any ApprovalCancellation {
    scheduledCount += 1
    return MetaPauseCancellation()
  }
}

private final class MetaPauseCancellation: ApprovalCancellation {
  func cancel() {}
}

private final class MetaPauseServerReviewer: ApprovalReviewer {
  private(set) var calls = 0

  func present(
    review _: DeterministicApprovalReview,
    completion _: @escaping (ApprovalReviewDecision) -> Void
  ) -> any ApprovalCancellation {
    calls += 1
    return MetaPauseCancellation()
  }
}

private final class MetaPauseServerAuthenticator: FreshAuthenticator {
  private(set) var calls = 0

  func authenticate(
    reason _: String,
    completion _: @escaping (ApprovalAuthenticationResult) -> Void
  ) -> any ApprovalCancellation {
    calls += 1
    return MetaPauseCancellation()
  }
}

private final class MetaPauseServerSigner: ReceiptSigner {
  private(set) var calls = 0

  func sign(
    _: ReceiptSigningPayload,
    authenticatedSession _: any AuthenticatedApprovalSession,
    permit _: any ReceiptSigningPermit,
    completion _: @escaping (Result<Data, ApprovalHostError>) -> Void
  ) -> any ApprovalCancellation {
    calls += 1
    return MetaPauseCancellation()
  }
}

private final class MetaPauseIdentifierGenerator: ReceiptIdentifierGenerator {
  func nextReceiptIdentifier() -> String {
    "approval-receipt:" + String(repeating: "M", count: 43)
  }
}

private enum FixtureError: Error {
  case invalid
}
