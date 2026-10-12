import Foundation
import XCTest
@testable import ApprovalHostCore

final class ApprovalHostCoreTests: XCTestCase {
    private let now = Date(timeIntervalSince1970: 1_893_456_060)

    func testOpenAIPauseFixtureReconstructsActualPythonPlanAndReviewFacts() throws {
        let challenge = try ApprovalChallenge.decode(fixture("openai-pause-challenge-v1"), now: now)
        XCTAssertEqual(challenge.kind, .openAIPausePlan)
        guard case let .openAIPausePlan(grantorRef, action) = challenge.subject else {
            return XCTFail("expected fixed OpenAI pause action")
        }
        XCTAssertEqual(grantorRef, "customer-grantor-1")
        XCTAssertEqual(action.plan.digest, metadata().actionPlanDigest)
        XCTAssertEqual(action.plan.subplans.map(\.digest), [metadata().actionSubplanDigest])
        XCTAssertEqual(action.binding.campaign.startAtText, "2029-12-31T18:00:00.123456Z")
        XCTAssertEqual(action.binding.campaign.endAtText, "2030-01-31T18:00:00.654321Z")
        XCTAssertEqual(action.workspaceBinding?.workspaceProfileRef, "workspace-ledger-1")

        let review = try DeterministicApprovalReview.make(for: challenge)
        XCTAssertTrue(review.text.contains("clicks"))
        XCTAssertTrue(review.text.contains("2500000"))
        XCTAssertTrue(review.text.contains("NOT_PROVEN_STOPPED"))
        XCTAssertTrue(review.text.contains("campaign/status"))
        XCTAssertTrue(review.text.contains("openai-ads-pause-development-contract-2026-09-05"))
        XCTAssertTrue(review.text.contains("Requested transition: Active → Paused"))
        XCTAssertTrue(review.text.contains("Existing lifetime spend limit: 2500000 micros (USD)"))
        XCTAssertTrue(review.text.contains("Incremental spend cap for this pause: 0 minor units (USD)"))
        XCTAssertTrue(review.text.contains("Technical detail — canonical facts bound by the review digest"))
        XCTAssertFalse(review.text.contains("credential-binding:"))
        XCTAssertFalse(review.text.contains(String(repeating: "a", count: 64)))
    }

    func testDelegationPolicyFixturesUseActualPythonDigestAndTypedKinds() throws {
        let install = try ApprovalChallenge.decode(fixture("delegation-policy-install-challenge-v1"), now: now)
        let revoke = try ApprovalChallenge.decode(fixture("delegation-policy-revoke-challenge-v1"), now: now)
        guard case let .delegationPolicyInstall(policy) = install.subject else {
            return XCTFail("expected policy installation")
        }
        XCTAssertEqual(policy.digest, metadata().policyDigest)
        XCTAssertEqual(policy.maximumChangePerOperation[0].maximumMinorUnits, 0)
        XCTAssertEqual(policy.maximumChangePerOperation[0].currency, "USD")
        guard case let .delegationPolicyRevoke(revokedPolicy, _, revokedAtText) = revoke.subject else {
            return XCTFail("expected policy revocation")
        }
        XCTAssertEqual(revokedPolicy.digest, metadata().policyDigest)
        XCTAssertEqual(revokedAtText, "2030-01-01T00:03:00Z")
        let policyReview = try DeterministicApprovalReview.make(for: install)
        XCTAssertTrue(policyReview.text.contains("Per-operation caps: 0 minor units (USD)"))
        XCTAssertTrue(policyReview.text.contains("Authorized agent or host: customer-agent-1"))
    }

    func testWholeSecondPythonVectorReconstructsWithoutTimestampNormalization() throws {
        let challenge = try ApprovalChallenge.decode(
            fixture("openai-pause-whole-seconds-challenge-v1"), now: now
        )
        guard case let .openAIPausePlan(_, action) = challenge.subject else {
            return XCTFail("expected fixed OpenAI pause action")
        }
        XCTAssertEqual(action.binding.campaign.startAtText, "2029-12-31T18:00:00Z")
        XCTAssertEqual(action.binding.campaign.endAtText, "2030-01-31T18:00:00Z")
        XCTAssertEqual(action.plan.digest, metadata().wholeSecondsActionPlanDigest)
        XCTAssertEqual(action.plan.subplans.map(\.digest), [metadata().wholeSecondsActionSubplanDigest])
    }

