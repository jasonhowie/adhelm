import Foundation

/// A reviewed signing identity selector whose requirement grammar is generated
/// by this module. It is a build-time selector, not an observed signing result:
/// callers must still compile the generated requirement and validate a current
/// `SecCode` before constructing protected host services.
package enum ApprovalHostCodeIdentityRoleV1: String, Equatable, Sendable {
  case client
  case host
}

package struct ApprovalHostCodeIdentitySelectorV1: Equatable, Sendable {
  package let role: ApprovalHostCodeIdentityRoleV1
  package let bundleIdentifier: String
  package let teamIdentifier: String

  package init(
    role: ApprovalHostCodeIdentityRoleV1,
    bundleIdentifier: String,
    teamIdentifier: String
  ) throws {
    guard ApprovalHostBuildMetadataV1.isBundleIdentifier(bundleIdentifier),
      ApprovalHostBuildMetadataV1.isTeamIdentifier(teamIdentifier)
    else { throw ApprovalHostError.hostNotInstalled }
    self.role = role
    self.bundleIdentifier = bundleIdentifier
    self.teamIdentifier = teamIdentifier
  }

  /// The only code-requirement grammar metadata can produce. The separately
  /// reviewed bootstrap compiles this string and performs the OS identity
  /// check; generating it does not itself attest a signature.
  package var codeSigningRequirement: String {
    "anchor apple generic and identifier \"\(bundleIdentifier)\""
      + " and certificate leaf[subject.OU] = \"\(teamIdentifier)\""
  }
}

