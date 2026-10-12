import Foundation
import XCTest

@testable import ApprovalHostCore

final class ApprovalHostBuildMetadataTests: XCTestCase {
  func testPythonProtocolConfigurationVectorsMatchCanonicalBytesAndDigest() throws {
    let fixture = try configurationFixture()
    XCTAssertEqual(fixture.schemaVersion, 1)
    XCTAssertEqual(fixture.scope, "PROTOCOL_CONFIGURATION_ONLY_NOT_INSTALLATION_ATTESTATION")

    for vector in fixture.vectors {
      let configuration = try trustedConfiguration(
        hostReference: vector.configuration.hostReference,
        signingKeyReference: vector.configuration.signingKeyReference,
        limits: ApprovalHostLimits(
          maximumChallengeBytes: vector.configuration.maximumChallengeBytes,
          maximumChallengeLifetime: TimeInterval(
            vector.configuration.maximumChallengeLifetimeSeconds
          ),
          maximumIssuedAtSkew: TimeInterval(vector.configuration.maximumIssuedAtSkewSeconds),
          maximumSubplans: vector.configuration.maximumSubplans,
          maximumPolicyItems: vector.configuration.maximumPolicyItems
        )
      )
      let projection = configuration.approvalProtocolConfigurationV1

      XCTAssertEqual(String(data: projection.canonicalBytes, encoding: .utf8), vector.canonicalJSON)
      XCTAssertEqual(projection.configurationDigest, vector.configurationDigest)
      XCTAssertEqual(projection.hostReference, vector.configuration.hostReference)
      XCTAssertEqual(projection.signingKeyReference, vector.configuration.signingKeyReference)
    }
  }

  func testTrustedConfigurationRejectsEveryNonPythonV1LimitShape() {
    let invalidLimits: [ApprovalHostLimits] = [
      ApprovalHostLimits(maximumChallengeBytes: 0),
      ApprovalHostLimits(maximumChallengeBytes: 65_537),
      ApprovalHostLimits(maximumChallengeLifetime: 0),
      ApprovalHostLimits(maximumChallengeLifetime: 300.5),
      ApprovalHostLimits(maximumChallengeLifetime: 301),
      ApprovalHostLimits(maximumChallengeLifetime: .infinity),
      ApprovalHostLimits(maximumChallengeLifetime: .nan),
      ApprovalHostLimits(maximumIssuedAtSkew: -1),
      ApprovalHostLimits(maximumIssuedAtSkew: 0.5),
      ApprovalHostLimits(maximumIssuedAtSkew: 301),
      ApprovalHostLimits(maximumIssuedAtSkew: .infinity),
      ApprovalHostLimits(maximumIssuedAtSkew: .nan),
      ApprovalHostLimits(maximumSubplans: 0),
      ApprovalHostLimits(maximumSubplans: 7),
      ApprovalHostLimits(maximumPolicyItems: 0),
      ApprovalHostLimits(maximumPolicyItems: 65),
    ]

    for limits in invalidLimits {
      XCTAssertThrowsError(try trustedConfiguration(limits: limits))
    }
  }

  func testBuildMetadataGeneratesRoleBoundRequirementsAndValidatedConfiguration() throws {
    let metadata = try buildMetadata()
    let configuration = try metadata.makeTrustedHostConfiguration()

    XCTAssertEqual(
      metadata.expectedClientCodeSigningRequirement,
      "anchor apple generic and identifier \"com.aptsonic.adhelm.client\""
        + " and certificate leaf[subject.OU] = \"ABCDE12345\""
    )
    XCTAssertEqual(
      metadata.expectedHostCodeSigningRequirement,
      "anchor apple generic and identifier \"com.aptsonic.adhelm.host\""
        + " and certificate leaf[subject.OU] = \"ABCDE12345\""
    )
    XCTAssertEqual(
      configuration.expectedClientCodeSigningRequirement,
      metadata.expectedClientCodeSigningRequirement
    )
    XCTAssertEqual(
      configuration.expectedHostCodeSigningRequirement,
      metadata.expectedHostCodeSigningRequirement
    )
    XCTAssertEqual(configuration.initialEnrollment?.provenanceService, "com.aptsonic.adhelm.provenance")
    XCTAssertEqual(configuration.initialEnrollment?.provenanceAccount, "approval-host-v1")
    XCTAssertEqual(configuration.approvalProtocolConfigurationV1.hostReference, "host:fixture")
  }