    func testRejectsGenericOpaqueDirectPlanBeforeReview() throws {
        let data = Data("{\"expires_at\":\"2030-01-01T00:04:00Z\",\"grantor_ref\":\"g\",\"issued_at\":\"2030-01-01T00:00:00Z\",\"kind\":\"DIRECT_PLAN\",\"nonce\":\"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\",\"schema_version\":1}".utf8)
        XCTAssertThrowsError(try ApprovalChallenge.decode(data, now: now))
    }

    func testRejectsNoncanonicalDuplicateFloatAndUnsafeIntegerBeforeSchemaUse() throws {
        XCTAssertThrowsError(try CanonicalJSON.parseCanonical(Data("{\"b\":1,\"a\":2}".utf8)))
        XCTAssertThrowsError(try CanonicalJSON.parseCanonical(Data("{\"a\":1,\"a\":2}".utf8)))
        XCTAssertThrowsError(try CanonicalJSON.parseCanonical(Data("{\"a\":1.0}".utf8)))
        XCTAssertThrowsError(try CanonicalJSON.parseCanonical(Data("{\"a\":9007199254740992}".utf8)))
    }

    func testTrustedConfigurationCannotDisableTheClosedChallengeByteBound() {
        XCTAssertThrowsError(try TrustedHostConfiguration(
            hostReference: "host:fixture",
            signingKeyReference: "key:fixture",
            machServiceName: "com.example.fixture",
            expectedClientCodeSigningRequirement: "identifier \"com.example.fixture\"",
            expectedHostCodeSigningRequirement: "identifier \"com.example.fixture\"",
            signingKeyApplicationTag: Data("fixture".utf8),
            keyStorage: .keychainP256,
            limits: ApprovalHostLimits(maximumChallengeBytes: 65_537)
        ))
    }

    func testRejectsAlternateBase64PadBitsAndInvalidCanonicalTimestamps() throws {
        let original = String(data: fixture("openai-pause-challenge-v1"), encoding: .utf8)!
        let nonce = try XCTUnwrap(quotedValue(named: "nonce", in: original))
        let alternateNonce = String(nonce.dropLast()) + "B"
        let badNonce = original.replacingOccurrences(of: "\"nonce\":\"\(nonce)\"", with: "\"nonce\":\"\(alternateNonce)\"")
        XCTAssertThrowsError(try ApprovalChallenge.decode(Data(badNonce.utf8), now: now))

        let badCalendar = original.replacingOccurrences(of: "\"issued_at\":\"2030-01-01T00:00:00Z\"", with: "\"issued_at\":\"2030-02-30T00:00:00Z\"")
        XCTAssertThrowsError(try ApprovalChallenge.decode(Data(badCalendar.utf8), now: now))
        let badLeapSecond = original.replacingOccurrences(of: "\"issued_at\":\"2030-01-01T00:00:00Z\"", with: "\"issued_at\":\"2030-01-01T00:00:60Z\"")
        XCTAssertThrowsError(try ApprovalChallenge.decode(Data(badLeapSecond.utf8), now: now))
        let noncanonicalFraction = original.replacingOccurrences(of: "\"issued_at\":\"2030-01-01T00:00:00Z\"", with: "\"issued_at\":\"2030-01-01T00:00:00.1Z\"")
        XCTAssertThrowsError(try ApprovalChallenge.decode(Data(noncanonicalFraction.utf8), now: now))
    }

    func testReconstructedPausePlanRejectsAlteredSemanticBaselineBeforeReview() throws {
        let original = String(data: fixture("openai-pause-challenge-v1"), encoding: .utf8)!
        // `conversions` is an allowed review value, so this exercises the
        // separate full-plan reconstruction rather than the source enum.
        let forged = original.replacingOccurrences(
            of: "\"bidding_type\":\"clicks\"",
            with: "\"bidding_type\":\"conversions\""
        )
        XCTAssertThrowsError(try ApprovalChallenge.decode(Data(forged.utf8), now: now))
    }

