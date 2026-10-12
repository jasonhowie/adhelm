import CryptoKit
import Foundation
import XCTest
@testable import ApprovalHostCore

final class Ga4KeyEventCreateApprovalTests: XCTestCase {
    private enum FixtureError: Error {
        case unavailable
    }

    private let now = Date(timeIntervalSince1970: 1_893_456_060)

    func testActualPythonGa4CreateFixtureReconstructsPlanAndRedactedReviewFacts() throws {
        let challenge = try ApprovalChallenge.decode(
            fixture("ga4-key-event-create-challenge-v1"), now: now
        )
        XCTAssertEqual(challenge.kind, .ga4KeyEventCreatePlan)
        guard case .ga4KeyEventCreatePlan(let grantorRef, let action) = challenge.subject else {
            return XCTFail("expected fixed GA4 key-event CREATE action")
        }
        XCTAssertEqual(grantorRef, "customer-grantor-1")
        XCTAssertEqual(action.plan.maximumCurrency, "USD")
        XCTAssertEqual(action.semanticState.beforeMemberCount, 0)
        XCTAssertEqual(action.semanticState.afterMemberCount, 1)
        XCTAssertEqual(action.semanticState.documentedKeyEventCap, 30)
        XCTAssertEqual(action.plan.expiresAtText, "2030-01-01T00:10:00Z")

        let review = try DeterministicApprovalReview.make(for: challenge)
        XCTAssertEqual(review.facts, fixture("ga4-key-event-create-review-v1"))
        XCTAssertTrue(review.text.contains("GA4 key-event CREATE"))
        XCTAssertTrue(review.text.contains("0 → 1 key events"))
        XCTAssertTrue(review.text.contains("0 minor units (USD)"))
        XCTAssertTrue(review.text.contains("no remote compensation"))
        XCTAssertTrue(review.text.contains("ga4.edit.profile"))
        XCTAssertTrue(review.text.contains("GA4 property 123, web stream 456"))
        XCTAssertTrue(review.text.contains("Key event: qualified_request"))
        XCTAssertFalse(review.text.contains("customer-defined qualified request"))
        XCTAssertFalse(review.text.contains("fixture-edit-token"))
    }

    func testEphemeralP256ReceiptBindsTheExactGa4Challenge() throws {
        let challenge = try ApprovalChallenge.decode(
            fixture("ga4-key-event-create-challenge-v1"), now: now
        )
        let key = P256.Signing.PrivateKey()
        let review = try DeterministicApprovalReview.make(for: challenge)
        let unsigned = try ApprovalReceiptFactory.makeUnsigned(
            challenge: challenge,
            review: review,
            configuration: configuration(),
            now: now,
            receiptIdentifier: "approval-receipt:" + String(repeating: "A", count: 43)
        )
        let signature = try key.signature(for: unsigned.signingPayload.domainSeparatedBytes)
        let signed = try ApprovalReceiptFactory.finalize(unsigned, signature: signature.derRepresentation)
        let verifier = try PinnedReceiptVerifier(
            configuration: configuration(), trustedPublicKeyX963: key.publicKey.x963Representation
        )
        XCTAssertEqual(
            try verifier.verify(signed.canonicalReceipt, expectedChallenge: challenge, now: now), signed
        )
        XCTAssertTrue(signed.canonicalReceipt.contains(Data("\"action_digest\"".utf8)))
        XCTAssertTrue(signed.canonicalReceipt.contains(Data("\"plan_digest\"".utf8)))
        XCTAssertTrue(signed.canonicalReceipt.contains(Data("\"subplan_digests\"".utf8)))
    }

    func testRejectsAlteredGa4StateBeforeReview() throws {
        let source = String(data: fixture("ga4-key-event-create-challenge-v1"), encoding: .utf8)!
        let forged = source.replacingOccurrences(
            of: "\"execution_authority\":false", with: "\"execution_authority\":true"
        )
        XCTAssertThrowsError(try ApprovalChallenge.decode(Data(forged.utf8), now: now))
    }

    func testRejectsCandidateCountingAndConversionFieldBindingDriftBeforePlanReview() throws {
        let oncePerSession = try candidateRecord { compilerCandidate, _ in
            guard case .object(var request) = compilerCandidate["request"] else {
                return XCTFail("fixture request is unavailable")
            }
            request["countingMethod"] = .string("ONCE_PER_SESSION")
            compilerCandidate["request"] = .object(request)
        }
        XCTAssertThrowsError(try ExactGa4Candidate.decode(oncePerSession))

        let divergentFields = try candidateRecord { compilerCandidate, _ in
            compilerCandidate["declared_field_names"] = .array([.string("another_boolean")])
        }
        XCTAssertThrowsError(try ExactGa4Candidate.decode(divergentFields))

        let sensitiveName = try candidateRecord { compilerCandidate, _ in
            guard case .object(var request) = compilerCandidate["request"] else {
                return XCTFail("fixture request is unavailable")
            }
            request["eventName"] = .string("email_request")
            compilerCandidate["request"] = .object(request)
        }
        XCTAssertThrowsError(try ExactGa4Candidate.decode(sensitiveName))
    }

    private func candidateRecord(
        mutate: (inout [String: CanonicalJSONValue], inout [String: CanonicalJSONValue]) -> Void
    ) throws -> CanonicalJSONValue {
        guard case .object(let action) = try CanonicalJSON.parseCanonical(
            fixture("ga4-key-event-create-action-v1")
        ),
            case .object(var candidate) = action["candidate"],
            case .object(var compilation) = candidate["compilation"],
            case .object(var compilerCandidate) = compilation["compiler_candidate"]
        else {
            XCTFail("fixture candidate is unavailable")
            throw FixtureError.unavailable
        }
        mutate(&compilerCandidate, &compilation)
        compilation["compiler_candidate"] = .object(compilerCandidate)
        compilation["compiler_candidate_digest"] = .string(
            try CanonicalJSON.sha256Hex(CanonicalJSON.encode(.object(compilerCandidate)))
        )
        candidate["compilation"] = .object(compilation)
        candidate["candidate_digest"] = .string(
            try CanonicalJSON.sha256Hex(CanonicalJSON.encode(.object(compilation)))
        )
        return .object(candidate)
    }

    private func fixture(_ name: String) -> Data {
        let url = Bundle.module.url(forResource: name, withExtension: "json", subdirectory: "Fixtures")!
        return try! Data(contentsOf: url)
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
}
