import Foundation

/// One canonical private request for the future Meta campaign PAUSE approval bridge V2.
///
/// This is an offline wire contract only. It does not open a pipe, locate a
/// host, resolve a key, perform a review, consume a nonce, or create authority.
/// A later configured boundary must derive its metadata and current-key facts
/// from reviewed static configuration and protected state, not from this value.
package struct NativeMetaCampaignPauseApprovalBridgeRequestV2: Equatable, Sendable,
  CustomStringConvertible,
  CustomDebugStringConvertible
{
  package static let schemaVersion = 2
  package static let messageType = "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_REQUEST_V2"
  package static let maximumWireBytes = 98_304

  package let requestID: Data
  package let sourceContextDigest: String
  package let metadataDigest: String
  package let protocolConfigurationDigest: String
  package let canonicalChallenge: Data
  package let canonicalBytes: Data
  package let digest: String

  package var description: String { "NativeMetaCampaignPauseApprovalBridgeRequestV2(redacted)" }
  package var debugDescription: String { description }

  package init(
    requestID: Data,
    sourceContextDigest: String,
    metadataDigest: String,
    protocolConfigurationDigest: String,
    canonicalChallenge: Data
  ) throws {
    let checkedRequestID = try NativeMetaCampaignPauseApprovalBridgeWireV2.boundedSnapshot(
      requestID,
      maximumBytes: NativeMetaCampaignPauseApprovalBridgeWireV2.requestIDBytes,
      error: .malformedRequest
    )
    let checkedChallenge = try NativeMetaCampaignPauseApprovalBridgeWireV2.boundedSnapshot(
      canonicalChallenge,
      maximumBytes: NativeMetaCampaignPauseApprovalBridgeWireV2.maximumChallengeBytes,
      error: .malformedRequest
    )
    guard checkedRequestID.count == NativeMetaCampaignPauseApprovalBridgeWireV2.requestIDBytes,
      NativeMetaCampaignPauseApprovalBridgeWireV2.isDigest(sourceContextDigest),
      NativeMetaCampaignPauseApprovalBridgeWireV2.isDigest(metadataDigest),
      NativeMetaCampaignPauseApprovalBridgeWireV2.isDigest(protocolConfigurationDigest)
    else { throw NativeApprovalBridgeProtocolError.malformedRequest }
    try NativeMetaCampaignPauseApprovalBridgeWireV2
      .requireStructuralCanonicalMetaCampaignPauseChallenge(checkedChallenge)

    let encoded: Data
    do {
      encoded = try CanonicalJSON.encode(
        .object([
          "schema_version": .integer(Int64(Self.schemaVersion)),
          "message_type": .string(Self.messageType),
          "request_id": .string(base64URLEncoded(checkedRequestID)),
          "source_context_digest": .string(sourceContextDigest),
          "metadata_digest": .string(metadataDigest),
          "protocol_configuration_digest": .string(protocolConfigurationDigest),
          "canonical_challenge_b64url": .string(base64URLEncoded(checkedChallenge)),
        ]))
    } catch {
      throw NativeApprovalBridgeProtocolError.malformedRequest
    }
    guard encoded.count <= Self.maximumWireBytes else {
      throw NativeApprovalBridgeProtocolError.malformedRequest
    }

    self.requestID = checkedRequestID
    self.sourceContextDigest = sourceContextDigest
    self.metadataDigest = metadataDigest
    self.protocolConfigurationDigest = protocolConfigurationDigest
    self.canonicalChallenge = checkedChallenge
    canonicalBytes = encoded
    digest = CanonicalJSON.sha256Hex(encoded)
  }

  /// Decode a standalone wire request without selecting any native metadata,
  /// key, host, clock, or transport. Callers must bind it to compiled metadata
  /// before using it as part of a future installed exchange.
  package static func decode(_ data: Data) throws -> NativeMetaCampaignPauseApprovalBridgeRequestV2
  {
    let snapshot = try NativeMetaCampaignPauseApprovalBridgeWireV2.boundedSnapshot(
      data,
      maximumBytes: maximumWireBytes,
      error: .malformedRequest
    )
    let fields = try NativeMetaCampaignPauseApprovalBridgeWireV2.closedObject(
      snapshot,
      maximumBytes: maximumWireBytes,
      maximumItems: 7,
      expected: [
        "schema_version", "message_type", "request_id", "source_context_digest",
        "metadata_digest", "protocol_configuration_digest", "canonical_challenge_b64url",
      ],
      error: .malformedRequest
    )
    guard fields["schema_version"] == .integer(Int64(schemaVersion)),
      NativeMetaCampaignPauseApprovalBridgeWireV2.exactString(fields["message_type"])
        == messageType,
      let requestIDText = NativeMetaCampaignPauseApprovalBridgeWireV2.exactString(
        fields["request_id"]),
      let sourceContextDigest = NativeMetaCampaignPauseApprovalBridgeWireV2.exactString(
        fields["source_context_digest"]),
      let metadataDigest = NativeMetaCampaignPauseApprovalBridgeWireV2.exactString(
        fields["metadata_digest"]),
      let protocolDigest = NativeMetaCampaignPauseApprovalBridgeWireV2.exactString(
        fields["protocol_configuration_digest"]),
      let challengeText = NativeMetaCampaignPauseApprovalBridgeWireV2.exactString(
        fields["canonical_challenge_b64url"])
    else { throw NativeApprovalBridgeProtocolError.malformedRequest }
    let requestID = try NativeMetaCampaignPauseApprovalBridgeWireV2.decodeBase64URL(
      requestIDText,
      minimumBytes: NativeMetaCampaignPauseApprovalBridgeWireV2.requestIDBytes,
      maximumBytes: NativeMetaCampaignPauseApprovalBridgeWireV2.requestIDBytes,
      error: .malformedRequest
    )
    let challenge = try NativeMetaCampaignPauseApprovalBridgeWireV2.decodeBase64URL(
      challengeText,
      minimumBytes: 1,
      maximumBytes: NativeMetaCampaignPauseApprovalBridgeWireV2.maximumChallengeBytes,
      error: .malformedRequest
    )
    let rebuilt = try Self(
      requestID: requestID,
      sourceContextDigest: sourceContextDigest,
      metadataDigest: metadataDigest,
      protocolConfigurationDigest: protocolDigest,
      canonicalChallenge: challenge
    )
    guard rebuilt.canonicalBytes == snapshot else {
      throw NativeApprovalBridgeProtocolError.malformedRequest
    }
    return rebuilt
  }

  /// Rebuild the exact reviewed build metadata and inspect the expected
  /// challenge under one trusted exchange-start time. This remains pure: it
  /// proves no installation, current key, user interaction, nonce consumption,
  /// or provider authority.
  package func boundChallenge(
    metadata: ApprovalHostBuildMetadataV1,
    exchangeStartedAt: Date
  ) throws -> ApprovalChallenge {
    guard NativeMetaCampaignPauseApprovalBridgeWireV2.isFinite(exchangeStartedAt) else {
      throw NativeApprovalBridgeProtocolError.exchangeTimeInvalid
    }
    let context = try NativeMetaCampaignPauseApprovalBridgeContextV2(
      metadata: metadata, error: .bindingMismatch)
    guard metadataDigest == context.descriptor.metadataDigest,
      protocolConfigurationDigest == context.descriptor.protocolConfigurationDigest
    else { throw NativeApprovalBridgeProtocolError.bindingMismatch }
    do {
      let challenge = try ApprovalChallenge.decode(
        canonicalChallenge,
        now: exchangeStartedAt,
        limits: context.configuration.limits
      )
      guard NativeMetaCampaignPauseApprovalBridgeWireV2.isMetaCampaignPausePlanKind(challenge.kind)
      else {
        throw NativeApprovalBridgeProtocolError.bindingMismatch
      }
      return challenge
    } catch let error as NativeApprovalBridgeProtocolError {
      throw error
    } catch {
      throw NativeApprovalBridgeProtocolError.bindingMismatch
    }
  }
}