    func testOpenAIPauseReviewSourceValuesRejectUnsupportedBidsAndUnsafeReferencesBeforeReview() throws {
        let original = String(data: fixture("openai-pause-challenge-v1"), encoding: .utf8)!
        let unsupportedBid = original.replacingOccurrences(of: "\"bidding_type\":\"clicks\"", with: "\"bidding_type\":\"MAXIMIZE_CLICKS\"")
        XCTAssertThrowsError(try ApprovalChallenge.decode(Data(unsupportedBid.utf8), now: now))

        let unsafeLocalReference = original.replacingOccurrences(
            of: "openai-account-local-1",
            with: "openai account local 1"
        )
        XCTAssertThrowsError(try ApprovalChallenge.decode(Data(unsafeLocalReference.utf8), now: now))

        let unsafeNativeReference = original.replacingOccurrences(
            of: "openai-account-native-1",
            with: "openai/account/native/1"
        )
        XCTAssertThrowsError(try ApprovalChallenge.decode(Data(unsafeNativeReference.utf8), now: now))

        // Foundation accepts this fixed-offset alias, while Python ZoneInfo
        // rejects it. The native host uses an explicit shared subset instead
        // of treating Foundation resolution as cross-language proof.
        let foundationOnlyTimezone = original.replacingOccurrences(
            of: "America/Chicago",
            with: "GMT-05:00"
        )
        XCTAssertThrowsError(try ApprovalChallenge.decode(Data(foundationOnlyTimezone.utf8), now: now))
    }

    func testStateMachineOrdersReviewFreshAuthenticationThenTypedSigning() throws {
        let reviewer = FakeReviewer()
        let authenticator = FakeAuthenticator()
        let signer = FakeSigner()
        let scheduler = FakeScheduler()
        let engine = ApprovalHostEngine(
            configuration: configuration(),
            reviewer: reviewer,
            authenticator: authenticator,
            signer: signer,
            identifierGenerator: FixedIdentifierGenerator(),
            scheduler: scheduler,
            clock: { self.now }
        )
        var result: Result<SignedApprovalReceipt, ApprovalHostError>?
        _ = engine.requestApproval(challengeData: fixture("openai-pause-challenge-v1")) { result = $0 }
        XCTAssertEqual(reviewer.calls, 1)
        XCTAssertEqual(authenticator.calls, 0)
        XCTAssertEqual(signer.calls, 0)

        reviewer.respond(.continueToAuthentication)
        XCTAssertEqual(authenticator.calls, 1)
        XCTAssertEqual(signer.calls, 0)
        authenticator.respond(.authenticated(FakeAuthenticatedSession()))
        XCTAssertEqual(signer.calls, 1)
        XCTAssertTrue(try XCTUnwrap(signer.payload).domainSeparatedBytes.starts(with: Data("adhelm.macos-approval-host.receipt.v1\u{0}OPENAI_PAUSE_PLAN\u{0}".utf8)))
        signer.respond(.success(Data(repeating: 0x5A, count: 64)))

        guard case let .success(receipt) = result else { return XCTFail("expected receipt") }
        XCTAssertEqual(receipt.kind, .openAIPausePlan)
        XCTAssertTrue(receipt.canonicalReceipt.contains(Data("\"action_digest\"".utf8)))
        XCTAssertTrue(receipt.canonicalReceipt.contains(Data("\"plan_digest\"".utf8)))
        XCTAssertTrue(receipt.interactionReference.hasPrefix("evidence:sha256:"))
    }

    func testDecodeFailureNeverCallsReviewAuthenticationOrSigner() {
        let reviewer = FakeReviewer()
        let authenticator = FakeAuthenticator()
        let signer = FakeSigner()
        let engine = ApprovalHostEngine(
            configuration: configuration(),
            reviewer: reviewer,
            authenticator: authenticator,
            signer: signer,
            identifierGenerator: FixedIdentifierGenerator(),
            scheduler: FakeScheduler(),
            clock: { self.now }
        )
        var result: Result<SignedApprovalReceipt, ApprovalHostError>?
        _ = engine.requestApproval(challengeData: Data("{}".utf8)) { result = $0 }
        XCTAssertEqual(reviewer.calls, 0)
        XCTAssertEqual(authenticator.calls, 0)
        XCTAssertEqual(signer.calls, 0)
        XCTAssertEqual(failure(result), .malformedChallenge)
    }

