import Foundation
import XCTest

@testable import ApprovalHostCore
@testable import ApprovalHostMacOS

/// Fake-only coverage for the distinct Meta campaign PAUSE V2 XPC selector.
/// These tests do not create an XPC connection, LocalAuthentication context,
/// protected-store item, helper executable, or provider request.
final class NativeMetaCampaignPauseApprovalBridgeXPCTests: XCTestCase {
  func testClientRebuildsExactV2RequestAcceptsOneCandidateAndIgnoresRepeatedReply() throws {
    let fixture = try MetaV2ProducerFixture()
    let candidate = try fixture.produce(session: BridgeProducerTestSession())
    let transport = MetaV2XPCTransport()
    let client = AuthenticatedNativeMetaCampaignPauseApprovalBridgeClient(
      metadata: fixture.metadata,
      transportFactory: { transport },
      scheduler: MetaV2XPCNoopScheduler(),
      clock: { fixture.now }
    )
    var responses: [NativeMetaCampaignPauseApprovalBridgeXPCResponse] = []

    client.request(fixture.request) { responses.append($0) }

    XCTAssertEqual(transport.sendCount, 1)
    XCTAssertEqual(transport.requestBytes, fixture.request.canonicalBytes)
    transport.reply(.response(candidate: candidate.canonicalBytes, code: nil, message: nil))
    transport.reply(.transportUnavailable)

    XCTAssertEqual(responses, [.candidate(candidate)])
    XCTAssertTrue(transport.invalidated)
  }

  func testClientRejectsV1OrMalformedReplyBeforeCompletion() throws {
    let fixture = try MetaV2ProducerFixture()
    let v1Fixture = try BridgeProducerTestFixture()
    let v1Candidate = try v1Fixture.produce(session: BridgeProducerTestSession())

    let v1Transport = MetaV2XPCTransport()
    let v1Client = AuthenticatedNativeMetaCampaignPauseApprovalBridgeClient(
      metadata: fixture.metadata,
      transportFactory: { v1Transport },
      scheduler: MetaV2XPCNoopScheduler(),
      clock: { fixture.now }
    )
    var v1Response: NativeMetaCampaignPauseApprovalBridgeXPCResponse?
    v1Client.request(fixture.request) { v1Response = $0 }
    v1Transport.reply(.response(candidate: v1Candidate.canonicalBytes, code: nil, message: nil))
    XCTAssertEqual(v1Response, .failure(ApprovalPublicFailure(.transportUnavailable)))

    let malformedTransport = MetaV2XPCTransport()
    let malformedClient = AuthenticatedNativeMetaCampaignPauseApprovalBridgeClient(
      metadata: fixture.metadata,
      transportFactory: { malformedTransport },
      scheduler: MetaV2XPCNoopScheduler(),
      clock: { fixture.now }
    )
    var malformedResponse: NativeMetaCampaignPauseApprovalBridgeXPCResponse?
    malformedClient.request(fixture.request) { malformedResponse = $0 }
    malformedTransport.reply(.response(candidate: nil, code: nil, message: nil))
    XCTAssertEqual(malformedResponse, .failure(ApprovalPublicFailure(.transportUnavailable)))
  }

  func testV1ClientRejectsV2CandidateBeforeCompletion() throws {
    let v1Fixture = try BridgeProducerTestFixture()
    let v2Fixture = try MetaV2ProducerFixture()
    let v2Candidate = try v2Fixture.produce(session: BridgeProducerTestSession())
    let transport = MetaV2XPCV1Transport()
    let client = AuthenticatedNativeApprovalBridgeClient(
      metadata: v1Fixture.metadata,
      transportFactory: { transport },
      scheduler: MetaV2XPCNoopScheduler(),
      clock: { v1Fixture.now }
    )
    var response: NativeApprovalBridgeXPCResponse?

    client.request(v1Fixture.request) { response = $0 }
    transport.reply(.response(candidate: v2Candidate.canonicalBytes, code: nil, message: nil))

    XCTAssertEqual(response, .failure(ApprovalPublicFailure(.transportUnavailable)))
    XCTAssertTrue(transport.invalidated)
  }

