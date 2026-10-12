import CryptoKit
import Foundation
import XCTest
@testable import ApprovalHostCore

/// Independent checks for the pure verifier. These tests deliberately use only
/// in-memory P-256 keys and fixture challenges; they do not touch Keychain, UI,
/// XPC, enrollment, or any provider.
final class ReceiptVerifierUltraReviewTests: XCTestCase {
    private let initialNow = Date(timeIntervalSince1970: 1_893_456_030)

    func testCorrectKeyStillRejectsReceiptBoundToDifferentTrustedHostConfiguration() throws {
        let key = P256.Signing.PrivateKey()
        let challenge = try loadChallenge()
        let signed = try makeReceipt(challenge: challenge, key: key, now: initialNow)
        let mismatchedConfiguration = try configuration(hostReference: "host:another-approved-host")
        let verifier = try PinnedReceiptVerifier(
            configuration: mismatchedConfiguration,
            trustedPublicKeyX963: key.publicKey.x963Representation
        )

        assertVerificationFailure(
            verifier, receipt: signed.canonicalReceipt, challenge: challenge, now: initialNow,
            equals: .bindingMismatch
        )
    }

    func testPolicyReceiptSignedUnderPauseDomainFailsButItsOwnDomainVerifies() throws {
        let key = P256.Signing.PrivateKey()
        let challenge = try loadChallenge("delegation-policy-install-challenge-v1")
        let signed = try makeReceipt(challenge: challenge, key: key, now: initialNow)
        let verifier = try makeVerifier(key)

        let wrongDomain = try resign(
            signed.canonicalReceipt,
            key: key,
            signingKind: .openAIPausePlan
        )
        assertVerificationFailure(
            verifier, receipt: wrongDomain, challenge: challenge, now: initialNow,
            equals: .invalidSignature
        )
        XCTAssertEqual(
            try verifier.verify(signed.canonicalReceipt, expectedChallenge: challenge, now: initialNow),
            signed
        )
    }

    func testVerifierRejectsAlternateBase64PadBitsForAnOtherwiseValidSignature() throws {
        let key = P256.Signing.PrivateKey()
        let challenge = try loadChallenge()
        let verifier = try makeVerifier(key)

        // DER ECDSA signatures vary between 70 and 72 bytes. Varying only the
        // receipt identifier gives an input with unused final base64 bits
        // without relying on a particular CryptoKit DER length.
        for byte in UInt8(0)...UInt8(47) {
            let signed = try makeReceipt(
                challenge: challenge,
                key: key,
                now: initialNow,
                identifierByte: byte
            )
            guard let altered = try alternateSignaturePadBits(in: signed.canonicalReceipt) else {
                continue
            }
            assertVerificationFailure(
                verifier, receipt: altered, challenge: challenge, now: initialNow,
                equals: .invalidSignature
            )
            return
        }
        XCTFail("unable to produce a DER signature with unused base64 pad bits")
    }

    func testReceiptIssuedAfterInitialRequestSkewButBeforeExactExpiryRemainsVerifiable() throws {
        let key = P256.Signing.PrivateKey()
        let challenge = try loadChallenge()
        // The challenge was validated when received. A human review can take
        // longer than the initial 60-second request-age window, as long as the
        // final receipt is issued and verified before the exact expiry.
        let reviewedAt = challenge.issuedAt.addingTimeInterval(120)
        XCTAssertLessThan(reviewedAt, challenge.expiresAt)
        let signed = try makeReceipt(challenge: challenge, key: key, now: reviewedAt)
        let verifier = try makeVerifier(key)

        XCTAssertEqual(
            try verifier.verify(signed.canonicalReceipt, expectedChallenge: challenge, now: reviewedAt),
            signed
        )
    }

    func testFiniteTrustedTimeLimitsAreRequired() throws {
        XCTAssertThrowsError(try configuration(limits: ApprovalHostLimits(
            maximumChallengeLifetime: .infinity,
            maximumIssuedAtSkew: 60
        )))
        XCTAssertThrowsError(try configuration(limits: ApprovalHostLimits(
            maximumChallengeLifetime: 300,
            maximumIssuedAtSkew: .infinity
        )))
    }

    private func loadChallenge(_ name: String = "openai-pause-challenge-v1") throws -> ApprovalChallenge {
        let url = try XCTUnwrap(
            Bundle.module.url(forResource: name, withExtension: "json", subdirectory: "Fixtures")
        )
        return try ApprovalChallenge.decode(Data(contentsOf: url), now: initialNow)
    }