    func testPolicyBoundToAnotherHostFailsBeforeReviewOrAuthentication() {
        let reviewer = FakeReviewer()
        let authenticator = FakeAuthenticator()
        let signer = FakeSigner()
        let engine = ApprovalHostEngine(
            configuration: configuration(),
            reviewer: reviewer,
            authenticator: authenticator,
            signer: signer,
            identifierGenerator: FixedIdentifierGenerator(),
            scheduler: FakeScheduler(),
            clock: { self.now }
        )
        let original = String(data: fixture("delegation-policy-install-challenge-v1"), encoding: .utf8)!
        let wrongHost = original.replacingOccurrences(
            of: "host:adhelm-macos-approval-v1",
            with: "host:evil-macos-approval-v1"
        )
        var result: Result<SignedApprovalReceipt, ApprovalHostError>?
        _ = engine.requestApproval(challengeData: Data(wrongHost.utf8)) { result = $0 }
        XCTAssertEqual(failure(result), .malformedChallenge)
        XCTAssertEqual(reviewer.calls, 0)
        XCTAssertEqual(authenticator.calls, 0)
        XCTAssertEqual(signer.calls, 0)
    }

    func testCancellationAndTimeoutNeverReachSignerAndInvalidateAuthentication() throws {
        let reviewer = FakeReviewer()
        let authenticator = FakeAuthenticator()
        let signer = FakeSigner()
        let scheduler = FakeScheduler()
        let engine = ApprovalHostEngine(
            configuration: configuration(),
            reviewer: reviewer,
            authenticator: authenticator,
            signer: signer,
            identifierGenerator: FixedIdentifierGenerator(),
            scheduler: scheduler,
            clock: { self.now }
        )
        var result: Result<SignedApprovalReceipt, ApprovalHostError>?
        let handle = engine.requestApproval(challengeData: fixture("openai-pause-challenge-v1")) { result = $0 }
        handle.cancel()
        XCTAssertEqual(failure(result), .reviewCancelled)
        XCTAssertEqual(authenticator.calls, 0)
        XCTAssertEqual(signer.calls, 0)

        var timeoutResult: Result<SignedApprovalReceipt, ApprovalHostError>?
        _ = engine.requestApproval(challengeData: fixture("openai-pause-challenge-v1")) { timeoutResult = $0 }
        scheduler.fireLatest()
        XCTAssertEqual(failure(timeoutResult), .timedOut)
        XCTAssertEqual(authenticator.calls, 0)
        XCTAssertEqual(signer.calls, 0)
    }

    func testOnlyOneApprovalFlowCanBeActiveAcrossDistinctChallengeNonces() throws {
        let reviewer = FakeReviewer()
        let authenticator = FakeAuthenticator()
        let signer = FakeSigner()
        let engine = ApprovalHostEngine(
            configuration: configuration(),
            reviewer: reviewer,
            authenticator: authenticator,
            signer: signer,
            identifierGenerator: FixedIdentifierGenerator(),
            scheduler: FakeScheduler(),
            clock: { self.now }
        )
        let first = engine.requestApproval(challengeData: fixture("openai-pause-challenge-v1")) { _ in }
        var secondResult: Result<SignedApprovalReceipt, ApprovalHostError>?
        _ = engine.requestApproval(challengeData: try challengeWithDifferentNonce()) { secondResult = $0 }

        XCTAssertEqual(reviewer.calls, 1)
        XCTAssertEqual(failure(secondResult), .requestAlreadyActive)
        first.cancel()
    }

