import CryptoKit
import Foundation
import XCTest
@testable import ApprovalHostCore

final class ReceiptVerificationTests: XCTestCase {
    private let now = Date(timeIntervalSince1970: 1_893_456_030)

    func testActualEphemeralP256SignaturesVerifyForEveryClosedReceiptKind() throws {
        let key = P256.Signing.PrivateKey()
        let verifier = try verifier(key)
        for name in ["openai-pause-challenge-v1", "openai-pause-whole-seconds-challenge-v1",
                     "delegation-policy-install-challenge-v1", "delegation-policy-revoke-challenge-v1"] {
            let challenge = try challenge(name)
            let signed = try receipt(challenge, key: key)
            let checked = try verifier.verify(signed.canonicalReceipt, expectedChallenge: challenge, now: now)
            XCTAssertEqual(checked, signed)
            XCTAssertEqual(checked.interactionReference, "evidence:sha256:" + signed.digest)
            // Verification itself has no replay-consumption side effect. The
            // installed grant path must add its own durable one-shot check.
            XCTAssertEqual(try verifier.verify(signed.canonicalReceipt, expectedChallenge: challenge, now: now), checked)
        }
    }

    func testWrongKeyAndTamperedSignatureFailCryptographically() throws {
        let key = P256.Signing.PrivateKey()
        let challenge = try challenge()
        let signed = try receipt(challenge, key: key)
        XCTAssertThrowsError(try verifier(P256.Signing.PrivateKey()).verify(
            signed.canonicalReceipt, expectedChallenge: challenge, now: now
        ))
        let badSignature = try mutate(signed.canonicalReceipt) { fields in
            fields["signature"] = .string(base64URLEncoded(Data(repeating: 0, count: 70)))
        }
        XCTAssertThrowsError(try verifier(key).verify(badSignature, expectedChallenge: challenge, now: now))
    }

    func testEveryUnsignedFieldIsBoundEvenWhenModifiedReceiptHasAValidSignature() throws {
        let key = P256.Signing.PrivateKey()
        let challenge = try challenge()
        let signed = try receipt(challenge, key: key)
        guard case let .object(original) = try CanonicalJSON.parseCanonical(signed.canonicalReceipt) else {
            return XCTFail("expected receipt")
        }
        for name in original.keys where !["signature", "issued_at", "receipt_id"].contains(name) {
            let changed = try mutate(signed.canonicalReceipt, resign: key) { fields in
                if case .array = fields[name] { fields[name] = .array([]) }
                else if case .integer = fields[name] { fields[name] = .integer(2) }
                else { fields[name] = .string("modified") }
            }
            XCTAssertThrowsError(try verifier(key).verify(changed, expectedChallenge: challenge, now: now), name)
        }
        let extra = try mutate(signed.canonicalReceipt, resign: key) { $0["extra"] = .bool(true) }
        XCTAssertThrowsError(try verifier(key).verify(extra, expectedChallenge: challenge, now: now))
    }

    func testChallengeNonceAndReceiptKindsCannotBeRebound() throws {
        let key = P256.Signing.PrivateKey()
        let original = try challenge()
        let signed = try receipt(original, key: key)
        let changedBytes = try mutate(original.canonicalBytes) {
            $0["nonce"] = .string(base64URLEncoded(Data(repeating: 0xFA, count: 32)))
        }
        let changed = try ApprovalChallenge.decode(changedBytes, now: now)
        XCTAssertThrowsError(try verifier(key).verify(signed.canonicalReceipt, expectedChallenge: changed, now: now))
        let install = try challenge("delegation-policy-install-challenge-v1")
        let revoke = try challenge("delegation-policy-revoke-challenge-v1")
        let installReceipt = try receipt(install, key: key)
        XCTAssertThrowsError(try verifier(key).verify(installReceipt.canonicalReceipt, expectedChallenge: revoke, now: now))
    }