  func testClientAcceptsOnlyExactFixedFailureTuple() throws {
    let fixture = try MetaV2ProducerFixture()
    let exactTransport = MetaV2XPCTransport()
    let exactClient = AuthenticatedNativeMetaCampaignPauseApprovalBridgeClient(
      metadata: fixture.metadata,
      transportFactory: { exactTransport },
      scheduler: MetaV2XPCNoopScheduler(),
      clock: { fixture.now }
    )
    var exactResponse: NativeMetaCampaignPauseApprovalBridgeXPCResponse?

    exactClient.request(fixture.request) { exactResponse = $0 }
    exactTransport.reply(
      .response(
        candidate: nil,
        code: ApprovalHostError.hostNotInstalled.code,
        message: ApprovalHostError.hostNotInstalled.publicMessage
      )
    )

    XCTAssertEqual(exactResponse, .failure(ApprovalPublicFailure(.hostNotInstalled)))
    XCTAssertTrue(exactTransport.invalidated)

    let mismatchedTransport = MetaV2XPCTransport()
    let mismatchedClient = AuthenticatedNativeMetaCampaignPauseApprovalBridgeClient(
      metadata: fixture.metadata,
      transportFactory: { mismatchedTransport },
      scheduler: MetaV2XPCNoopScheduler(),
      clock: { fixture.now }
    )
    var mismatchedResponse: NativeMetaCampaignPauseApprovalBridgeXPCResponse?

    mismatchedClient.request(fixture.request) { mismatchedResponse = $0 }
    mismatchedTransport.reply(
      .response(
        candidate: nil,
        code: ApprovalHostError.hostNotInstalled.code,
        message: "untrusted failure text"
      )
    )

    XCTAssertEqual(
      mismatchedResponse,
      .failure(ApprovalPublicFailure(.transportUnavailable))
    )
    XCTAssertTrue(mismatchedTransport.invalidated)
  }

  func testClientTimeoutAndInvalidationSuppressLateV2Candidate() throws {
    let fixture = try MetaV2ProducerFixture()
    let candidate = try fixture.produce(session: BridgeProducerTestSession())
    let transport = MetaV2XPCTransport()
    let scheduler = MetaV2XPCDeadlineScheduler()
    let client = AuthenticatedNativeMetaCampaignPauseApprovalBridgeClient(
      metadata: fixture.metadata,
      transportFactory: { transport },
      scheduler: scheduler,
      clock: { fixture.now }
    )
    var responses: [NativeMetaCampaignPauseApprovalBridgeXPCResponse] = []

    client.request(fixture.request) { responses.append($0) }
    scheduler.fire()
    transport.reply(.response(candidate: candidate.canonicalBytes, code: nil, message: nil))

    XCTAssertEqual(responses, [.failure(ApprovalPublicFailure(.timedOut))])
    XCTAssertTrue(transport.invalidated)
  }

  func testV2SelectorWithoutFactoryFailsBeforeReviewOrInteraction() throws {
    let fixture = try MetaV2ProducerFixture()
    let reviewer = MetaV2XPCReviewer()
    let gate = makeGate()
    let service = makeService(fixture: fixture, reviewer: reviewer, gate: gate)
    var response: (Data?, String?, String?)?

    service.requestNativeMetaCampaignPauseApprovalBridge(fixture.request.canonicalBytes) {
      response = ($0, $1, $2)
    }

    XCTAssertNil(response?.0)
    XCTAssertEqual(response?.1, ApprovalHostError.hostNotInstalled.code)
    XCTAssertEqual(response?.2, ApprovalHostError.hostNotInstalled.publicMessage)
    XCTAssertEqual(reviewer.calls, 0)
    XCTAssertTrue(gate.isAvailable)
  }