    func testSigningPermitRevokesAfterCancellationAndLateSignerReplyCannotProduceReceipt() throws {
        let reviewer = FakeReviewer()
        let authenticator = FakeAuthenticator()
        let signer = FakeSigner()
        let engine = ApprovalHostEngine(
            configuration: configuration(),
            reviewer: reviewer,
            authenticator: authenticator,
            signer: signer,
            identifierGenerator: FixedIdentifierGenerator(),
            scheduler: FakeScheduler(),
            clock: { self.now }
        )
        var result: Result<SignedApprovalReceipt, ApprovalHostError>?
        let handle = engine.requestApproval(challengeData: fixture("openai-pause-challenge-v1")) { result = $0 }
        reviewer.respond(.continueToAuthentication)
        let session = FakeAuthenticatedSession()
        authenticator.respond(.authenticated(session))
        XCTAssertTrue(try XCTUnwrap(signer.permit).permitsSigning())

        handle.cancel()
        XCTAssertFalse(try XCTUnwrap(signer.permit).permitsSigning())
        XCTAssertTrue(session.invalidated)
        signer.respond(.success(Data(repeating: 0x5A, count: 64)))
        XCTAssertEqual(failure(result), .authenticationCancelled)
    }

    func testCancellationBeforeConcreteSignerClaimsPermitLeavesNonceUnconsumed() throws {
        let reviewer = FakeReviewer()
        let authenticator = FakeAuthenticator()
        let signer = FakeSigner()
        let engine = ApprovalHostEngine(
            configuration: configuration(),
            reviewer: reviewer,
            authenticator: authenticator,
            signer: signer,
            identifierGenerator: FixedIdentifierGenerator(),
            scheduler: FakeScheduler(),
            clock: { self.now }
        )
        let challenge = fixture("openai-pause-challenge-v1")
        var cancelled: Result<SignedApprovalReceipt, ApprovalHostError>?
        let handle = engine.requestApproval(challengeData: challenge) { cancelled = $0 }
        reviewer.respond(.continueToAuthentication)
        authenticator.respond(.authenticated(FakeAuthenticatedSession()))
        XCTAssertEqual(signer.calls, 1)
        XCTAssertFalse(try XCTUnwrap(signer.permit).hasClaimedSigning())

        handle.cancel()
        XCTAssertEqual(failure(cancelled), .authenticationCancelled)

        var retry: Result<SignedApprovalReceipt, ApprovalHostError>?
        _ = engine.requestApproval(challengeData: challenge) { retry = $0 }
        XCTAssertNil(retry)
        XCTAssertEqual(reviewer.calls, 2)
    }

    func testUnclaimedSignerSuccessCannotCreateReceiptAndClaimedFailureConsumesNonce() throws {
        let reviewer = FakeReviewer()
        let authenticator = FakeAuthenticator()
        let signer = FakeSigner()
        let engine = ApprovalHostEngine(
            configuration: configuration(),
            reviewer: reviewer,
            authenticator: authenticator,
            signer: signer,
            identifierGenerator: FixedIdentifierGenerator(),
            scheduler: FakeScheduler(),
            clock: { self.now }
        )
        let challenge = fixture("openai-pause-challenge-v1")
        var unclaimedResult: Result<SignedApprovalReceipt, ApprovalHostError>?
        _ = engine.requestApproval(challengeData: challenge) { unclaimedResult = $0 }
        reviewer.respond(.continueToAuthentication)
        authenticator.respond(.authenticated(FakeAuthenticatedSession()))
        signer.respondWithoutClaim(.success(Data(repeating: 0x5A, count: 64)))
        XCTAssertEqual(failure(unclaimedResult), .signingFailed)

        // No protected-signing claim happened, so a fresh flow may use the
        // nonce. Once the concrete signer claims it, even a later key failure
        // is deliberately nonretryable.
        var claimedFailure: Result<SignedApprovalReceipt, ApprovalHostError>?
        _ = engine.requestApproval(challengeData: challenge) { claimedFailure = $0 }
        reviewer.respond(.continueToAuthentication)
        authenticator.respond(.authenticated(FakeAuthenticatedSession()))
        XCTAssertTrue(signer.claimPermit())
        signer.respondWithoutClaim(.failure(.signingUnavailable))
        XCTAssertEqual(failure(claimedFailure), .signingUnavailable)

        var replay: Result<SignedApprovalReceipt, ApprovalHostError>?
        _ = engine.requestApproval(challengeData: challenge) { replay = $0 }
        XCTAssertEqual(failure(replay), .challengeAlreadyConsumed)
    }