/// A bounded private candidate asserted by a future configured Meta campaign PAUSE bridge V2.
///
/// Its status is evidence syntax only in this offline contract. It is not a
/// grant, trust resolver, execution permit, provider credential, or proof that
/// a protected store, host, pipe, review, authentication, or nonce consumption
/// actually ran. The future bridge must construct it only after those steps;
/// this module intentionally provides no such integration.
package struct NativeMetaCampaignPauseApprovalBridgeCandidateV2: Equatable, Sendable,
  CustomStringConvertible,
  CustomDebugStringConvertible
{
  package static let schemaVersion = 2
  package static let messageType = "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_CANDIDATE_V2"
  package static let observationStatus = "NATIVE_CURRENT_RECEIPT_VERIFIED_AND_NONCE_CONSUMED"
  package static let maximumWireBytes = 131_072

  package let requestIDDigest: String
  package let requestDigest: String
  package let sourceContextDigest: String
  package let metadataDigest: String
  package let protocolConfigurationDigest: String
  package let canonicalChallenge: Data
  package let challengeDigest: String
  package let challengeNonceDigest: String
  package let receiptKind: ApprovalKind
  package let expiresAtText: String
  package let canonicalReceipt: Data
  package let receiptDigest: String
  package let receiptIdentifier: String
  package let activePublicKeyX963: Data
  package let activePublicKeyFingerprint: String
  package let nativeObservedAtText: String
  package let canonicalBytes: Data
  package let digest: String

  package var description: String { "NativeMetaCampaignPauseApprovalBridgeCandidateV2(redacted)" }
  package var debugDescription: String { description }

  /// Parse a candidate only against the one locally retained request, reviewed
  /// metadata, and trusted exchange time window that produced it. This verifies
  /// receipt cryptography with the candidate's exact X9.63 key, but it does not
  /// resolve or trust that key independently and does not accept a Python grant.
  package static func decode(
    _ data: Data,
    expectedRequest: NativeMetaCampaignPauseApprovalBridgeRequestV2,
    metadata: ApprovalHostBuildMetadataV1,
    exchangeStartedAt: Date,
    currentNow: Date
  ) throws -> NativeMetaCampaignPauseApprovalBridgeCandidateV2 {
    let snapshot = try NativeMetaCampaignPauseApprovalBridgeWireV2.boundedSnapshot(
      data,
      maximumBytes: maximumWireBytes,
      error: .malformedCandidate
    )
    let fields = try NativeMetaCampaignPauseApprovalBridgeWireV2.closedObject(
      snapshot,
      maximumBytes: maximumWireBytes,
      maximumItems: 19,
      expected: [
        "schema_version", "message_type", "native_observation_status", "request_id_digest",
        "request_digest", "source_context_digest", "metadata_digest",
        "protocol_configuration_digest", "canonical_challenge_b64url", "challenge_digest",
        "challenge_nonce_digest", "receipt_kind", "expires_at", "canonical_receipt_b64url",
        "receipt_digest", "receipt_id", "active_public_key_x963_b64url",
        "active_public_key_fingerprint", "native_observed_at",
      ],
      error: .malformedCandidate
    )
    guard fields["schema_version"] == .integer(Int64(schemaVersion)),
      NativeMetaCampaignPauseApprovalBridgeWireV2.exactString(fields["message_type"])
        == messageType,
      NativeMetaCampaignPauseApprovalBridgeWireV2.exactString(fields["native_observation_status"])
        == observationStatus,
      let requestIDDigest = NativeMetaCampaignPauseApprovalBridgeWireV2.exactString(
        fields["request_id_digest"]),
      let requestDigest = NativeMetaCampaignPauseApprovalBridgeWireV2.exactString(
        fields["request_digest"]),
      let sourceContextDigest = NativeMetaCampaignPauseApprovalBridgeWireV2.exactString(
        fields["source_context_digest"]),
      let metadataDigest = NativeMetaCampaignPauseApprovalBridgeWireV2.exactString(
        fields["metadata_digest"]),
      let protocolDigest = NativeMetaCampaignPauseApprovalBridgeWireV2.exactString(
        fields["protocol_configuration_digest"]),
      let challengeText = NativeMetaCampaignPauseApprovalBridgeWireV2.exactString(
        fields["canonical_challenge_b64url"]),
      let challengeDigest = NativeMetaCampaignPauseApprovalBridgeWireV2.exactString(
        fields["challenge_digest"]),
      let nonceDigest = NativeMetaCampaignPauseApprovalBridgeWireV2.exactString(
        fields["challenge_nonce_digest"]),
      let receiptKindText = NativeMetaCampaignPauseApprovalBridgeWireV2.exactString(
        fields["receipt_kind"]),
      let expiresAtText = NativeMetaCampaignPauseApprovalBridgeWireV2.exactString(
        fields["expires_at"]),
      let receiptText = NativeMetaCampaignPauseApprovalBridgeWireV2.exactString(
        fields["canonical_receipt_b64url"]),
      let receiptDigest = NativeMetaCampaignPauseApprovalBridgeWireV2.exactString(
        fields["receipt_digest"]),
      let receiptIdentifier = NativeMetaCampaignPauseApprovalBridgeWireV2.exactString(
        fields["receipt_id"]),
      let publicKeyText = NativeMetaCampaignPauseApprovalBridgeWireV2.exactString(
        fields["active_public_key_x963_b64url"]),
      let publicKeyFingerprint = NativeMetaCampaignPauseApprovalBridgeWireV2.exactString(
        fields["active_public_key_fingerprint"]),
      let nativeObservedAtText = NativeMetaCampaignPauseApprovalBridgeWireV2.exactString(
        fields["native_observed_at"]),
      NativeMetaCampaignPauseApprovalBridgeWireV2.isDigest(requestIDDigest),
      NativeMetaCampaignPauseApprovalBridgeWireV2.isDigest(requestDigest),
      NativeMetaCampaignPauseApprovalBridgeWireV2.isDigest(sourceContextDigest),
      NativeMetaCampaignPauseApprovalBridgeWireV2.isDigest(metadataDigest),
      NativeMetaCampaignPauseApprovalBridgeWireV2.isDigest(protocolDigest),
      NativeMetaCampaignPauseApprovalBridgeWireV2.isDigest(challengeDigest),
      NativeMetaCampaignPauseApprovalBridgeWireV2.isDigest(nonceDigest),
      NativeMetaCampaignPauseApprovalBridgeWireV2.isDigest(receiptDigest),
      NativeMetaCampaignPauseApprovalBridgeWireV2.isDigest(publicKeyFingerprint),
      let receiptKind = ApprovalKind(rawValue: receiptKindText),
      NativeMetaCampaignPauseApprovalBridgeWireV2.isMetaCampaignPausePlanKind(receiptKind),
      isExactApprovalReceiptIdentifier(receiptIdentifier)
    else { throw NativeApprovalBridgeProtocolError.malformedCandidate }

    let challenge = try NativeMetaCampaignPauseApprovalBridgeWireV2.decodeBase64URL(
      challengeText,
      minimumBytes: 1,
      maximumBytes: NativeMetaCampaignPauseApprovalBridgeWireV2.maximumChallengeBytes,
      error: .malformedCandidate
    )
    let receipt = try NativeMetaCampaignPauseApprovalBridgeWireV2.decodeBase64URL(
      receiptText,
      minimumBytes: 1,
      maximumBytes: NativeMetaCampaignPauseApprovalBridgeWireV2.maximumReceiptBytes,
      error: .malformedCandidate
    )
    let publicKey = try NativeMetaCampaignPauseApprovalBridgeWireV2.decodeBase64URL(
      publicKeyText,
      minimumBytes: NativeMetaCampaignPauseApprovalBridgeWireV2.publicKeyBytes,
      maximumBytes: NativeMetaCampaignPauseApprovalBridgeWireV2.publicKeyBytes,
      error: .malformedCandidate
    )
    guard publicKey.first == 0x04 else {
      throw NativeApprovalBridgeProtocolError.malformedCandidate
    }
    let observedAt = try NativeMetaCampaignPauseApprovalBridgeWireV2.sixFractionTimestamp(
      nativeObservedAtText,
      error: .observationTimeInvalid
    )

    let checkedRequest = try NativeMetaCampaignPauseApprovalBridgeRequestV2.decode(
      expectedRequest.canonicalBytes)
    guard checkedRequest == expectedRequest else {
      throw NativeApprovalBridgeProtocolError.bindingMismatch
    }
    let context = try NativeMetaCampaignPauseApprovalBridgeContextV2(
      metadata: metadata, error: .bindingMismatch)
    guard checkedRequest.metadataDigest == context.descriptor.metadataDigest,
      checkedRequest.protocolConfigurationDigest == context.descriptor.protocolConfigurationDigest,
      requestIDDigest == CanonicalJSON.sha256Hex(checkedRequest.requestID),
      requestDigest == checkedRequest.digest,
      sourceContextDigest == checkedRequest.sourceContextDigest,
      metadataDigest == checkedRequest.metadataDigest,
      protocolDigest == checkedRequest.protocolConfigurationDigest,
      challenge == checkedRequest.canonicalChallenge
    else { throw NativeApprovalBridgeProtocolError.bindingMismatch }

    let expectedChallenge = try checkedRequest.boundChallenge(
      metadata: metadata,
      exchangeStartedAt: exchangeStartedAt
    )
    try NativeMetaCampaignPauseApprovalBridgeWireV2.requireExchangeTime(
      exchangeStartedAt: exchangeStartedAt,
      currentNow: currentNow,
      observation: observedAt,
      expiry: expectedChallenge.expiresAt
    )
    guard challengeDigest == expectedChallenge.digest,
      nonceDigest == CanonicalJSON.sha256Hex(expectedChallenge.nonce),
      receiptKind == expectedChallenge.kind,
      expiresAtText == expectedChallenge.expiresAtText,
      publicKeyFingerprint == CanonicalJSON.sha256Hex(publicKey)
    else { throw NativeApprovalBridgeProtocolError.bindingMismatch }

    let signed = try NativeMetaCampaignPauseApprovalBridgeWireV2.verifiedReceipt(
      receipt,
      publicKeyX963: publicKey,
      challenge: expectedChallenge,
      configuration: context.configuration,
      observation: observedAt,
      error: .bindingMismatch
    )
    let issuedAt = try NativeMetaCampaignPauseApprovalBridgeWireV2.receiptIssuedAt(
      receipt,
      error: .bindingMismatch
    )
    guard issuedAt <= observedAt,
      signed.digest == receiptDigest,
      signed.receiptIdentifier == receiptIdentifier,
      signed.kind == receiptKind
    else { throw NativeApprovalBridgeProtocolError.bindingMismatch }

    let rebuilt = NativeMetaCampaignPauseApprovalBridgeCandidateV2(
      requestIDDigest: requestIDDigest,
      requestDigest: requestDigest,
      sourceContextDigest: sourceContextDigest,
      metadataDigest: metadataDigest,
      protocolConfigurationDigest: protocolDigest,
      canonicalChallenge: challenge,
      challengeDigest: challengeDigest,
      challengeNonceDigest: nonceDigest,
      receiptKind: receiptKind,
      expiresAtText: expiresAtText,
      canonicalReceipt: receipt,
      receiptDigest: receiptDigest,
      receiptIdentifier: receiptIdentifier,
      activePublicKeyX963: publicKey,
      activePublicKeyFingerprint: publicKeyFingerprint,
      nativeObservedAtText: nativeObservedAtText,
      canonicalBytes: snapshot
    )
    let reencoded: Data
    do {
      reencoded = try rebuilt.reencodedBytes()
    } catch {
      throw NativeApprovalBridgeProtocolError.malformedCandidate
    }
    guard reencoded == snapshot else {
      throw NativeApprovalBridgeProtocolError.malformedCandidate
    }
    return rebuilt
  }

  private init(
    requestIDDigest: String,
    requestDigest: String,
    sourceContextDigest: String,
    metadataDigest: String,
    protocolConfigurationDigest: String,
    canonicalChallenge: Data,
    challengeDigest: String,
    challengeNonceDigest: String,
    receiptKind: ApprovalKind,
    expiresAtText: String,
    canonicalReceipt: Data,
    receiptDigest: String,
    receiptIdentifier: String,
    activePublicKeyX963: Data,
    activePublicKeyFingerprint: String,
    nativeObservedAtText: String,
    canonicalBytes: Data
  ) {
    self.requestIDDigest = requestIDDigest
    self.requestDigest = requestDigest
    self.sourceContextDigest = sourceContextDigest
    self.metadataDigest = metadataDigest
    self.protocolConfigurationDigest = protocolConfigurationDigest
    self.canonicalChallenge = canonicalChallenge
    self.challengeDigest = challengeDigest
    self.challengeNonceDigest = challengeNonceDigest
    self.receiptKind = receiptKind
    self.expiresAtText = expiresAtText
    self.canonicalReceipt = canonicalReceipt
    self.receiptDigest = receiptDigest
    self.receiptIdentifier = receiptIdentifier
    self.activePublicKeyX963 = activePublicKeyX963
    self.activePublicKeyFingerprint = activePublicKeyFingerprint
    self.nativeObservedAtText = nativeObservedAtText
    self.canonicalBytes = canonicalBytes
    digest = CanonicalJSON.sha256Hex(canonicalBytes)
  }

  private func reencodedBytes() throws -> Data {
    try CanonicalJSON.encode(
      .object([
        "schema_version": .integer(Int64(Self.schemaVersion)),
        "message_type": .string(Self.messageType),
        "native_observation_status": .string(Self.observationStatus),
        "request_id_digest": .string(requestIDDigest),
        "request_digest": .string(requestDigest),
        "source_context_digest": .string(sourceContextDigest),
        "metadata_digest": .string(metadataDigest),
        "protocol_configuration_digest": .string(protocolConfigurationDigest),
        "canonical_challenge_b64url": .string(base64URLEncoded(canonicalChallenge)),
        "challenge_digest": .string(challengeDigest),
        "challenge_nonce_digest": .string(challengeNonceDigest),
        "receipt_kind": .string(receiptKind.rawValue),
        "expires_at": .string(expiresAtText),
        "canonical_receipt_b64url": .string(base64URLEncoded(canonicalReceipt)),
        "receipt_digest": .string(receiptDigest),
        "receipt_id": .string(receiptIdentifier),
        "active_public_key_x963_b64url": .string(base64URLEncoded(activePublicKeyX963)),
        "active_public_key_fingerprint": .string(activePublicKeyFingerprint),
        "native_observed_at": .string(nativeObservedAtText),
      ]))
  }
}