  func testDirectServiceWithoutHeldProtocolDigestKeepsV2SelectorClosed() throws {
    let fixture = try MetaV2ProducerFixture()
    let reviewer = MetaV2XPCReviewer()
    let factory = MetaV2XPCConsumerFactory(metadata: fixture.metadata, consumer: nil)
    let gate = makeGate()
    let service = ApprovalHostXPCService(
      engine: ApprovalHostEngine(
        configuration: fixture.configuration,
        reviewer: reviewer,
        authenticator: MetaV2XPCAuthenticator(),
        signer: MetaV2XPCSigner(),
        identifierGenerator: MetaV2XPCIdentifierGenerator(),
        scheduler: MetaV2XPCNoopScheduler(),
        clock: { fixture.now }
      ),
      interactionGate: gate,
      nativeMetaCampaignPauseBridgeConsumerFactory: factory,
      clock: { fixture.now }
    )
    var response: (Data?, String?, String?)?

    service.requestNativeMetaCampaignPauseApprovalBridge(fixture.request.canonicalBytes) {
      response = ($0, $1, $2)
    }

    XCTAssertNil(response?.0)
    XCTAssertEqual(response?.1, ApprovalHostError.hostNotInstalled.code)
    XCTAssertEqual(response?.2, ApprovalHostError.hostNotInstalled.publicMessage)
    XCTAssertEqual(factory.makeCalls, 0)
    XCTAssertEqual(reviewer.calls, 0)
    XCTAssertTrue(gate.isAvailable)
  }

  func testV2SelectorRejectsV1RequestBeforeFactoryOrInteraction() throws {
    let fixture = try MetaV2ProducerFixture()
    let v1Fixture = try BridgeProducerTestFixture()
    let reviewer = MetaV2XPCReviewer()
    let factory = MetaV2XPCConsumerFactory(metadata: fixture.metadata, consumer: nil)
    let gate = makeGate()
    let service = makeService(
      fixture: fixture,
      reviewer: reviewer,
      gate: gate,
      metaFactory: factory
    )
    var response: (Data?, String?, String?)?

    service.requestNativeMetaCampaignPauseApprovalBridge(v1Fixture.request.canonicalBytes) {
      response = ($0, $1, $2)
    }

    XCTAssertNil(response?.0)
    XCTAssertEqual(response?.1, ApprovalHostError.malformedChallenge.code)
    XCTAssertEqual(factory.makeCalls, 0)
    XCTAssertEqual(reviewer.calls, 0)
    XCTAssertTrue(gate.isAvailable)
  }

  func testV2SelectorRejectsFactoryAndEngineProtocolMismatchBeforeInteraction() throws {
    let fixture = try MetaV2ProducerFixture()
    let mismatchedMetadata = try ApprovalHostBuildMetadataV1(
      hostReference: fixture.metadata.hostReference,
      signingKeyReference: fixture.metadata.signingKeyReference,
      machServiceName: fixture.metadata.machServiceName,
      clientCodeIdentity: fixture.metadata.clientCodeIdentity,
      hostCodeIdentity: fixture.metadata.hostCodeIdentity,
      signingKeyApplicationTag: fixture.metadata.signingKeyApplicationTag,
      signingKeyAccessGroup: fixture.metadata.signingKeyAccessGroup,
      keyStorage: fixture.metadata.keyStorage,
      provenanceService: fixture.metadata.provenanceService,
      provenanceAccount: fixture.metadata.provenanceAccount,
      limits: ApprovalHostLimits(maximumSubplans: 5)
    )
    let descriptor = try NativeApprovalHostArtifactDescriptorV1(metadata: mismatchedMetadata)
    let mismatchedRequest = try NativeMetaCampaignPauseApprovalBridgeRequestV2(
      requestID: fixture.request.requestID,
      sourceContextDigest: fixture.request.sourceContextDigest,
      metadataDigest: descriptor.metadataDigest,
      protocolConfigurationDigest: descriptor.protocolConfigurationDigest,
      canonicalChallenge: fixture.request.canonicalChallenge
    )
    let reviewer = MetaV2XPCReviewer()
    let factory = MetaV2XPCConsumerFactory(metadata: mismatchedMetadata, consumer: nil)
    let gate = makeGate()
    let service = makeService(
      fixture: fixture,
      reviewer: reviewer,
      gate: gate,
      metaFactory: factory
    )
    var response: (Data?, String?, String?)?

    service.requestNativeMetaCampaignPauseApprovalBridge(mismatchedRequest.canonicalBytes) {
      response = ($0, $1, $2)
    }

    XCTAssertNil(response?.0)
    XCTAssertEqual(response?.1, ApprovalHostError.malformedChallenge.code)
    XCTAssertEqual(response?.2, ApprovalHostError.malformedChallenge.publicMessage)
    XCTAssertEqual(factory.makeCalls, 0)
    XCTAssertEqual(reviewer.calls, 0)
    XCTAssertTrue(gate.isAvailable)
  }

