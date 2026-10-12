import Foundation

/// Deterministic, build-only evidence for one generated approval-host
/// configuration. This descriptor is not an installation, signing, runtime,
/// key, service, client, or authorization attestation. It contains no secret
/// material and does not perform an OS operation.
package struct NativeApprovalHostArtifactDescriptorV1: Equatable, Sendable {
  package static let schemaVersion = 1
  package static let scope = "UNSIGNED_BUILD_METADATA_ONLY_NOT_OS_ATTESTATION"
  package static let artifactStatus = "UNSIGNED_UNVERIFIED"

  package let metadataCanonicalBytes: Data
  package let metadataDigest: String
  package let protocolConfiguration: ApprovalHostProtocolConfigurationV1
  package let protocolConfigurationDigest: String
  package let expectedClientCodeSigningRequirement: String
  package let expectedHostCodeSigningRequirement: String
  package let canonicalBytes: Data
  package let digest: String

  /// Rebuild every field through the closed M29 metadata constructor before
  /// emitting descriptor bytes. This prevents a constructed package object
  /// from bypassing selector, role, limit, or provenance validation.
  package init(metadata: ApprovalHostBuildMetadataV1) throws {
    let checkedClient = try ApprovalHostCodeIdentitySelectorV1(
      role: metadata.clientCodeIdentity.role,
      bundleIdentifier: metadata.clientCodeIdentity.bundleIdentifier,
      teamIdentifier: metadata.clientCodeIdentity.teamIdentifier
    )
    let checkedHost = try ApprovalHostCodeIdentitySelectorV1(
      role: metadata.hostCodeIdentity.role,
      bundleIdentifier: metadata.hostCodeIdentity.bundleIdentifier,
      teamIdentifier: metadata.hostCodeIdentity.teamIdentifier
    )
    let checkedMetadata = try ApprovalHostBuildMetadataV1(
      hostReference: metadata.hostReference,
      signingKeyReference: metadata.signingKeyReference,
      machServiceName: metadata.machServiceName,
      clientCodeIdentity: checkedClient,
      hostCodeIdentity: checkedHost,
      signingKeyApplicationTag: metadata.signingKeyApplicationTag,
      signingKeyAccessGroup: metadata.signingKeyAccessGroup,
      keyStorage: metadata.keyStorage,
      provenanceService: metadata.provenanceService,
      provenanceAccount: metadata.provenanceAccount,
      limits: metadata.limits
    )
    let configuration = try checkedMetadata.makeTrustedHostConfiguration()
    let projection = configuration.approvalProtocolConfigurationV1
    let metadataValue = Self.metadataValue(metadata: checkedMetadata, projection: projection)
    let encodedMetadata: Data
    let encodedDescriptor: Data
    do {
      encodedMetadata = try CanonicalJSON.encode(metadataValue)
      encodedDescriptor = try CanonicalJSON.encode(
        .object([
          "schema_version": .integer(Int64(Self.schemaVersion)),
          "scope": .string(Self.scope),
          "artifact_status": .string(Self.artifactStatus),
          "metadata": metadataValue,
          "metadata_digest": .string(CanonicalJSON.sha256Hex(encodedMetadata)),
          "protocol_configuration": Self.protocolValue(projection),
          "protocol_configuration_digest": .string(projection.configurationDigest),
          "expected_client_code_signing_requirement": .string(
            checkedMetadata.expectedClientCodeSigningRequirement
          ),
          "expected_host_code_signing_requirement": .string(
            checkedMetadata.expectedHostCodeSigningRequirement
          ),
        ]))
    } catch {
      throw ApprovalHostError.hostNotInstalled
    }

    metadataCanonicalBytes = encodedMetadata
    metadataDigest = CanonicalJSON.sha256Hex(encodedMetadata)
    protocolConfiguration = projection
    protocolConfigurationDigest = projection.configurationDigest
    expectedClientCodeSigningRequirement = checkedMetadata.expectedClientCodeSigningRequirement
    expectedHostCodeSigningRequirement = checkedMetadata.expectedHostCodeSigningRequirement
    canonicalBytes = encodedDescriptor
    digest = CanonicalJSON.sha256Hex(encodedDescriptor)
  }

  private static func metadataValue(
    metadata: ApprovalHostBuildMetadataV1,
    projection: ApprovalHostProtocolConfigurationV1
  ) -> CanonicalJSONValue {
    .object([
      "schema_version": .integer(Int64(schemaVersion)),
      "host_ref": .string(metadata.hostReference),
      "signing_key_ref": .string(metadata.signingKeyReference),
      "mach_service_name": .string(metadata.machServiceName),
      "client_bundle_identifier": .string(metadata.clientCodeIdentity.bundleIdentifier),
      "client_team_identifier": .string(metadata.clientCodeIdentity.teamIdentifier),
      "host_bundle_identifier": .string(metadata.hostCodeIdentity.bundleIdentifier),
      "host_team_identifier": .string(metadata.hostCodeIdentity.teamIdentifier),
      "signing_key_application_tag_hex": .string(lowercaseHex(metadata.signingKeyApplicationTag)),
      "signing_key_access_group": metadata.signingKeyAccessGroup.map(CanonicalJSONValue.string)
        ?? .null,
      "key_storage": .string(metadata.keyStorage.rawValue),
      "provenance_service": .string(metadata.provenanceService),
      "provenance_account": .string(metadata.provenanceAccount),
      "maximum_challenge_bytes": .integer(Int64(projection.maximumChallengeBytes)),
      "maximum_challenge_lifetime_seconds": .integer(
        Int64(projection.maximumChallengeLifetimeSeconds)
      ),
      "maximum_issued_at_skew_seconds": .integer(Int64(projection.maximumIssuedAtSkewSeconds)),
      "maximum_subplans": .integer(Int64(projection.maximumSubplans)),
      "maximum_policy_items": .integer(Int64(projection.maximumPolicyItems)),
    ])
  }

  private static func protocolValue(
    _ projection: ApprovalHostProtocolConfigurationV1
  ) -> CanonicalJSONValue {
    .object([
      "host_ref": .string(projection.hostReference),
      "signing_key_ref": .string(projection.signingKeyReference),
      "maximum_challenge_bytes": .integer(Int64(projection.maximumChallengeBytes)),
      "maximum_challenge_lifetime_seconds": .integer(
        Int64(projection.maximumChallengeLifetimeSeconds)
      ),
      "maximum_issued_at_skew_seconds": .integer(Int64(projection.maximumIssuedAtSkewSeconds)),
      "maximum_subplans": .integer(Int64(projection.maximumSubplans)),
      "maximum_policy_items": .integer(Int64(projection.maximumPolicyItems)),
    ])
  }

  private static func lowercaseHex(_ data: Data) -> String {
    data.map { String(format: "%02x", $0) }.joined()
  }
}