private struct NativeMetaCampaignPauseApprovalBridgeContextV2 {
  let configuration: TrustedHostConfiguration
  let descriptor: NativeApprovalHostArtifactDescriptorV1

  init(metadata: ApprovalHostBuildMetadataV1, error: NativeApprovalBridgeProtocolError) throws {
    do {
      descriptor = try NativeApprovalHostArtifactDescriptorV1(metadata: metadata)
      configuration = try metadata.makeTrustedHostConfiguration()
    } catch {
      throw error
    }
  }
}

private enum NativeMetaCampaignPauseApprovalBridgeWireV2 {
  static let requestIDBytes = 32
  static let publicKeyBytes = 65
  static let maximumChallengeBytes = 65_536
  static let maximumReceiptBytes = 16_384

  static func closedObject(
    _ data: Data,
    maximumBytes: Int,
    maximumItems: Int,
    expected: Set<String>,
    error: NativeApprovalBridgeProtocolError
  ) throws -> [String: CanonicalJSONValue] {
    let root: CanonicalJSONValue
    do {
      root = try CanonicalJSON.parseCanonical(
        data,
        limits: CanonicalJSONLimits(
          maximumBytes: maximumBytes,
          maximumDepth: 1,
          maximumContainerItems: maximumItems,
          maximumStringBytes: 90_000
        )
      )
    } catch {
      throw error
    }
    guard case .object(let fields) = root, Set(fields.keys) == expected else { throw error }
    return fields
  }

