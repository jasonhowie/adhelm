import Foundation
import XCTest

@testable import ApprovalHostCore

final class NativeApprovalHostArtifactDescriptorTests: XCTestCase {
  func testDescriptorRebuildsClosedMetadataAndExactProtocolProjection() throws {
    let descriptor = try NativeApprovalHostArtifactDescriptorV1(metadata: fixtureMetadata())
    let expectedProtocol = try CanonicalJSON.encode(
      .object([
        "host_ref": .string("host:review-\"é😀"),
        "signing_key_ref": .string("key:receipt-\\東京"),
        "maximum_challenge_bytes": .integer(65_536),
        "maximum_challenge_lifetime_seconds": .integer(300),
        "maximum_issued_at_skew_seconds": .integer(60),
        "maximum_subplans": .integer(6),
        "maximum_policy_items": .integer(64),
      ]))

    XCTAssertEqual(descriptor.protocolConfiguration.canonicalBytes, expectedProtocol)
    XCTAssertEqual(
      descriptor.protocolConfigurationDigest,
      CanonicalJSON.sha256Hex(expectedProtocol)
    )
    XCTAssertEqual(
      descriptor.expectedClientCodeSigningRequirement,
      "anchor apple generic and identifier \"com.aptsonic.adhelm.client\""
        + " and certificate leaf[subject.OU] = \"ABCDE12345\""
    )
    XCTAssertEqual(
      descriptor.expectedHostCodeSigningRequirement,
      "anchor apple generic and identifier \"com.aptsonic.adhelm.host\""
        + " and certificate leaf[subject.OU] = \"ABCDE12345\""
    )

    let expectedMetadata = try CanonicalJSON.encode(
      .object([
        "schema_version": .integer(1),
        "host_ref": .string("host:review-\"é😀"),
        "signing_key_ref": .string("key:receipt-\\東京"),
        "mach_service_name": .string("com.aptsonic.adhelm.approvalhost"),
        "client_bundle_identifier": .string("com.aptsonic.adhelm.client"),
        "client_team_identifier": .string("ABCDE12345"),
        "host_bundle_identifier": .string("com.aptsonic.adhelm.host"),
        "host_team_identifier": .string("ABCDE12345"),
        "signing_key_application_tag_hex": .string("00616263ff"),
        "signing_key_access_group": .string("ABCDE12345.com.aptsonic.adhelm.host"),
        "key_storage": .string("KEYCHAIN_P256"),
        "provenance_service": .string("com.aptsonic.adhelm.provenance"),
        "provenance_account": .string("approval-host-v1"),
        "maximum_challenge_bytes": .integer(65_536),
        "maximum_challenge_lifetime_seconds": .integer(300),
        "maximum_issued_at_skew_seconds": .integer(60),
        "maximum_subplans": .integer(6),
        "maximum_policy_items": .integer(64),
      ]))
    XCTAssertEqual(descriptor.metadataCanonicalBytes, expectedMetadata)
    XCTAssertEqual(descriptor.metadataDigest, CanonicalJSON.sha256Hex(expectedMetadata))
    XCTAssertEqual(descriptor.digest, CanonicalJSON.sha256Hex(descriptor.canonicalBytes))
    // These independent Python v1 vector values prove that Core emits the
    // same RFC 8785 bytes and SHA-256 values as the build-only selector model.
    XCTAssertEqual(
      descriptor.metadataDigest,
      "2a4bad16de7f64a389d1ed4accbf646b2b06d8a36f9ea26e3f3a652009b04e2e"
    )
    XCTAssertEqual(
      descriptor.protocolConfigurationDigest,
      "a738e1462d12905b9b50ea2ce819f811593f9e3e676df0ef871c1a5dc970555c"
    )
    XCTAssertEqual(
      descriptor.digest,
      "a1a3b93d7350fe56fc4df786a21128f38cea617b9172fbc4dac2cc71433a3923"
    )

    let parsed = try CanonicalJSON.parseCanonical(descriptor.canonicalBytes)
    guard case .object(let fields) = parsed else {
      return XCTFail("Expected closed descriptor object")
    }
    XCTAssertEqual(fields["schema_version"], .integer(1))
    XCTAssertEqual(fields["scope"], .string("UNSIGNED_BUILD_METADATA_ONLY_NOT_OS_ATTESTATION"))
    XCTAssertEqual(fields["artifact_status"], .string("UNSIGNED_UNVERIFIED"))
    XCTAssertEqual(fields["metadata_digest"], .string(descriptor.metadataDigest))
    XCTAssertEqual(
      fields["protocol_configuration_digest"],
      .string(descriptor.protocolConfigurationDigest)
    )
  }