/// Closed reviewed constants for one installed approval-host build. This type
/// has no `Codable` conformance and has no loader for arguments, environment,
/// Bundle metadata, files, requests, or network data. The checked-in foundation
/// intentionally supplies no instance.
package struct ApprovalHostBuildMetadataV1: Equatable, Sendable {
  package let hostReference: String
  package let signingKeyReference: String
  package let machServiceName: String
  package let clientCodeIdentity: ApprovalHostCodeIdentitySelectorV1
  package let hostCodeIdentity: ApprovalHostCodeIdentitySelectorV1
  package let signingKeyApplicationTag: Data
  package let signingKeyAccessGroup: String?
  package let keyStorage: ProtectedKeyStorage
  package let provenanceService: String
  package let provenanceAccount: String
  package let limits: ApprovalHostLimits

  package init(
    hostReference: String,
    signingKeyReference: String,
    machServiceName: String,
    clientCodeIdentity: ApprovalHostCodeIdentitySelectorV1,
    hostCodeIdentity: ApprovalHostCodeIdentitySelectorV1,
    signingKeyApplicationTag: Data,
    signingKeyAccessGroup: String? = nil,
    keyStorage: ProtectedKeyStorage,
    provenanceService: String,
    provenanceAccount: String,
    limits: ApprovalHostLimits = ApprovalHostLimits()
  ) throws {
    guard clientCodeIdentity.role == .client,
      hostCodeIdentity.role == .host,
      clientCodeIdentity != hostCodeIdentity,
      clientCodeIdentity.bundleIdentifier != hostCodeIdentity.bundleIdentifier,
      Self.isMachServiceName(machServiceName),
      Self.isFixedSelector(provenanceService),
      Self.isFixedSelector(provenanceAccount),
      signingKeyAccessGroup.map({
        Self.isKeyAccessGroup($0, teamIdentifier: hostCodeIdentity.teamIdentifier)
      }) ?? true
    else { throw ApprovalHostError.hostNotInstalled }

    let initialEnrollment = try InitialEnrollmentConfiguration(
      provenanceService: provenanceService,
      provenanceAccount: provenanceAccount
    )
    // Validate the complete static selection now. This does not compile code
    // requirements, read a signing identity, access a protected store, or
    // create a listener.
    _ = try TrustedHostConfiguration(
      hostReference: hostReference,
      signingKeyReference: signingKeyReference,
      machServiceName: machServiceName,
      expectedClientCodeSigningRequirement: clientCodeIdentity.codeSigningRequirement,
      expectedHostCodeSigningRequirement: hostCodeIdentity.codeSigningRequirement,
      signingKeyApplicationTag: signingKeyApplicationTag,
      signingKeyAccessGroup: signingKeyAccessGroup,
      keyStorage: keyStorage,
      initialEnrollment: initialEnrollment,
      limits: limits
    )

    self.hostReference = hostReference
    self.signingKeyReference = signingKeyReference
    self.machServiceName = machServiceName
    self.clientCodeIdentity = clientCodeIdentity
    self.hostCodeIdentity = hostCodeIdentity
    self.signingKeyApplicationTag = signingKeyApplicationTag
    self.signingKeyAccessGroup = signingKeyAccessGroup
    self.keyStorage = keyStorage
    self.provenanceService = provenanceService
    self.provenanceAccount = provenanceAccount
    self.limits = limits
  }

  package var expectedClientCodeSigningRequirement: String {
    clientCodeIdentity.codeSigningRequirement
  }

  package var expectedHostCodeSigningRequirement: String {
    hostCodeIdentity.codeSigningRequirement
  }

  /// Reconstruct the trusted configuration from only the reviewed constants.
  /// It remains a local selection object; it does not prove current code
  /// identity, an installed service, protected-key availability, or authority.
  package func makeTrustedHostConfiguration() throws -> TrustedHostConfiguration {
    try TrustedHostConfiguration(
      hostReference: hostReference,
      signingKeyReference: signingKeyReference,
      machServiceName: machServiceName,
      expectedClientCodeSigningRequirement: expectedClientCodeSigningRequirement,
      expectedHostCodeSigningRequirement: expectedHostCodeSigningRequirement,
      signingKeyApplicationTag: signingKeyApplicationTag,
      signingKeyAccessGroup: signingKeyAccessGroup,
      keyStorage: keyStorage,
      initialEnrollment: try InitialEnrollmentConfiguration(
        provenanceService: provenanceService,
        provenanceAccount: provenanceAccount
      ),
      limits: limits
    )
  }

  fileprivate static func isBundleIdentifier(_ value: String) -> Bool {
    guard isFixedASCII(value, maximumBytes: 255) else { return false }
    let labels = value.split(separator: ".", omittingEmptySubsequences: false)
    guard labels.count >= 3 else { return false }
    return labels.allSatisfy { label in
      guard !label.isEmpty, label.utf8.count <= 63,
        label.first != "-", label.last != "-"
      else { return false }
      return label.unicodeScalars.allSatisfy { scalar in
        (scalar.value >= 0x41 && scalar.value <= 0x5A)
          || (scalar.value >= 0x61 && scalar.value <= 0x7A)
          || (scalar.value >= 0x30 && scalar.value <= 0x39)
          || scalar.value == 0x2D
      }
    }
  }

  fileprivate static func isTeamIdentifier(_ value: String) -> Bool {
    value.utf8.count == 10 && value.unicodeScalars.allSatisfy { scalar in
      (scalar.value >= 0x41 && scalar.value <= 0x5A)
        || (scalar.value >= 0x30 && scalar.value <= 0x39)
    }
  }

  private static func isMachServiceName(_ value: String) -> Bool {
    isBundleIdentifier(value)
  }

  private static func isKeyAccessGroup(_ value: String, teamIdentifier: String) -> Bool {
    guard value.hasPrefix("\(teamIdentifier).") else { return false }
    return isBundleIdentifier(String(value.dropFirst(teamIdentifier.count + 1)))
  }

  private static func isFixedSelector(_ value: String) -> Bool {
    isFixedASCII(value, maximumBytes: 256)
  }

  private static func isFixedASCII(_ value: String, maximumBytes: Int) -> Bool {
    guard !value.isEmpty, value.utf8.count <= maximumBytes,
      !value.contains("*"), !value.contains("${"),
      !value.localizedCaseInsensitiveContains("placeholder"),
      !value.localizedCaseInsensitiveContains("todo")
    else { return false }
    return value.unicodeScalars.allSatisfy { scalar in
      scalar.value >= 0x21 && scalar.value <= 0x7E
    }
  }
}