  static func exactString(_ value: CanonicalJSONValue?) -> String? {
    guard case .string(let result) = value else { return nil }
    return result
  }

  static func isDigest(_ value: String) -> Bool {
    value.utf8.count == 64
      && value.utf8.allSatisfy {
        (48...57).contains($0) || (97...102).contains($0)
      }
  }

  static func isMetaCampaignPausePlanKind(_ kind: ApprovalKind) -> Bool {
    kind == .metaCampaignPausePlan
  }

  /// Copy a potentially bridged Foundation buffer only after its declared size
  /// is bounded. The resulting `Data` owns its bytes for every later parse,
  /// digest and stored field in this protocol. This is a defensive snapshot,
  /// not a claim to solve arbitrary concurrent-memory mutation.
  static func boundedSnapshot(
    _ input: Data,
    maximumBytes: Int,
    error: NativeApprovalBridgeProtocolError
  ) throws -> Data {
    guard maximumBytes >= 0, input.count <= maximumBytes else { throw error }
    let snapshot = input.withUnsafeBytes { bytes -> Data in
      guard let baseAddress = bytes.baseAddress, bytes.count > 0 else { return Data() }
      return Data(bytes: baseAddress, count: bytes.count)
    }
    guard snapshot.count <= maximumBytes else { throw error }
    return snapshot
  }