  func testV1SelectorRejectsV2RequestBeforeV1FactoryOrInteraction() throws {
    let fixture = try MetaV2ProducerFixture()
    let v1Fixture = try BridgeProducerTestFixture()
    let reviewer = MetaV2XPCReviewer()
    let v1Factory = MetaV2XPCV1Factory(metadata: v1Fixture.metadata)
    let metaFactory = MetaV2XPCConsumerFactory(metadata: fixture.metadata, consumer: nil)
    let gate = makeGate()
    let service = makeService(
      fixture: fixture,
      reviewer: reviewer,
      gate: gate,
      v1Factory: v1Factory,
      metaFactory: metaFactory
    )
    var response: (Data?, String?, String?)?

    service.requestNativeApprovalBridge(fixture.request.canonicalBytes) {
      response = ($0, $1, $2)
    }

    XCTAssertNil(response?.0)
    XCTAssertEqual(response?.1, ApprovalHostError.malformedChallenge.code)
    XCTAssertEqual(v1Factory.makeCalls, 0)
    XCTAssertEqual(reviewer.calls, 0)
    XCTAssertTrue(gate.isAvailable)
  }

  func testRawMetaReceiptRouteRemainsClosedWhenV2FactoryIsPresent() throws {
    let fixture = try MetaV2ProducerFixture()
    let reviewer = MetaV2XPCReviewer()
    let factory = MetaV2XPCConsumerFactory(metadata: fixture.metadata, consumer: nil)
    let gate = makeGate()
    let service = makeService(
      fixture: fixture,
      reviewer: reviewer,
      gate: gate,
      metaFactory: factory
    )
    var response: (Data?, String?, String?)?

    service.requestApproval(fixture.request.canonicalChallenge) {
      response = ($0, $1, $2)
    }

    XCTAssertNil(response?.0)
    XCTAssertEqual(response?.1, ApprovalHostError.malformedChallenge.code)
    XCTAssertEqual(response?.2, ApprovalHostError.malformedChallenge.publicMessage)
    XCTAssertEqual(factory.makeCalls, 0)
    XCTAssertEqual(reviewer.calls, 0)
    XCTAssertTrue(gate.isAvailable)
  }

  func testV2SelectorRejectsConcurrentAndInvalidatedRequestsWithFixedFailures() throws {
    let fixture = try MetaV2ProducerFixture()
    let candidate = try fixture.produce(session: BridgeProducerTestSession())
    let reviewer = MetaV2XPCReviewer()
    let factory = MetaV2XPCConsumerFactory(
      metadata: fixture.metadata,
      consumer: MetaV2XPCCandidateConsumer(candidate: candidate)
    )
    let gate = makeGate()
    let service = makeService(
      fixture: fixture,
      reviewer: reviewer,
      gate: gate,
      metaFactory: factory
    )
    var firstResponse: (Data?, String?, String?)?
    var concurrentResponse: (Data?, String?, String?)?

    service.requestNativeMetaCampaignPauseApprovalBridge(fixture.request.canonicalBytes) {
      firstResponse = ($0, $1, $2)
    }
    XCTAssertEqual(reviewer.calls, 1)
    XCTAssertFalse(gate.isAvailable)

    service.requestNativeMetaCampaignPauseApprovalBridge(fixture.request.canonicalBytes) {
      concurrentResponse = ($0, $1, $2)
    }

    XCTAssertNil(concurrentResponse?.0)
    XCTAssertEqual(concurrentResponse?.1, ApprovalHostError.requestAlreadyActive.code)
    XCTAssertEqual(concurrentResponse?.2, ApprovalHostError.requestAlreadyActive.publicMessage)
    XCTAssertEqual(reviewer.calls, 1)
    XCTAssertFalse(gate.isAvailable)

    service.cancelPending()

    XCTAssertNil(firstResponse?.0)
    XCTAssertEqual(firstResponse?.1, ApprovalHostError.reviewCancelled.code)
    XCTAssertEqual(firstResponse?.2, ApprovalHostError.reviewCancelled.publicMessage)
    XCTAssertTrue(gate.isAvailable)

    var invalidatedResponse: (Data?, String?, String?)?
    service.requestNativeMetaCampaignPauseApprovalBridge(fixture.request.canonicalBytes) {
      invalidatedResponse = ($0, $1, $2)
    }

    XCTAssertNil(invalidatedResponse?.0)
    XCTAssertEqual(invalidatedResponse?.1, ApprovalHostError.hostNotInstalled.code)
    XCTAssertEqual(invalidatedResponse?.2, ApprovalHostError.hostNotInstalled.publicMessage)
    XCTAssertEqual(reviewer.calls, 1)
    XCTAssertTrue(gate.isAvailable)
  }

