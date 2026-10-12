import Foundation

public enum ReceiptSignatureAlgorithm: String, Equatable, Sendable {
    case ecdsaP256SHA256X962 = "ECDSA_P256_SHA256_X9_62"
}

public struct ReceiptSigningPayload: Equatable, Sendable {
    /// The only construction path is the typed receipt factory.  XPC exposes no
    /// operation that accepts this value or arbitrary bytes for signing.
    public let domainSeparatedBytes: Data

    fileprivate init(kind: ApprovalKind, unsignedReceipt: Data) {
        var bytes = Data("adhelm.macos-approval-host.receipt.v1\u{0}".utf8)
        bytes.append(contentsOf: kind.rawValue.utf8)
        bytes.append(0)
        bytes.append(unsignedReceipt)
        domainSeparatedBytes = bytes
    }
}

/// Bound to the request that completed review and fresh OS authentication.
/// Production signers check it immediately before protected-key work; it has
/// no XPC construction or serialization path.
public protocol ReceiptSigningPermit: AnyObject, Sendable {
    func permitsSigning() -> Bool
    /// The concrete protected signer must claim this immediately before its
    /// first protected-key operation. A claim is one-use and marks the
    /// challenge consumed; a cancellation before this point prevents signing.
    func claimSigning() -> Bool
    /// The state machine accepts a successful signature only after the signer
    /// made the one-use claim. This prevents an injected adapter from turning
    /// an unclaimed permit into a receipt.
    func hasClaimedSigning() -> Bool
}

public protocol ReceiptSigner: AnyObject {
    @discardableResult
    func sign(
        _ payload: ReceiptSigningPayload,
        authenticatedSession: any AuthenticatedApprovalSession,
        permit: any ReceiptSigningPermit,
        completion: @escaping (Result<Data, ApprovalHostError>) -> Void
    ) -> any ApprovalCancellation
}

public protocol ReceiptIdentifierGenerator: AnyObject {
    func nextReceiptIdentifier() -> String
}

public final class SystemReceiptIdentifierGenerator: ReceiptIdentifierGenerator {
    public init() {}

    public func nextReceiptIdentifier() -> String {
        var generator = SystemRandomNumberGenerator()
        let bytes = (0..<32).map { _ in UInt8.random(in: UInt8.min...UInt8.max, using: &generator) }
        return "approval-receipt:" + base64URLEncoded(Data(bytes))
    }
}

public struct UnsignedApprovalReceipt: Equatable, Sendable {
    public let kind: ApprovalKind
    public let receiptIdentifier: String
    public let canonicalUnsignedReceipt: Data
    public let signingPayload: ReceiptSigningPayload

    fileprivate let unsignedValue: CanonicalJSONValue
}

public struct SignedApprovalReceipt: Equatable, Sendable {
    public let kind: ApprovalKind
    public let receiptIdentifier: String
    public let canonicalReceipt: Data
    public let digest: String
    public let signature: Data

    /// This opaque digest reference is suitable only as the host's interaction
    /// evidence reference after a future enrolled verifier checks the receipt.
    public var interactionReference: String { "evidence:sha256:\(digest)" }
}