  /// Rebuild the entire closed M42 subject before it becomes a typed V2
  /// request. This intentionally omits only the current exchange clock
  /// eligibility, which belongs to `boundChallenge(metadata:exchangeStartedAt:)`.
  /// It cannot select host metadata, resolve a key, or consume a nonce.
  static func requireStructuralCanonicalMetaCampaignPauseChallenge(_ data: Data) throws {
    guard !data.isEmpty, data.count <= maximumChallengeBytes else {
      throw NativeApprovalBridgeProtocolError.malformedRequest
    }
    do {
      let root = try CanonicalJSON.parseCanonical(
        data,
        limits: CanonicalJSONLimits(
          maximumBytes: maximumChallengeBytes,
          maximumDepth: 20,
          maximumContainerItems: 256,
          maximumStringBytes: 4_096
        )
      )
      let object = try ClosedObject(
        root,
        exactly: [
          "schema_version", "kind", "nonce", "issued_at", "expires_at", "action", "grantor_ref",
        ]
      )
      guard try object.integer("schema_version") == 1,
        let kind = ApprovalKind(rawValue: try object.string("kind")),
        isMetaCampaignPausePlanKind(kind)
      else { throw NativeApprovalBridgeProtocolError.malformedRequest }

      _ = try decodeBase64URL(
        try object.string("nonce"),
        minimumBytes: 32,
        maximumBytes: 32,
        error: .malformedRequest
      )
      let issuedAt = try ClosedTimestamp.parse(try object.string("issued_at"))
      let expiresAt = try ClosedTimestamp.parse(try object.string("expires_at"))
      let limits = ApprovalHostLimits()
      guard expiresAt > issuedAt,
        expiresAt.timeIntervalSince(issuedAt) <= limits.maximumChallengeLifetime
      else { throw NativeApprovalBridgeProtocolError.malformedRequest }
      _ = try object.boundedString("grantor_ref", maximumBytes: 256)
      let action = try ExactMetaCampaignPauseAction.decode(
        try object.value("action"),
        maximumSubplans: limits.maximumSubplans
      )
      guard expiresAt <= action.plan.expiresAt else {
        throw NativeApprovalBridgeProtocolError.malformedRequest
      }
    } catch {
      throw NativeApprovalBridgeProtocolError.malformedRequest
    }
  }