  func testDescriptorBindsNonProtocolMetadataSeparatelyFromProtocolConfiguration() throws {
    let original = try NativeApprovalHostArtifactDescriptorV1(metadata: fixtureMetadata())
    let replacement = try ApprovalHostBuildMetadataV1(
      hostReference: "host:review-\"é😀",
      signingKeyReference: "key:receipt-\\東京",
      machServiceName: "com.aptsonic.adhelm.approvalhost",
      clientCodeIdentity: try selector(.client, "com.aptsonic.adhelm.client"),
      hostCodeIdentity: try selector(.host, "com.aptsonic.adhelm.host"),
      signingKeyApplicationTag: Data([0x00, 0x61, 0x62, 0x63, 0xFE]),
      signingKeyAccessGroup: nil,
      keyStorage: .secureEnclaveP256,
      provenanceService: "com.aptsonic.adhelm.provenance",
      provenanceAccount: "approval-host-v2",
      limits: ApprovalHostLimits(
        maximumChallengeBytes: 65_536,
        maximumChallengeLifetime: 300,
        maximumIssuedAtSkew: 60,
        maximumSubplans: 6,
        maximumPolicyItems: 64
      )
    )
    let changed = try NativeApprovalHostArtifactDescriptorV1(metadata: replacement)

    XCTAssertNotEqual(changed.metadataCanonicalBytes, original.metadataCanonicalBytes)
    XCTAssertNotEqual(changed.metadataDigest, original.metadataDigest)
    XCTAssertNotEqual(changed.canonicalBytes, original.canonicalBytes)
    XCTAssertNotEqual(changed.digest, original.digest)
    XCTAssertEqual(
      changed.protocolConfiguration.canonicalBytes,
      original.protocolConfiguration.canonicalBytes
    )
    XCTAssertEqual(changed.protocolConfigurationDigest, original.protocolConfigurationDigest)
    XCTAssertTrue(
      String(data: changed.metadataCanonicalBytes, encoding: .utf8)?.contains(
        "\"signing_key_access_group\":null"
      ) ?? false)
  }

  func testInvalidRoleBoundMetadataIsRejectedBeforeDescriptorEmission() throws {
    let invalidClient = try ApprovalHostCodeIdentitySelectorV1(
      role: .host,
      bundleIdentifier: "com.aptsonic.adhelm.client",
      teamIdentifier: "ABCDE12345"
    )
    XCTAssertThrowsError(
      try ApprovalHostBuildMetadataV1(
        hostReference: "host:review",
        signingKeyReference: "key:review",
        machServiceName: "com.aptsonic.adhelm.approvalhost",
        clientCodeIdentity: invalidClient,
        hostCodeIdentity: try selector(.host, "com.aptsonic.adhelm.host"),
        signingKeyApplicationTag: Data([0x01]),
        keyStorage: .keychainP256,
        provenanceService: "com.aptsonic.adhelm.provenance",
        provenanceAccount: "approval-host-v1"
      ))
  }

  private func fixtureMetadata() throws -> ApprovalHostBuildMetadataV1 {
    try ApprovalHostBuildMetadataV1(
      hostReference: "host:review-\"é😀",
      signingKeyReference: "key:receipt-\\東京",
      machServiceName: "com.aptsonic.adhelm.approvalhost",
      clientCodeIdentity: try selector(.client, "com.aptsonic.adhelm.client"),
      hostCodeIdentity: try selector(.host, "com.aptsonic.adhelm.host"),
      signingKeyApplicationTag: Data([0x00, 0x61, 0x62, 0x63, 0xFF]),
      signingKeyAccessGroup: "ABCDE12345.com.aptsonic.adhelm.host",
      keyStorage: .keychainP256,
      provenanceService: "com.aptsonic.adhelm.provenance",
      provenanceAccount: "approval-host-v1",
      limits: ApprovalHostLimits(
        maximumChallengeBytes: 65_536,
        maximumChallengeLifetime: 300,
        maximumIssuedAtSkew: 60,
        maximumSubplans: 6,
        maximumPolicyItems: 64
      )
    )
  }

  private func selector(
    _ role: ApprovalHostCodeIdentityRoleV1,
    _ bundleIdentifier: String
  ) throws -> ApprovalHostCodeIdentitySelectorV1 {
    try ApprovalHostCodeIdentitySelectorV1(
      role: role,
      bundleIdentifier: bundleIdentifier,
      teamIdentifier: "ABCDE12345"
    )
  }
}
