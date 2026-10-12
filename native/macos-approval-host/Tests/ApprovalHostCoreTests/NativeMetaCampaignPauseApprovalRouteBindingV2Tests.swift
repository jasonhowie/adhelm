import Foundation
import XCTest

@testable import ApprovalHostCore

final class NativeMetaCampaignPauseApprovalRouteBindingV2Tests: XCTestCase {
  func testCheckedInGeneratedRouteIsAbsentForOrdinarySource() throws {
    let metadata = try fixtureMetadata()
    let configuration = try metadata.makeTrustedHostConfiguration()

    XCTAssertNil(
      try GeneratedMetaCampaignPauseApprovalRouteBinding.resolve(
        metadata: metadata,
        configuration: configuration
      )
    )
  }

  func testExactFullMetadataAndProtocolBindingIsAcceptedAndRechecked() throws {
    let metadata = try fixtureMetadata()
    let configuration = try metadata.makeTrustedHostConfiguration()
    let descriptor = try NativeApprovalHostArtifactDescriptorV1(metadata: metadata)

    let binding = try NativeMetaCampaignPauseApprovalRouteBindingV2(
      metadata: metadata,
      configuration: configuration,
      expectedMetadataDigest: descriptor.metadataDigest,
      expectedProtocolConfigurationDigest: descriptor.protocolConfigurationDigest
    )

    XCTAssertNoThrow(try binding.requireMatches(metadata: metadata, configuration: configuration))
  }

  func testNonProtocolMetadataMutationCannotReuseTheOriginalBinding() throws {
    let original = try fixtureMetadata()
    let originalDescriptor = try NativeApprovalHostArtifactDescriptorV1(metadata: original)
    let replacement = try fixtureMetadata(
      signingKeyApplicationTag: Data([0x00, 0x61, 0x62, 0x63, 0xFE])
    )
    let replacementConfiguration = try replacement.makeTrustedHostConfiguration()

    XCTAssertEqual(
      (try NativeApprovalHostArtifactDescriptorV1(metadata: replacement))
        .protocolConfigurationDigest,
      originalDescriptor.protocolConfigurationDigest
    )
    XCTAssertThrowsError(
      try NativeMetaCampaignPauseApprovalRouteBindingV2(
        metadata: replacement,
        configuration: replacementConfiguration,
        expectedMetadataDigest: originalDescriptor.metadataDigest,
        expectedProtocolConfigurationDigest: originalDescriptor.protocolConfigurationDigest
      )
    ) { error in
      XCTAssertEqual(error as? ApprovalHostError, .hostNotInstalled)
    }
  }

  func testProtocolOrConfigurationMismatchIsRejectedBeforeRouteAdmission() throws {
    let metadata = try fixtureMetadata()
    let configuration = try metadata.makeTrustedHostConfiguration()
    let descriptor = try NativeApprovalHostArtifactDescriptorV1(metadata: metadata)
    let differentMetadata = try fixtureMetadata(signingKeyReference: "key:replacement")
    let differentConfiguration = try differentMetadata.makeTrustedHostConfiguration()

    for (expectedProtocolDigest, suppliedConfiguration) in [
      (String(repeating: "f", count: 64), configuration),
      (descriptor.protocolConfigurationDigest, differentConfiguration),
    ] {
      XCTAssertThrowsError(
        try NativeMetaCampaignPauseApprovalRouteBindingV2(
          metadata: metadata,
          configuration: suppliedConfiguration,
          expectedMetadataDigest: descriptor.metadataDigest,
          expectedProtocolConfigurationDigest: expectedProtocolDigest
        )
      ) { error in
        XCTAssertEqual(error as? ApprovalHostError, .hostNotInstalled)
      }
    }
  }

  func testMalformedDigestDoesNotBecomeAnAbsentRoute() throws {
    let metadata = try fixtureMetadata()
    let configuration = try metadata.makeTrustedHostConfiguration()
    let descriptor = try NativeApprovalHostArtifactDescriptorV1(metadata: metadata)

    XCTAssertThrowsError(
      try NativeMetaCampaignPauseApprovalRouteBindingV2(
        metadata: metadata,
        configuration: configuration,
        expectedMetadataDigest: "not-a-digest",
        expectedProtocolConfigurationDigest: descriptor.protocolConfigurationDigest
      )
    ) { error in
      XCTAssertEqual(error as? ApprovalHostError, .hostNotInstalled)
    }
  }

  private func fixtureMetadata(
    signingKeyReference: String = "key:receipt",
    signingKeyApplicationTag: Data = Data([0x00, 0x61, 0x62, 0x63, 0xFF])
  ) throws -> ApprovalHostBuildMetadataV1 {
    try ApprovalHostBuildMetadataV1(
      hostReference: "host:review",
      signingKeyReference: signingKeyReference,
      machServiceName: "com.aptsonic.adhelm.approvalhost",
      clientCodeIdentity: try ApprovalHostCodeIdentitySelectorV1(
        role: .client,
        bundleIdentifier: "com.aptsonic.adhelm.client",
        teamIdentifier: "ABCDE12345"
      ),
      hostCodeIdentity: try ApprovalHostCodeIdentitySelectorV1(
        role: .host,
        bundleIdentifier: "com.aptsonic.adhelm.host",
        teamIdentifier: "ABCDE12345"
      ),
      signingKeyApplicationTag: signingKeyApplicationTag,
      signingKeyAccessGroup: "ABCDE12345.com.aptsonic.adhelm.host",
      keyStorage: .keychainP256,
      provenanceService: "com.aptsonic.adhelm.provenance",
      provenanceAccount: "approval-host-v1"
    )
  }
}