  func testV2SelectorReturnsCandidateAfterOneSessionBoundHandoff() throws {
    let fixture = try MetaV2ProducerFixture()
    let candidate = try fixture.produce(session: BridgeProducerTestSession())
    let reviewer = MetaV2XPCReviewer()
    let authenticator = MetaV2XPCAuthenticator()
    let signer = MetaV2XPCSigner()
    let session = MetaV2XPCSession()
    let consumer = MetaV2XPCCandidateConsumer(candidate: candidate)
    let factory = MetaV2XPCConsumerFactory(metadata: fixture.metadata, consumer: consumer)
    let gate = makeGate()
    let service = makeService(
      fixture: fixture,
      reviewer: reviewer,
      authenticator: authenticator,
      signer: signer,
      gate: gate,
      metaFactory: factory
    )
    var response: (Data?, String?, String?)?

    service.requestNativeMetaCampaignPauseApprovalBridge(fixture.request.canonicalBytes) {
      response = ($0, $1, $2)
    }
    XCTAssertEqual(factory.makeCalls, 1)
    XCTAssertEqual(reviewer.calls, 1)
    reviewer.respond(.continueToAuthentication)
    authenticator.respond(.authenticated(session))
    signer.respond()

    XCTAssertEqual(response?.0, candidate.canonicalBytes)
    XCTAssertNil(response?.1)
    XCTAssertNil(response?.2)
    XCTAssertEqual(consumer.calls, 1)
    XCTAssertEqual(consumer.sessionUses, 1)
    XCTAssertEqual(session.invalidations, 1)
    XCTAssertTrue(gate.isAvailable)
  }

  func testV2SelectorCancellationSuppressesCandidateAndReleasesInteractionGate() throws {
    let fixture = try MetaV2ProducerFixture()
    let reviewer = MetaV2XPCReviewer()
    let consumer = MetaV2XPCCandidateConsumer(
      candidate: try fixture.produce(session: BridgeProducerTestSession())
    )
    let factory = MetaV2XPCConsumerFactory(metadata: fixture.metadata, consumer: consumer)
    let gate = makeGate()
    let service = makeService(
      fixture: fixture,
      reviewer: reviewer,
      gate: gate,
      metaFactory: factory
    )
    var response: (Data?, String?, String?)?

    service.requestNativeMetaCampaignPauseApprovalBridge(fixture.request.canonicalBytes) {
      response = ($0, $1, $2)
    }
    service.cancelPending()

    XCTAssertNil(response?.0)
    XCTAssertEqual(response?.1, ApprovalHostError.reviewCancelled.code)
    XCTAssertEqual(response?.2, ApprovalHostError.reviewCancelled.publicMessage)
    XCTAssertEqual(consumer.calls, 0)
    XCTAssertTrue(gate.isAvailable)
  }

  private func makeGate() -> InstalledApprovalHostInteractionGate {
    let gate = InstalledApprovalHostInteractionGate(performRelease: { $0() })
    gate.activate()
    return gate
  }