    func testCancelledRequestPermitCannotClaimANewerFlowWithTheSameNonce() throws {
        let reviewer = FakeReviewer()
        let authenticator = FakeAuthenticator()
        let signer = FakeSigner()
        let engine = ApprovalHostEngine(
            configuration: configuration(),
            reviewer: reviewer,
            authenticator: authenticator,
            signer: signer,
            identifierGenerator: FixedIdentifierGenerator(),
            scheduler: FakeScheduler(),
            clock: { self.now }
        )
        let challenge = fixture("openai-pause-challenge-v1")
        let first = engine.requestApproval(challengeData: challenge) { _ in }
        reviewer.respond(.continueToAuthentication)
        authenticator.respond(.authenticated(FakeAuthenticatedSession()))
        let stalePermit = try XCTUnwrap(signer.permit)
        first.cancel()

        var secondResult: Result<SignedApprovalReceipt, ApprovalHostError>?
        _ = engine.requestApproval(challengeData: challenge) { secondResult = $0 }
        reviewer.respond(.continueToAuthentication)
        authenticator.respond(.authenticated(FakeAuthenticatedSession()))
        XCTAssertFalse(stalePermit.claimSigning())
        XCTAssertTrue(signer.claimPermit())
        signer.respondWithoutClaim(.success(Data(repeating: 0x5A, count: 64)))
        guard case .success? = secondResult else {
            return XCTFail("expected only the current request to produce a receipt")
        }
    }

    func testStaleChallengeAndSignedNonceReplayFailClosed() throws {
        let reviewer = FakeReviewer()
        let authenticator = FakeAuthenticator()
        let signer = FakeSigner()
        let engine = ApprovalHostEngine(
            configuration: configuration(),
            reviewer: reviewer,
            authenticator: authenticator,
            signer: signer,
            identifierGenerator: FixedIdentifierGenerator(),
            scheduler: FakeScheduler(),
            clock: { self.now }
        )
        XCTAssertThrowsError(try ApprovalChallenge.decode(
            fixture("openai-pause-challenge-v1"),
            now: now.addingTimeInterval(61)
        ))

        var firstResult: Result<SignedApprovalReceipt, ApprovalHostError>?
        _ = engine.requestApproval(challengeData: fixture("openai-pause-challenge-v1")) { firstResult = $0 }
        reviewer.respond(.continueToAuthentication)
        authenticator.respond(.authenticated(FakeAuthenticatedSession()))
        signer.respond(.success(Data(repeating: 0x5A, count: 64)))
        guard case .success? = firstResult else { return XCTFail("expected first signed receipt") }

        var replayResult: Result<SignedApprovalReceipt, ApprovalHostError>?
        _ = engine.requestApproval(challengeData: fixture("openai-pause-challenge-v1")) { replayResult = $0 }
        XCTAssertEqual(failure(replayResult), .challengeAlreadyConsumed)
        XCTAssertEqual(reviewer.calls, 1)
        XCTAssertEqual(authenticator.calls, 1)
        XCTAssertEqual(signer.calls, 1)
    }

    private func fixture(_ name: String) -> Data {
        let url = Bundle.module.url(forResource: name, withExtension: "json", subdirectory: "Fixtures")!
        return try! Data(contentsOf: url)
    }

    private func challengeWithDifferentNonce() throws -> Data {
        let original = String(data: fixture("openai-pause-challenge-v1"), encoding: .utf8)!
        let originalNonce = try XCTUnwrap(quotedValue(named: "nonce", in: original))
        let replacementNonce = base64URLEncoded(Data(repeating: 0x01, count: 32))
        precondition(replacementNonce != originalNonce)
        return Data(original.replacingOccurrences(
            of: "\"nonce\":\"\(originalNonce)\"",
            with: "\"nonce\":\"\(replacementNonce)\""
        ).utf8)
    }

    private func metadata() -> Metadata {
        let data = fixture("safety-vector-metadata-v1")
        return try! JSONDecoder().decode(Metadata.self, from: data)
    }

    private func configuration() -> TrustedHostConfiguration {
        try! TrustedHostConfiguration(
            hostReference: "host:adhelm-macos-approval-v1",
            signingKeyReference: "approval-key:fixture-v1",
            machServiceName: "com.aptsonic.adhelm.approval.fixture",
            expectedClientCodeSigningRequirement: "identifier \"com.aptsonic.adhelm.client\"",
            expectedHostCodeSigningRequirement: "identifier \"com.aptsonic.adhelm.host\"",
            signingKeyApplicationTag: Data("com.aptsonic.adhelm.approval.fixture.v1".utf8),
            keyStorage: .keychainP256
        )
    }