    private func configuration(
        hostReference: String = "host:adhelm-macos-approval-v1",
        limits: ApprovalHostLimits = ApprovalHostLimits()
    ) throws -> TrustedHostConfiguration {
        try TrustedHostConfiguration(
            hostReference: hostReference,
            signingKeyReference: "key:fixture",
            machServiceName: "invalid.fixture.service",
            expectedClientCodeSigningRequirement: "never",
            expectedHostCodeSigningRequirement: "never",
            signingKeyApplicationTag: Data("fixture".utf8),
            keyStorage: .keychainP256,
            limits: limits
        )
    }

    private func makeVerifier(_ key: P256.Signing.PrivateKey) throws -> PinnedReceiptVerifier {
        try PinnedReceiptVerifier(
            configuration: configuration(),
            trustedPublicKeyX963: key.publicKey.x963Representation
        )
    }

    private func makeReceipt(
        challenge: ApprovalChallenge,
        key: P256.Signing.PrivateKey,
        now: Date,
        identifierByte: UInt8 = 0xA5
    ) throws -> SignedApprovalReceipt {
        let identifier = "approval-receipt:" + base64URLEncoded(Data(repeating: identifierByte, count: 32))
        let unsigned = try ApprovalReceiptFactory.makeUnsigned(
            challenge: challenge,
            review: DeterministicApprovalReview.make(for: challenge),
            configuration: configuration(),
            now: now,
            receiptIdentifier: identifier
        )
        let signature = try key.signature(for: unsigned.signingPayload.domainSeparatedBytes)
        return try ApprovalReceiptFactory.finalize(unsigned, signature: signature.derRepresentation)
    }

    private func resign(
        _ raw: Data,
        key: P256.Signing.PrivateKey,
        signingKind: ApprovalKind
    ) throws -> Data {
        guard case var .object(fields) = try CanonicalJSON.parseCanonical(raw) else {
            throw ReceiptVerificationError.malformedReceipt
        }
        fields.removeValue(forKey: "signature")
        let unsigned = try CanonicalJSON.encode(.object(fields))
        var payload = Data("adhelm.macos-approval-host.receipt.v1\u{0}".utf8)
        payload.append(contentsOf: signingKind.rawValue.utf8)
        payload.append(0)
        payload.append(unsigned)
        fields["signature"] = .string(base64URLEncoded(try key.signature(for: payload).derRepresentation))
        return try CanonicalJSON.encode(.object(fields))
    }

    private func alternateSignaturePadBits(in raw: Data) throws -> Data? {
        guard case var .object(fields) = try CanonicalJSON.parseCanonical(raw),
              case let .string(signature) = fields["signature"],
              let decoded = base64URLDecoded(signature)
        else { throw ReceiptVerificationError.malformedReceipt }
        let remainder = decoded.count % 3
        guard remainder != 0, let last = signature.utf8.last,
              let index = base64URLAlphabet.firstIndex(of: last)
        else { return nil }
        let alternateIndex: Int
        switch remainder {
        case 1:
            guard index % 16 == 0 else { throw ReceiptVerificationError.malformedReceipt }
            alternateIndex = index + 1
        case 2:
            guard index % 4 == 0 else { throw ReceiptVerificationError.malformedReceipt }
            alternateIndex = index + 1
        default:
            throw ReceiptVerificationError.malformedReceipt
        }
        var bytes = Array(signature.utf8)
        bytes[bytes.count - 1] = base64URLAlphabet[alternateIndex]
        fields["signature"] = .string(String(decoding: bytes, as: UTF8.self))
        return try CanonicalJSON.encode(.object(fields))
    }

    private func base64URLDecoded(_ raw: String) -> Data? {
        let base64 = raw.replacingOccurrences(of: "-", with: "+")
            .replacingOccurrences(of: "_", with: "/")
        return Data(base64Encoded: base64 + String(repeating: "=", count: (4 - base64.count % 4) % 4))
    }

    private func assertVerificationFailure(
        _ verifier: PinnedReceiptVerifier,
        receipt: Data,
        challenge: ApprovalChallenge,
        now: Date,
        equals expected: ReceiptVerificationError,
        file: StaticString = #filePath,
        line: UInt = #line
    ) {
        XCTAssertThrowsError(
            try verifier.verify(receipt, expectedChallenge: challenge, now: now), file: file, line: line
        ) { error in
            XCTAssertEqual(error as? ReceiptVerificationError, expected, file: file, line: line)
        }
    }
}

private let base64URLAlphabet = Array("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_".utf8)