  private func makeService(
    fixture: MetaV2ProducerFixture,
    reviewer: MetaV2XPCReviewer,
    authenticator: MetaV2XPCAuthenticator = MetaV2XPCAuthenticator(),
    signer: MetaV2XPCSigner = MetaV2XPCSigner(),
    gate: InstalledApprovalHostInteractionGate,
    v1Factory: (any NativeApprovalBridgePostSigningConsumerProviding)? = nil,
    metaFactory: (any NativeMetaCampaignPauseApprovalBridgePostSigningConsumerProviding)? = nil
  ) -> ApprovalHostXPCService {
    ApprovalHostXPCService(
      engine: ApprovalHostEngine(
        configuration: fixture.configuration,
        reviewer: reviewer,
        authenticator: authenticator,
        signer: signer,
        identifierGenerator: MetaV2XPCIdentifierGenerator(),
        scheduler: MetaV2XPCNoopScheduler(),
        clock: { fixture.now }
      ),
      interactionGate: gate,
      nativeBridgeConsumerFactory: v1Factory,
      nativeMetaCampaignPauseBridgeConsumerFactory: metaFactory,
      nativeMetaCampaignPauseProtocolConfigurationDigest:
        fixture.configuration.approvalProtocolConfigurationV1.configurationDigest,
      clock: { fixture.now }
    )
  }
}

private final class MetaV2XPCTransport: NativeMetaCampaignPauseApprovalBridgeXPCTransport {
  private(set) var sendCount = 0
  private(set) var requestBytes: Data?
  private(set) var invalidated = false
  private var completion: ((NativeMetaCampaignPauseApprovalBridgeXPCWireResponse) -> Void)?

  func send(
    canonicalRequest: Data,
    completion: @escaping (NativeMetaCampaignPauseApprovalBridgeXPCWireResponse) -> Void
  ) {
    sendCount += 1
    requestBytes = canonicalRequest
    self.completion = completion
  }

  func invalidate() { invalidated = true }

  func reply(_ response: NativeMetaCampaignPauseApprovalBridgeXPCWireResponse) {
    completion?(response)
  }
}

private final class MetaV2XPCV1Transport: NativeApprovalBridgeXPCTransport {
  private(set) var invalidated = false
  private var completion: ((NativeApprovalBridgeXPCWireResponse) -> Void)?

  func send(
    canonicalRequest _: Data,
    completion: @escaping (NativeApprovalBridgeXPCWireResponse) -> Void
  ) {
    self.completion = completion
  }

  func invalidate() { invalidated = true }

  func reply(_ response: NativeApprovalBridgeXPCWireResponse) {
    completion?(response)
  }
}

private final class MetaV2XPCNoopScheduler: ApprovalDeadlineScheduler {
  func schedule(
    after _: TimeInterval,
    action _: @escaping () -> Void
  ) -> any ApprovalCancellation {
    MetaV2XPCCancellation()
  }
}

private final class MetaV2XPCDeadlineScheduler: ApprovalDeadlineScheduler {
  private var cancellation: MetaV2XPCCancellation?

  func schedule(
    after _: TimeInterval,
    action: @escaping () -> Void
  ) -> any ApprovalCancellation {
    let cancellation = MetaV2XPCCancellation(action: action)
    self.cancellation = cancellation
    return cancellation
  }

  func fire() { cancellation?.fire() }
}

private final class MetaV2XPCCancellation: ApprovalCancellation {
  private var cancelled = false
  private let action: (() -> Void)?

  init(action: (() -> Void)? = nil) { self.action = action }

  func cancel() { cancelled = true }

  func fire() {
    guard !cancelled else { return }
    action?()
  }
}

private final class MetaV2XPCReviewer: ApprovalReviewer {
  private var completion: ((ApprovalReviewDecision) -> Void)?
  private(set) var calls = 0

  func present(
    review _: DeterministicApprovalReview,
    completion: @escaping (ApprovalReviewDecision) -> Void
  ) -> any ApprovalCancellation {
    calls += 1
    self.completion = completion
    return MetaV2XPCCancellation()
  }

