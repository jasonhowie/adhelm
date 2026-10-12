import Foundation

/// Closed, package-internal failures for the future native approval bridge.
/// They never cross a pipe, XPC boundary, CLI, or public API.
package enum NativeApprovalBridgeProtocolError: Error, Equatable, Sendable {
  case malformedRequest
  case malformedCandidate
  case bindingMismatch
  case exchangeTimeInvalid
  case observationTimeInvalid
}

/// One canonical private request for a future one-shot native approval bridge.
///
/// This is an offline wire contract only. It does not open a pipe, locate a
/// host, resolve a key, perform a review, consume a nonce, or create authority.
/// A later configured boundary must derive its metadata and current-key facts
/// from reviewed static configuration and protected state, not from this value.
package struct NativeApprovalBridgeRequestV1: Equatable, Sendable, CustomStringConvertible,
  CustomDebugStringConvertible
{
  package static let schemaVersion = 1
  package static let messageType = "NATIVE_APPROVAL_BRIDGE_REQUEST_V1"
  package static let maximumWireBytes = 98_304

  package let requestID: Data
  package let sourceContextDigest: String
  package let metadataDigest: String
  package let protocolConfigurationDigest: String
  package let canonicalChallenge: Data
  package let canonicalBytes: Data
  package let digest: String

  package var description: String { "NativeApprovalBridgeRequestV1(redacted)" }
  package var debugDescription: String { description }

  package init(
    requestID: Data,
    sourceContextDigest: String,
    metadataDigest: String,
    protocolConfigurationDigest: String,
    canonicalChallenge: Data
  ) throws {
    let checkedRequestID = try NativeApprovalBridgeWire.boundedSnapshot(
      requestID,
      maximumBytes: NativeApprovalBridgeWire.requestIDBytes,
      error: .malformedRequest
    )
    let checkedChallenge = try NativeApprovalBridgeWire.boundedSnapshot(
      canonicalChallenge,
      maximumBytes: NativeApprovalBridgeWire.maximumChallengeBytes,
      error: .malformedRequest
    )
    guard checkedRequestID.count == NativeApprovalBridgeWire.requestIDBytes,
      NativeApprovalBridgeWire.isDigest(sourceContextDigest),
      NativeApprovalBridgeWire.isDigest(metadataDigest),
      NativeApprovalBridgeWire.isDigest(protocolConfigurationDigest)
    else { throw NativeApprovalBridgeProtocolError.malformedRequest }
    try NativeApprovalBridgeWire.requireCanonicalDirectChallenge(checkedChallenge)

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
  package static func decode(_ data: Data) throws -> NativeApprovalBridgeRequestV1 {
    let snapshot = try NativeApprovalBridgeWire.boundedSnapshot(
      data,
      maximumBytes: maximumWireBytes,
      error: .malformedRequest
    )
    let fields = try NativeApprovalBridgeWire.closedObject(
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
      NativeApprovalBridgeWire.exactString(fields["message_type"]) == messageType,
      let requestIDText = NativeApprovalBridgeWire.exactString(fields["request_id"]),
      let sourceContextDigest = NativeApprovalBridgeWire.exactString(
        fields["source_context_digest"]),
      let metadataDigest = NativeApprovalBridgeWire.exactString(fields["metadata_digest"]),
      let protocolDigest = NativeApprovalBridgeWire.exactString(
        fields["protocol_configuration_digest"]),
      let challengeText = NativeApprovalBridgeWire.exactString(fields["canonical_challenge_b64url"])
    else { throw NativeApprovalBridgeProtocolError.malformedRequest }
    let requestID = try NativeApprovalBridgeWire.decodeBase64URL(
      requestIDText,
      minimumBytes: NativeApprovalBridgeWire.requestIDBytes,
      maximumBytes: NativeApprovalBridgeWire.requestIDBytes,
      error: .malformedRequest
    )
    let challenge = try NativeApprovalBridgeWire.decodeBase64URL(
      challengeText,
      minimumBytes: 1,
      maximumBytes: NativeApprovalBridgeWire.maximumChallengeBytes,
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
    guard NativeApprovalBridgeWire.isFinite(exchangeStartedAt) else {
      throw NativeApprovalBridgeProtocolError.exchangeTimeInvalid
    }
    let context = try NativeApprovalBridgeContext(metadata: metadata, error: .bindingMismatch)
    guard metadataDigest == context.descriptor.metadataDigest,
      protocolConfigurationDigest == context.descriptor.protocolConfigurationDigest
    else { throw NativeApprovalBridgeProtocolError.bindingMismatch }
    do {
      let challenge = try ApprovalChallenge.decode(
        canonicalChallenge,
        now: exchangeStartedAt,
        limits: context.configuration.limits
      )
      guard NativeApprovalBridgeWire.isDirectPlanKind(challenge.kind) else {
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

/// A bounded private candidate asserted by a future configured native bridge.
///
/// Its status is evidence syntax only in this offline contract. It is not a
/// grant, trust resolver, execution permit, provider credential, or proof that
/// a protected store, host, pipe, review, authentication, or nonce consumption
/// actually ran. The future bridge must construct it only after those steps;
/// this module intentionally provides no such integration.
package struct NativeApprovalBridgeCandidateV1: Equatable, Sendable, CustomStringConvertible,
  CustomDebugStringConvertible
{
  package static let schemaVersion = 1
  package static let messageType = "NATIVE_APPROVAL_BRIDGE_CANDIDATE_V1"
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

  package var description: String { "NativeApprovalBridgeCandidateV1(redacted)" }
  package var debugDescription: String { description }

  /// Parse a candidate only against the one locally retained request, reviewed
  /// metadata, and trusted exchange time window that produced it. This verifies
  /// receipt cryptography with the candidate's exact X9.63 key, but it does not
  /// resolve or trust that key independently and does not accept a Python grant.
  package static func decode(
    _ data: Data,
    expectedRequest: NativeApprovalBridgeRequestV1,
    metadata: ApprovalHostBuildMetadataV1,
    exchangeStartedAt: Date,
    currentNow: Date
  ) throws -> NativeApprovalBridgeCandidateV1 {
    let snapshot = try NativeApprovalBridgeWire.boundedSnapshot(
      data,
      maximumBytes: maximumWireBytes,
      error: .malformedCandidate
    )
    let fields = try NativeApprovalBridgeWire.closedObject(
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
      NativeApprovalBridgeWire.exactString(fields["message_type"]) == messageType,
      NativeApprovalBridgeWire.exactString(fields["native_observation_status"])
        == observationStatus,
      let requestIDDigest = NativeApprovalBridgeWire.exactString(fields["request_id_digest"]),
      let requestDigest = NativeApprovalBridgeWire.exactString(fields["request_digest"]),
      let sourceContextDigest = NativeApprovalBridgeWire.exactString(
        fields["source_context_digest"]),
      let metadataDigest = NativeApprovalBridgeWire.exactString(fields["metadata_digest"]),
      let protocolDigest = NativeApprovalBridgeWire.exactString(
        fields["protocol_configuration_digest"]),
      let challengeText = NativeApprovalBridgeWire.exactString(
        fields["canonical_challenge_b64url"]),
      let challengeDigest = NativeApprovalBridgeWire.exactString(fields["challenge_digest"]),
      let nonceDigest = NativeApprovalBridgeWire.exactString(fields["challenge_nonce_digest"]),
      let receiptKindText = NativeApprovalBridgeWire.exactString(fields["receipt_kind"]),
      let expiresAtText = NativeApprovalBridgeWire.exactString(fields["expires_at"]),
      let receiptText = NativeApprovalBridgeWire.exactString(fields["canonical_receipt_b64url"]),
      let receiptDigest = NativeApprovalBridgeWire.exactString(fields["receipt_digest"]),
      let receiptIdentifier = NativeApprovalBridgeWire.exactString(fields["receipt_id"]),
      let publicKeyText = NativeApprovalBridgeWire.exactString(
        fields["active_public_key_x963_b64url"]),
      let publicKeyFingerprint = NativeApprovalBridgeWire.exactString(
        fields["active_public_key_fingerprint"]),
      let nativeObservedAtText = NativeApprovalBridgeWire.exactString(fields["native_observed_at"]),
      NativeApprovalBridgeWire.isDigest(requestIDDigest),
      NativeApprovalBridgeWire.isDigest(requestDigest),
      NativeApprovalBridgeWire.isDigest(sourceContextDigest),
      NativeApprovalBridgeWire.isDigest(metadataDigest),
      NativeApprovalBridgeWire.isDigest(protocolDigest),
      NativeApprovalBridgeWire.isDigest(challengeDigest),
      NativeApprovalBridgeWire.isDigest(nonceDigest),
      NativeApprovalBridgeWire.isDigest(receiptDigest),
      NativeApprovalBridgeWire.isDigest(publicKeyFingerprint),
      let receiptKind = ApprovalKind(rawValue: receiptKindText),
      NativeApprovalBridgeWire.isDirectPlanKind(receiptKind),
      isExactApprovalReceiptIdentifier(receiptIdentifier)
    else { throw NativeApprovalBridgeProtocolError.malformedCandidate }

    let challenge = try NativeApprovalBridgeWire.decodeBase64URL(
      challengeText,
      minimumBytes: 1,
      maximumBytes: NativeApprovalBridgeWire.maximumChallengeBytes,
      error: .malformedCandidate
    )
    let receipt = try NativeApprovalBridgeWire.decodeBase64URL(
      receiptText,
      minimumBytes: 1,
      maximumBytes: NativeApprovalBridgeWire.maximumReceiptBytes,
      error: .malformedCandidate
    )
    let publicKey = try NativeApprovalBridgeWire.decodeBase64URL(
      publicKeyText,
      minimumBytes: NativeApprovalBridgeWire.publicKeyBytes,
      maximumBytes: NativeApprovalBridgeWire.publicKeyBytes,
      error: .malformedCandidate
    )
    guard publicKey.first == 0x04 else {
      throw NativeApprovalBridgeProtocolError.malformedCandidate
    }
    let observedAt = try NativeApprovalBridgeWire.sixFractionTimestamp(
      nativeObservedAtText,
      error: .observationTimeInvalid
    )

    let checkedRequest = try NativeApprovalBridgeRequestV1.decode(expectedRequest.canonicalBytes)
    guard checkedRequest == expectedRequest else {
      throw NativeApprovalBridgeProtocolError.bindingMismatch
    }
    let context = try NativeApprovalBridgeContext(metadata: metadata, error: .bindingMismatch)
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
    try NativeApprovalBridgeWire.requireExchangeTime(
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

    let signed = try NativeApprovalBridgeWire.verifiedReceipt(
      receipt,
      publicKeyX963: publicKey,
      challenge: expectedChallenge,
      configuration: context.configuration,
      observation: observedAt,
      error: .bindingMismatch
    )
    let issuedAt = try NativeApprovalBridgeWire.receiptIssuedAt(
      receipt,
      error: .bindingMismatch
    )
    guard issuedAt <= observedAt,
      signed.digest == receiptDigest,
      signed.receiptIdentifier == receiptIdentifier,
      signed.kind == receiptKind
    else { throw NativeApprovalBridgeProtocolError.bindingMismatch }

    let rebuilt = NativeApprovalBridgeCandidateV1(
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

private struct NativeApprovalBridgeContext {
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

private enum NativeApprovalBridgeWire {
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

  static func isDirectPlanKind(_ kind: ApprovalKind) -> Bool {
    kind == .openAIPausePlan || kind == .ga4KeyEventCreatePlan
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

  /// Reject a non-object or non-direct subject before it becomes a typed bridge
  /// request. Full challenge schema, freshness, metadata and plan validation
  /// still occur later under the trusted exchange-start time.
  static func requireCanonicalDirectChallenge(_ data: Data) throws {
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
      guard case .object(let fields) = root,
        fields["schema_version"] == .integer(1),
        let kindText = exactString(fields["kind"]),
        let kind = ApprovalKind(rawValue: kindText),
        isDirectPlanKind(kind)
      else { throw NativeApprovalBridgeProtocolError.malformedRequest }
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
