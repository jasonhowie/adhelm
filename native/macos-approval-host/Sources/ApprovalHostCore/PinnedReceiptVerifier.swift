import CryptoKit
import Foundation

public enum ReceiptVerificationError: Error, Equatable, Sendable {
    case invalidPinnedKey
    case malformedReceipt
    case receiptExpired
    case invalidIssueTime
    case bindingMismatch
    case invalidSignature
}

/// Cryptographic receipt verification against an already trusted public key.
/// This pure verifier does not enroll a key, authenticate the OS user, consume
/// a challenge, append a grant, or authorize a provider operation. An installed
/// integration must obtain its pin from protected enrollment and enforce durable
/// nonce/receipt consumption and policy revocation before granting authority.
public struct PinnedReceiptVerifier {
    private let configuration: TrustedHostConfiguration
    private let publicKey: P256.Signing.PublicKey
    public let publicKeyFingerprint: String

    /// The key is a trusted integration input, never a field in a receipt or
    /// challenge. Only an uncompressed X9.63 P-256 public key is accepted.
    public init(configuration: TrustedHostConfiguration, trustedPublicKeyX963: Data) throws {
        guard trustedPublicKeyX963.count == 65, trustedPublicKeyX963.first == 4 else {
            throw ReceiptVerificationError.invalidPinnedKey
        }
        do {
            publicKey = try P256.Signing.PublicKey(x963Representation: trustedPublicKeyX963)
        } catch {
            throw ReceiptVerificationError.invalidPinnedKey
        }
        self.configuration = configuration
        publicKeyFingerprint = CanonicalJSON.sha256Hex(trustedPublicKeyX963)
    }

    public func verify(
        _ receiptData: Data,
        expectedChallenge: ApprovalChallenge,
        now: Date
    ) throws -> SignedApprovalReceipt {
        let candidate = try Self.boundReceipt(
            receiptData,
            expectedChallenge: expectedChallenge,
            configuration: configuration,
            now: now
        )
        let signatureBytes = try Self.signatureBytes(candidate.signatureText)
        let signature: P256.Signing.ECDSASignature
        do {
            signature = try P256.Signing.ECDSASignature(derRepresentation: signatureBytes)
        } catch {
            throw ReceiptVerificationError.invalidSignature
        }
        guard signature.derRepresentation == signatureBytes,
              publicKey.isValidSignature(signature, for: candidate.unsigned.signingPayload.domainSeparatedBytes) else {
            throw ReceiptVerificationError.invalidSignature
        }
        do {
            return try ApprovalReceiptFactory.finalize(candidate.unsigned, signature: signatureBytes)
        } catch {
            throw ReceiptVerificationError.malformedReceipt
        }
    }

    /// Performs the bounded canonical parsing, expected-challenge binding, and
    /// time checks that precede any protected-key lookup.  It verifies neither
    /// a pin nor a signature and therefore grants no authority on its own.
    /// The native fixed-slot coordinator calls this before its protected
    /// provenance/key reads, then invokes `verify` under its lease after it
    /// has obtained the current enrolled public key.
    package static func preflight(
        _ receiptData: Data,
        expectedChallenge: ApprovalChallenge,
        configuration: TrustedHostConfiguration,
        now: Date
    ) throws {
        let candidate = try boundReceipt(
            receiptData,
            expectedChallenge: expectedChallenge,
            configuration: configuration,
            now: now
        )
        // This only rejects an impossible/non-canonical signature encoding;
        // it does not verify the signature against a key.
        _ = try signatureBytes(candidate.signatureText)
    }

    private static func boundReceipt(
        _ receiptData: Data,
        expectedChallenge: ApprovalChallenge,
        configuration: TrustedHostConfiguration,
        now: Date
    ) throws -> (signatureText: String, unsigned: UnsignedApprovalReceipt) {
        guard now.timeIntervalSince1970.isFinite,
              now < expectedChallenge.expiresAt else {
            throw ReceiptVerificationError.receiptExpired
        }
        let parsed: CanonicalJSONValue
        do {
            parsed = try CanonicalJSON.parseCanonical(
                receiptData,
                limits: CanonicalJSONLimits(
                    maximumBytes: 16_384, maximumDepth: 4,
                    maximumContainerItems: 32, maximumStringBytes: 1_024
                )
            )
        } catch {
            throw ReceiptVerificationError.malformedReceipt
        }
        guard case var .object(fields) = parsed,
              case let .string(signatureText) = fields.removeValue(forKey: "signature"),
              case let .string(issuedAtText) = fields["issued_at"],
              case let .string(receiptID) = fields["receipt_id"] else {
            throw ReceiptVerificationError.malformedReceipt
        }
        let issuedAt = try Self.issueTime(issuedAtText)
        let skew = configuration.limits.maximumIssuedAtSkew
        guard issuedAt >= expectedChallenge.issuedAt.addingTimeInterval(-skew),
              issuedAt <= now.addingTimeInterval(skew),
              issuedAt < expectedChallenge.expiresAt else {
            throw ReceiptVerificationError.invalidIssueTime
        }
        let unsigned: UnsignedApprovalReceipt
        do {
            let review = try DeterministicApprovalReview.make(for: expectedChallenge)
            unsigned = try ApprovalReceiptFactory.makeUnsigned(
                challenge: expectedChallenge, review: review, configuration: configuration,
                now: issuedAt, receiptIdentifier: receiptID
            )
            // This single exact comparison covers every required field, kind,
            // digest, ordered subplan, nonce, host/key identity and expiry, and
            // rejects extra fields even when an attacker signs those fields.
            guard try CanonicalJSON.encode(.object(fields)) == unsigned.canonicalUnsignedReceipt else {
                throw ReceiptVerificationError.bindingMismatch
            }
        } catch let error as ReceiptVerificationError {
            throw error
        } catch {
            throw ReceiptVerificationError.bindingMismatch
        }
        return (signatureText, unsigned)
    }

    private static func signatureBytes(_ raw: String) throws -> Data {
        guard (8...128).contains(raw.utf8.count),
              raw.unicodeScalars.allSatisfy({
                  (65...90).contains($0.value) || (97...122).contains($0.value)
                  || (48...57).contains($0.value) || $0.value == 45 || $0.value == 95
              }) else {
            throw ReceiptVerificationError.invalidSignature
        }
        let base64 = raw.replacingOccurrences(of: "-", with: "+")
            .replacingOccurrences(of: "_", with: "/")
        let padded = base64 + String(repeating: "=", count: (4 - base64.count % 4) % 4)
        guard let decoded = Data(base64Encoded: padded), base64URLEncoded(decoded) == raw else {
            throw ReceiptVerificationError.invalidSignature
        }
        return decoded
    }

    private static func issueTime(_ raw: String) throws -> Date {
        // The host's receipt factory emits exactly UTC milliseconds. Round-trip
        // equality rejects normalized invalid dates, offsets and alternate text.
        guard raw.utf8.count == 24 else { throw ReceiptVerificationError.invalidIssueTime }
        let formatter = ISO8601DateFormatter()
        formatter.timeZone = TimeZone(secondsFromGMT: 0)
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        guard let date = formatter.date(from: raw), ReceiptTimestamp.encode(date) == raw else {
            throw ReceiptVerificationError.invalidIssueTime
        }
        return date
    }
}