    func testExactExpiryInvalidCalendarAndSignedFutureIssueFail() throws {
        let key = P256.Signing.PrivateKey()
        let challenge = try challenge()
        let signed = try receipt(challenge, key: key)
        XCTAssertThrowsError(try verifier(key).verify(signed.canonicalReceipt, expectedChallenge: challenge,
                                                     now: challenge.expiresAt))
        for timestamp in ["2030-02-30T00:00:00.000Z", "2030-01-01T00:00:60.000Z",
                          "2030-01-01T00:03:00.000Z", "2029-12-31T23:58:00.000Z",
                          "2030-01-01T00:00:30Z", "2030-01-01T00:00:30.000000Z"] {
            let changed = try mutate(signed.canonicalReceipt, resign: key) { $0["issued_at"] = .string(timestamp) }
            XCTAssertThrowsError(try verifier(key).verify(changed, expectedChallenge: challenge, now: now), timestamp)
        }
    }

    func testDuplicateNoncanonicalAndOversizedReceiptsFailBeforeSignatureVerification() throws {
        let key = P256.Signing.PrivateKey()
        let challenge = try challenge()
        let signed = try receipt(challenge, key: key)
        for malformed in [signed.canonicalReceipt + Data("\n".utf8),
                          Data("{\"schema_version\":1,\"schema_version\":1}".utf8),
                          Data(repeating: 32, count: 16_385)] {
            XCTAssertThrowsError(try verifier(key).verify(malformed, expectedChallenge: challenge, now: now))
        }
        XCTAssertThrowsError(try PinnedReceiptVerifier(configuration: configuration(), trustedPublicKeyX963: Data()))
        XCTAssertThrowsError(try PinnedReceiptVerifier(configuration: configuration(), trustedPublicKeyX963: Data(repeating: 4, count: 65)))
    }

    private func challenge(_ name: String = "openai-pause-challenge-v1") throws -> ApprovalChallenge {
        let url = try XCTUnwrap(Bundle.module.url(forResource: name, withExtension: "json", subdirectory: "Fixtures"))
        return try ApprovalChallenge.decode(Data(contentsOf: url), now: now)
    }

    private func configuration() throws -> TrustedHostConfiguration {
        try TrustedHostConfiguration(hostReference: "host:adhelm-macos-approval-v1", signingKeyReference: "key:fixture",
            machServiceName: "invalid.fixture.service", expectedClientCodeSigningRequirement: "never",
            expectedHostCodeSigningRequirement: "never", signingKeyApplicationTag: Data("fixture".utf8), keyStorage: .keychainP256)
    }

    private func verifier(_ key: P256.Signing.PrivateKey) throws -> PinnedReceiptVerifier {
        try PinnedReceiptVerifier(configuration: configuration(), trustedPublicKeyX963: key.publicKey.x963Representation)
    }

    private func receipt(_ challenge: ApprovalChallenge, key: P256.Signing.PrivateKey) throws -> SignedApprovalReceipt {
        let unsigned = try ApprovalReceiptFactory.makeUnsigned(challenge: challenge,
            review: DeterministicApprovalReview.make(for: challenge), configuration: configuration(), now: now,
            receiptIdentifier: "approval-receipt:" + base64URLEncoded(Data(repeating: 0xCD, count: 32)))
        let signature = try key.signature(for: unsigned.signingPayload.domainSeparatedBytes)
        return try ApprovalReceiptFactory.finalize(unsigned, signature: signature.derRepresentation)
    }

    private func mutate(_ raw: Data, resign key: P256.Signing.PrivateKey? = nil,
                        change: (inout [String: CanonicalJSONValue]) -> Void) throws -> Data {
        guard case var .object(fields) = try CanonicalJSON.parseCanonical(raw) else {
            throw ReceiptVerificationError.malformedReceipt
        }
        change(&fields)
        if let key {
            fields.removeValue(forKey: "signature")
            let unsigned = try CanonicalJSON.encode(.object(fields))
            var payload = Data("adhelm.macos-approval-host.receipt.v1\u{0}OPENAI_PAUSE_PLAN\u{0}".utf8)
            payload.append(unsigned)
            fields["signature"] = .string(base64URLEncoded(try key.signature(for: payload).derRepresentation))
        }
        return try CanonicalJSON.encode(.object(fields))
    }
}