  static func decodeBase64URL(
    _ raw: String,
    minimumBytes: Int,
    maximumBytes: Int,
    error: NativeApprovalBridgeProtocolError
  ) throws -> Data {
    guard !raw.isEmpty,
      raw.utf8.allSatisfy({
        (48...57).contains($0) || (65...90).contains($0) || (97...122).contains($0)
          || $0 == 45 || $0 == 95
      })
    else { throw error }
    let base64 = raw.replacingOccurrences(of: "-", with: "+")
      .replacingOccurrences(of: "_", with: "/")
    let padding = String(repeating: "=", count: (4 - base64.utf8.count % 4) % 4)
    guard let decoded = Data(base64Encoded: base64 + padding),
      (minimumBytes...maximumBytes).contains(decoded.count),
      base64URLEncoded(decoded) == raw
    else { throw error }
    return decoded
  }

  static func sixFractionTimestamp(
    _ raw: String,
    error: NativeApprovalBridgeProtocolError
  ) throws -> Date {
    guard raw.utf8.count == 27 else { throw error }
    do {
      return try ClosedTimestamp.parse(raw)
    } catch {
      throw error
    }
  }

  static func requireExchangeTime(
    exchangeStartedAt: Date,
    currentNow: Date,
    observation: Date,
    expiry: Date
  ) throws {
    guard isFinite(exchangeStartedAt), isFinite(currentNow), isFinite(observation),
      currentNow >= exchangeStartedAt
    else { throw NativeApprovalBridgeProtocolError.exchangeTimeInvalid }
    guard observation >= exchangeStartedAt,
      observation <= currentNow,
      currentNow < expiry,
      observation < expiry
    else { throw NativeApprovalBridgeProtocolError.observationTimeInvalid }
  }