  func testBuildMetadataRejectsRoleSwapsAndUnsafeStaticSelectorsBeforeAnyOSCheck() throws {
    let client = try ApprovalHostCodeIdentitySelectorV1(
      role: .client,
      bundleIdentifier: "com.aptsonic.adhelm.client",
      teamIdentifier: "ABCDE12345"
    )
    let host = try ApprovalHostCodeIdentitySelectorV1(
      role: .host,
      bundleIdentifier: "com.aptsonic.adhelm.host",
      teamIdentifier: "ABCDE12345"
    )

    XCTAssertThrowsError(try buildMetadata(clientCodeIdentity: host, hostCodeIdentity: client))
    XCTAssertThrowsError(try ApprovalHostCodeIdentitySelectorV1(
      role: .client,
      bundleIdentifier: "com.example.*",
      teamIdentifier: "ABCDE12345"
    ))
    XCTAssertThrowsError(try ApprovalHostCodeIdentitySelectorV1(
      role: .client,
      bundleIdentifier: "com.example.placeholder.client",
      teamIdentifier: "ABCDE12345"
    ))
    XCTAssertThrowsError(try buildMetadata(machServiceName: "com.example.*"))
    XCTAssertThrowsError(try buildMetadata(provenanceAccount: "approval\nhost"))
    XCTAssertThrowsError(try buildMetadata(signingKeyAccessGroup: "FGHIJ67890.com.example.shared"))
  }

  private func trustedConfiguration(
    hostReference: String = "host:fixture",
    signingKeyReference: String = "key:fixture",
    limits: ApprovalHostLimits = ApprovalHostLimits()
  ) throws -> TrustedHostConfiguration {
    try TrustedHostConfiguration(
      hostReference: hostReference,
      signingKeyReference: signingKeyReference,
      machServiceName: "com.aptsonic.adhelm.approval",
      expectedClientCodeSigningRequirement: "identifier \"com.aptsonic.adhelm.client\"",
      expectedHostCodeSigningRequirement: "identifier \"com.aptsonic.adhelm.host\"",
      signingKeyApplicationTag: Data("approval-host-fixture-v1".utf8),
      keyStorage: .keychainP256,
      limits: limits
    )
  }

  private func buildMetadata(
    clientCodeIdentity: ApprovalHostCodeIdentitySelectorV1? = nil,
    hostCodeIdentity: ApprovalHostCodeIdentitySelectorV1? = nil,
    machServiceName: String = "com.aptsonic.adhelm.approval",
    provenanceAccount: String = "approval-host-v1",
    signingKeyAccessGroup: String? = "ABCDE12345.com.example.shared"
  ) throws -> ApprovalHostBuildMetadataV1 {
    let client = try clientCodeIdentity ?? ApprovalHostCodeIdentitySelectorV1(
      role: .client,
      bundleIdentifier: "com.aptsonic.adhelm.client",
      teamIdentifier: "ABCDE12345"
    )
    let host = try hostCodeIdentity ?? ApprovalHostCodeIdentitySelectorV1(
      role: .host,
      bundleIdentifier: "com.aptsonic.adhelm.host",
      teamIdentifier: "ABCDE12345"
    )
    return try ApprovalHostBuildMetadataV1(
      hostReference: "host:fixture",
      signingKeyReference: "key:fixture",
      machServiceName: machServiceName,
      clientCodeIdentity: client,
      hostCodeIdentity: host,
      signingKeyApplicationTag: Data("approval-host-fixture-v1".utf8),
      signingKeyAccessGroup: signingKeyAccessGroup,
      keyStorage: .keychainP256,
      provenanceService: "com.aptsonic.adhelm.provenance",
      provenanceAccount: provenanceAccount
    )
  }

  private func configurationFixture() throws -> ConfigurationFixture {
    let url = try XCTUnwrap(
      Bundle.module.url(
        forResource: "approval-host-configuration-vectors-v1",
        withExtension: "json",
        subdirectory: "Fixtures"
      )
    )
    return try JSONDecoder().decode(ConfigurationFixture.self, from: Data(contentsOf: url))
  }
}

private struct ConfigurationFixture: Decodable {
  let schemaVersion: Int
  let scope: String
  let vectors: [ConfigurationVector]

  enum CodingKeys: String, CodingKey {
    case schemaVersion = "schema_version"
    case scope
    case vectors
  }
}

private struct ConfigurationVector: Decodable {
  let caseID: String
  let configuration: ProtocolConfigurationFixture
  let canonicalJSON: String
  let configurationDigest: String

  enum CodingKeys: String, CodingKey {
    case caseID = "case_id"
    case configuration
    case canonicalJSON = "canonical_json"
    case configurationDigest = "configuration_digest"
  }
}

private struct ProtocolConfigurationFixture: Decodable {
  let hostReference: String
  let signingKeyReference: String
  let maximumChallengeBytes: Int
  let maximumChallengeLifetimeSeconds: Int
  let maximumIssuedAtSkewSeconds: Int
  let maximumSubplans: Int
  let maximumPolicyItems: Int

  enum CodingKeys: String, CodingKey {
    case hostReference = "host_ref"
    case signingKeyReference = "signing_key_ref"
    case maximumChallengeBytes = "maximum_challenge_bytes"
    case maximumChallengeLifetimeSeconds = "maximum_challenge_lifetime_seconds"
    case maximumIssuedAtSkewSeconds = "maximum_issued_at_skew_seconds"
    case maximumSubplans = "maximum_subplans"
    case maximumPolicyItems = "maximum_policy_items"
  }
}