    private func quotedValue(named name: String, in source: String) throws -> String? {
        let expression = try NSRegularExpression(pattern: "\\\"\(name)\\\":\\\"([^\\\"]+)\\\"")
        guard let match = expression.firstMatch(in: source, range: NSRange(source.startIndex..., in: source)),
              let range = Range(match.range(at: 1), in: source)
        else { return nil }
        return String(source[range])
    }

    private func failure(_ result: Result<SignedApprovalReceipt, ApprovalHostError>?) -> ApprovalHostError? {
        guard case let .failure(error)? = result else { return nil }
        return error
    }
}

private struct Metadata: Decodable {
    let actionPlanDigest: String
    let actionSubplanDigest: String
    let wholeSecondsActionPlanDigest: String
    let wholeSecondsActionSubplanDigest: String
    let policyDigest: String

    enum CodingKeys: String, CodingKey {
        case actionPlanDigest = "action_plan_digest"
        case actionSubplanDigest = "action_subplan_digest"
        case wholeSecondsActionPlanDigest = "whole_seconds_action_plan_digest"
        case wholeSecondsActionSubplanDigest = "whole_seconds_action_subplan_digest"
        case policyDigest = "policy_digest"
    }
}

private final class FakeReviewer: ApprovalReviewer {
    private(set) var calls = 0
    private var completion: ((ApprovalReviewDecision) -> Void)?

    func present(
        review _: DeterministicApprovalReview,
        completion: @escaping (ApprovalReviewDecision) -> Void
    ) -> any ApprovalCancellation {
        calls += 1
        self.completion = completion
        return NoopApprovalCancellation()
    }

    func respond(_ decision: ApprovalReviewDecision) {
        completion?(decision)
    }
}

private final class FakeAuthenticator: FreshAuthenticator {
    private(set) var calls = 0
    private var completion: ((ApprovalAuthenticationResult) -> Void)?

    func authenticate(
        reason _: String,
        completion: @escaping (ApprovalAuthenticationResult) -> Void
    ) -> any ApprovalCancellation {
        calls += 1
        self.completion = completion
        return NoopApprovalCancellation()
    }

    func respond(_ result: ApprovalAuthenticationResult) {
        completion?(result)
    }
}

private final class FakeSigner: ReceiptSigner {
    private(set) var calls = 0
    private(set) var payload: ReceiptSigningPayload?
    private(set) var permit: (any ReceiptSigningPermit)?
    private var completion: ((Result<Data, ApprovalHostError>) -> Void)?

    func sign(
        _ payload: ReceiptSigningPayload,
        authenticatedSession _: any AuthenticatedApprovalSession,
        permit: any ReceiptSigningPermit,
        completion: @escaping (Result<Data, ApprovalHostError>) -> Void
    ) -> any ApprovalCancellation {
        calls += 1
        self.payload = payload
        self.permit = permit
        self.completion = completion
        return NoopApprovalCancellation()
    }

    func respond(_ result: Result<Data, ApprovalHostError>) {
        if case .success = result {
            _ = claimPermit()
        }
        completion?(result)
    }

    func respondWithoutClaim(_ result: Result<Data, ApprovalHostError>) {
        completion?(result)
    }

    func claimPermit() -> Bool {
        permit?.claimSigning() ?? false
    }
}

private final class FakeAuthenticatedSession: AuthenticatedApprovalSession {
    private(set) var invalidated = false
    func invalidate() { invalidated = true }
}

private final class FakeScheduler: ApprovalDeadlineScheduler {
    private var actions: [() -> Void] = []

    func schedule(after _: TimeInterval, action: @escaping () -> Void) -> any ApprovalCancellation {
        actions.append(action)
        return NoopApprovalCancellation()
    }

    func fireLatest() { actions.last?() }
}

private final class FixedIdentifierGenerator: ReceiptIdentifierGenerator {
    func nextReceiptIdentifier() -> String {
        "approval-receipt:" + String(repeating: "A", count: 43)
    }
}