  static func isFinite(_ date: Date) -> Bool {
    date.timeIntervalSince1970.isFinite
  }

  static func verifiedReceipt(
    _ receipt: Data,
    publicKeyX963: Data,
    challenge: ApprovalChallenge,
    configuration: TrustedHostConfiguration,
    observation: Date,
    error: NativeApprovalBridgeProtocolError
  ) throws -> SignedApprovalReceipt {
    do {
      let verifier = try PinnedReceiptVerifier(
        configuration: configuration,
        trustedPublicKeyX963: publicKeyX963
      )
      return try verifier.verify(receipt, expectedChallenge: challenge, now: observation)
    } catch {
      throw error
    }
  }

  static func receiptIssuedAt(
    _ canonicalReceipt: Data,
    error: NativeApprovalBridgeProtocolError
  ) throws -> Date {
    let root: CanonicalJSONValue
    do {
      root = try CanonicalJSON.parseCanonical(
        canonicalReceipt,
        limits: CanonicalJSONLimits(
          maximumBytes: maximumReceiptBytes,
          maximumDepth: 4,
          maximumContainerItems: 32,
          maximumStringBytes: 1_024
        )
      )
    } catch {
      throw error
    }
    guard case .object(let fields) = root,
      let raw = exactString(fields["issued_at"]), raw.utf8.count == 24
    else { throw error }
    let formatter = ISO8601DateFormatter()
    formatter.timeZone = TimeZone(secondsFromGMT: 0)
    formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    guard let issuedAt = formatter.date(from: raw), ReceiptTimestamp.encode(issuedAt) == raw else {
      throw error
    }
    return issuedAt
  }
}