public enum ApprovalReceiptFactory {
    public static func makeUnsigned(
        challenge: ApprovalChallenge,
        review: DeterministicApprovalReview,
        configuration: TrustedHostConfiguration,
        now: Date,
        receiptIdentifier: String
    ) throws -> UnsignedApprovalReceipt {
        guard isValidReceiptIdentifier(receiptIdentifier), now < challenge.expiresAt else {
            throw now < challenge.expiresAt ? ApprovalHostError.signingFailed : ApprovalHostError.challengeExpired
        }
        let issuedAt = ReceiptTimestamp.encode(now)
        var fields: [String: CanonicalJSONValue] = [
            "authentication_mode": .string("DEVICE_OWNER_AUTHENTICATION"),
            "challenge_digest": .string(challenge.digest),
            "challenge_nonce": .string(base64URLEncoded(challenge.nonce)),
            "expires_at": .string(challenge.expiresAtText),
            "host_ref": .string(configuration.hostReference),
            "issued_at": .string(issuedAt),
            "key_ref": .string(configuration.signingKeyReference),
            "receipt_id": .string(receiptIdentifier),
            "receipt_kind": .string(challenge.kind.rawValue),
            "review_digest": .string(review.digest),
            "schema_version": .integer(1),
            "signature_algorithm": .string(ReceiptSignatureAlgorithm.ecdsaP256SHA256X962.rawValue),
        ]
        switch challenge.subject {
        case let .openAIPausePlan(grantorRef, action):
            fields["grantor_ref"] = .string(grantorRef)
            fields["action_digest"] = .string(action.digest)
            fields["plan_digest"] = .string(action.plan.digest)
            fields["subplan_digests"] = .array(action.plan.subplans.map { .string($0.digest) })
        case let .ga4KeyEventCreatePlan(grantorRef, action):
            fields["grantor_ref"] = .string(grantorRef)
            fields["action_digest"] = .string(action.digest)
            fields["plan_digest"] = .string(action.plan.digest)
            fields["subplan_digests"] = .array([.string(action.plan.subplanDigest)])
        case let .metaCampaignPausePlan(grantorRef, action):
            fields["grantor_ref"] = .string(grantorRef)
            fields["action_digest"] = .string(action.digest)
            fields["plan_digest"] = .string(action.plan.digest)
            fields["subplan_digests"] = .array(action.plan.subplans.map { .string($0.digest) })
        case let .delegationPolicyInstall(policy):
            try bindPolicy(policy, to: configuration, fields: &fields)
        case let .delegationPolicyRevoke(policy, _, revokedAtText):
            try bindPolicy(policy, to: configuration, fields: &fields)
            fields["revoked_at"] = .string(revokedAtText)
        }
        let unsignedValue = CanonicalJSONValue.object(fields)
        let canonicalUnsignedReceipt = try CanonicalJSON.encode(unsignedValue)
        return UnsignedApprovalReceipt(
            kind: challenge.kind,
            receiptIdentifier: receiptIdentifier,
            canonicalUnsignedReceipt: canonicalUnsignedReceipt,
            signingPayload: ReceiptSigningPayload(kind: challenge.kind, unsignedReceipt: canonicalUnsignedReceipt),
            unsignedValue: unsignedValue
        )
    }

    public static func finalize(
        _ unsigned: UnsignedApprovalReceipt,
        signature: Data
    ) throws -> SignedApprovalReceipt {
        guard !signature.isEmpty, signature.count <= 512 else { throw ApprovalHostError.signingFailed }
        guard case var .object(fields) = unsigned.unsignedValue else { throw ApprovalHostError.signingFailed }
        fields["signature"] = .string(base64URLEncoded(signature))
        let canonicalReceipt = try CanonicalJSON.encode(.object(fields))
        return SignedApprovalReceipt(
            kind: unsigned.kind,
            receiptIdentifier: unsigned.receiptIdentifier,
            canonicalReceipt: canonicalReceipt,
            digest: CanonicalJSON.sha256Hex(canonicalReceipt),
            signature: signature
        )
    }

    private static func bindPolicy(
        _ policy: ExactDelegationPolicy,
        to configuration: TrustedHostConfiguration,
        fields: inout [String: CanonicalJSONValue]
    ) throws {
        guard policy.configurationHostRef == configuration.hostReference else {
            throw ApprovalHostError.malformedChallenge
        }
        fields["grantor_ref"] = .string(policy.grantorRef)
        fields["policy_digest"] = .string(policy.digest)
        fields["policy_id"] = .string(policy.policyID)
        fields["policy_version"] = .integer(policy.policyVersion)
    }

    private static func isValidReceiptIdentifier(_ value: String) -> Bool {
        isExactApprovalReceiptIdentifier(value)
    }
}

/// Exact receipt identifiers have an ASCII prefix and 43 URL-safe ASCII bytes.
/// Regex end anchors may match before a final line terminator, so acceptance
/// checks every byte rather than a matching substring.
func isExactApprovalReceiptIdentifier(_ value: String) -> Bool {
    let prefix = "approval-receipt:"
    guard value.hasPrefix(prefix), value.utf8.count == prefix.utf8.count + 43 else {
        return false
    }
    return value.utf8.dropFirst(prefix.utf8.count).allSatisfy {
        (48...57).contains($0) || (65...90).contains($0) || (97...122).contains($0)
            || $0 == 45 || $0 == 95
    }
}

enum ReceiptTimestamp {
    static func encode(_ date: Date) -> String {
        let formatter = ISO8601DateFormatter()
        formatter.timeZone = TimeZone(secondsFromGMT: 0)
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter.string(from: date)
    }
}