  func respond(_ decision: ApprovalReviewDecision) { completion?(decision) }
}

private final class MetaV2XPCAuthenticator: FreshAuthenticator {
  private var completion: ((ApprovalAuthenticationResult) -> Void)?

  func authenticate(
    reason _: String,
    completion: @escaping (ApprovalAuthenticationResult) -> Void
  ) -> any ApprovalCancellation {
    self.completion = completion
    return MetaV2XPCCancellation()
  }

  func respond(_ result: ApprovalAuthenticationResult) { completion?(result) }
}

private final class MetaV2XPCSigner: ReceiptSigner {
  private var permit: (any ReceiptSigningPermit)?
  private var completion: ((Result<Data, ApprovalHostError>) -> Void)?

  func sign(
    _: ReceiptSigningPayload,
    authenticatedSession _: any AuthenticatedApprovalSession,
    permit: any ReceiptSigningPermit,
    completion: @escaping (Result<Data, ApprovalHostError>) -> Void
  ) -> any ApprovalCancellation {
    self.permit = permit
    self.completion = completion
    return MetaV2XPCCancellation()
  }

  func respond() {
    guard permit?.claimSigning() == true else {
      completion?(.failure(.signingFailed))
      return
    }
    completion?(.success(Data(repeating: 0x5a, count: 64)))
  }
}

private final class MetaV2XPCSession: AuthenticatedApprovalSession {
  private(set) var invalidations = 0

  func invalidate() { invalidations += 1 }
}

private final class MetaV2XPCIdentifierGenerator: ReceiptIdentifierGenerator {
  func nextReceiptIdentifier() -> String {
    "approval-receipt:" + String(repeating: "X", count: 43)
  }
}

private final class MetaV2XPCConsumerFactory:
  NativeMetaCampaignPauseApprovalBridgePostSigningConsumerProviding
{
  let metadata: ApprovalHostBuildMetadataV1
  private let consumer: (any NativeMetaCampaignPauseApprovalBridgePostSigningConsumer)?
  private(set) var makeCalls = 0

  init(
    metadata: ApprovalHostBuildMetadataV1,
    consumer: (any NativeMetaCampaignPauseApprovalBridgePostSigningConsumer)?
  ) {
    self.metadata = metadata
    self.consumer = consumer
  }

  func makeConsumer(
    request _: NativeMetaCampaignPauseApprovalBridgeRequestV2,
    exchangeStartedAt _: Date
  ) throws -> any NativeMetaCampaignPauseApprovalBridgePostSigningConsumer {
    makeCalls += 1
    guard let consumer else { throw ApprovalHostError.hostNotInstalled }
    return consumer
  }
}

private final class MetaV2XPCCandidateConsumer:
  NativeMetaCampaignPauseApprovalBridgePostSigningConsumer
{
  private let candidate: NativeMetaCampaignPauseApprovalBridgeCandidateV2
  private(set) var calls = 0
  private(set) var sessionUses = 0

  init(candidate: NativeMetaCampaignPauseApprovalBridgeCandidateV2) {
    self.candidate = candidate
  }

  func consume(
    signedReceipt _: SignedApprovalReceipt,
    expectedChallenge _: ApprovalChallenge,
    sessionLease: ApprovalPostSigningSessionLease
  ) -> Result<NativeMetaCampaignPauseApprovalBridgeCandidateV2, ApprovalHostError> {
    calls += 1
    do {
      try sessionLease.withAuthenticatedSession { _ in sessionUses += 1 }
      return .success(candidate)
    } catch {
      return .failure(.enrollmentStateUnverified)
    }
  }
}

private final class MetaV2XPCV1Factory: NativeApprovalBridgePostSigningConsumerProviding {
  let metadata: ApprovalHostBuildMetadataV1
  private(set) var makeCalls = 0

  init(metadata: ApprovalHostBuildMetadataV1) { self.metadata = metadata }

  func makeConsumer(
    request _: NativeApprovalBridgeRequestV1,
    exchangeStartedAt _: Date
  ) throws -> any ApprovalPostSigningConsumer {
    makeCalls += 1
    throw ApprovalHostError.hostNotInstalled
  }
}
