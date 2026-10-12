/// Closed generated-route evidence for the optional installed Meta campaign
/// PAUSE bridge. It binds a generated route selection to the complete build
/// metadata and protocol configuration already held by the installed host.
/// It is build evidence only: it does not attest installation, code identity,
/// protected-key availability, or approval authority.
package struct NativeMetaCampaignPauseApprovalRouteBindingV2: Equatable, Sendable {
  private let expectedMetadataDigest: String
  private let expectedProtocolConfigurationDigest: String

  package init(
    metadata: ApprovalHostBuildMetadataV1,
    configuration: TrustedHostConfiguration,
    expectedMetadataDigest: String,
    expectedProtocolConfigurationDigest: String
  ) throws {
    guard Self.isLowercaseSHA256(expectedMetadataDigest),
      Self.isLowercaseSHA256(expectedProtocolConfigurationDigest)
    else { throw ApprovalHostError.hostNotInstalled }
    self.expectedMetadataDigest = expectedMetadataDigest
    self.expectedProtocolConfigurationDigest = expectedProtocolConfigurationDigest
    try requireMatches(metadata: metadata, configuration: configuration)
  }

  /// Rechecks the held route facts before installed construction uses the
  /// route. A generated binding can be absent, but a present binding never
  /// falls back to the V1 route after an incoherent field or digest is found.
  package func requireMatches(
    metadata: ApprovalHostBuildMetadataV1,
    configuration: TrustedHostConfiguration
  ) throws {
    do {
      let reconstructedConfiguration = try metadata.makeTrustedHostConfiguration()
      let descriptor = try NativeApprovalHostArtifactDescriptorV1(metadata: metadata)
      guard reconstructedConfiguration == configuration,
        descriptor.protocolConfiguration == configuration.approvalProtocolConfigurationV1,
        descriptor.metadataDigest == expectedMetadataDigest,
        descriptor.protocolConfigurationDigest == expectedProtocolConfigurationDigest
      else { throw ApprovalHostError.hostNotInstalled }
    } catch let error as ApprovalHostError {
      throw error
    } catch {
      throw ApprovalHostError.hostNotInstalled
    }
  }

  private static func isLowercaseSHA256(_ value: String) -> Bool {
    guard value.utf8.count == 64 else { return false }
    return value.utf8.allSatisfy { byte in
      (0x30...0x39).contains(byte) || (0x61...0x66).contains(byte)
    }
  }
}
