import Foundation

/// The exact non-secret v1 configuration surface used to compute local
/// protocol-configuration evidence. V1 challenge and receipt wire data bind
/// their existing host/key fields, not this digest or all five limits. This is
/// deliberately narrower than `TrustedHostConfiguration`: service names, code
/// requirements, key selectors, storage, and enrollment selectors are
/// installed-host facts, not protocol-configuration facts.
public struct ApprovalHostProtocolConfigurationV1: Equatable, Sendable {
  public let hostReference: String
  public let signingKeyReference: String
  public let maximumChallengeBytes: Int
  public let maximumChallengeLifetimeSeconds: Int
  public let maximumIssuedAtSkewSeconds: Int
  public let maximumSubplans: Int
  public let maximumPolicyItems: Int
  public let canonicalBytes: Data
  public let configurationDigest: String

  fileprivate init(
    hostReference: String,
    signingKeyReference: String,
    limits: ApprovalHostLimits
  ) throws {
    guard TrustedHostConfiguration.isSafeReference(hostReference, maximumBytes: 256),
      TrustedHostConfiguration.isSafeReference(signingKeyReference, maximumBytes: 256)
    else { throw ApprovalHostError.hostNotInstalled }
    let checkedLimits = try ApprovalHostProtocolLimitsV1(limits)
    let value: CanonicalJSONValue = .object([
      "host_ref": .string(hostReference),
      "signing_key_ref": .string(signingKeyReference),
      "maximum_challenge_bytes": .integer(Int64(checkedLimits.maximumChallengeBytes)),
      "maximum_challenge_lifetime_seconds": .integer(
        Int64(checkedLimits.maximumChallengeLifetimeSeconds)
      ),
      "maximum_issued_at_skew_seconds": .integer(
        Int64(checkedLimits.maximumIssuedAtSkewSeconds)
      ),
      "maximum_subplans": .integer(Int64(checkedLimits.maximumSubplans)),
      "maximum_policy_items": .integer(Int64(checkedLimits.maximumPolicyItems)),
    ])
    do {
      canonicalBytes = try CanonicalJSON.encode(value)
    } catch {
      throw ApprovalHostError.hostNotInstalled
    }
    configurationDigest = CanonicalJSON.sha256Hex(canonicalBytes)
    self.hostReference = hostReference
    self.signingKeyReference = signingKeyReference
    maximumChallengeBytes = checkedLimits.maximumChallengeBytes
    maximumChallengeLifetimeSeconds = checkedLimits.maximumChallengeLifetimeSeconds
    maximumIssuedAtSkewSeconds = checkedLimits.maximumIssuedAtSkewSeconds
    maximumSubplans = checkedLimits.maximumSubplans
    maximumPolicyItems = checkedLimits.maximumPolicyItems
  }
}

/// Values in this type are supplied by an installed product's reviewed build
/// configuration.  They are never decoded from an approval request, command
/// argument, URL, or environment variable.  This package intentionally ships
/// without a configured instance.
public struct TrustedHostConfiguration: Equatable, Sendable {
  public let hostReference: String
  public let signingKeyReference: String
  public let machServiceName: String
  public let expectedClientCodeSigningRequirement: String
  public let expectedHostCodeSigningRequirement: String
  public let signingKeyApplicationTag: Data
  public let signingKeyAccessGroup: String?
  public let keyStorage: ProtectedKeyStorage
  /// `nil` keeps the checked-in foundation unable to enroll or use a
  /// protected signing key.  A separately reviewed installed target supplies
  /// the fixed generic-password selector for the one initial slot.
  public let initialEnrollment: InitialEnrollmentConfiguration?
  public let limits: ApprovalHostLimits
  /// Exact canonical v1 protocol data shared with the Python verifier. Its
  /// digest is evidence for only these seven fields, never an OS installation
  /// or signing-attestation claim.
  public let approvalProtocolConfigurationV1: ApprovalHostProtocolConfigurationV1

  /// An installed product supplies these values from its reviewed build
  /// configuration.  This public value type is not a runtime config loader:
  /// the foundation executable never constructs it from a request,
  /// environment variable, local file, or command argument.
  public init(
    hostReference: String,
    signingKeyReference: String,
    machServiceName: String,
    expectedClientCodeSigningRequirement: String,
    expectedHostCodeSigningRequirement: String,
    signingKeyApplicationTag: Data,
    signingKeyAccessGroup: String? = nil,
    keyStorage: ProtectedKeyStorage,
    initialEnrollment: InitialEnrollmentConfiguration? = nil,
    limits: ApprovalHostLimits = ApprovalHostLimits()
  ) throws {
    let protocolConfiguration = try ApprovalHostProtocolConfigurationV1(
      hostReference: hostReference,
      signingKeyReference: signingKeyReference,
      limits: limits
    )
    guard Self.isSafeReference(hostReference, maximumBytes: 256),
      Self.isSafeReference(signingKeyReference, maximumBytes: 256),
      Self.isSafeReference(machServiceName, maximumBytes: 256),
      Self.isSafeRequirement(expectedClientCodeSigningRequirement),
      Self.isSafeRequirement(expectedHostCodeSigningRequirement),
      !signingKeyApplicationTag.isEmpty,
      signingKeyApplicationTag.count <= 128,
      signingKeyAccessGroup.map({ Self.isSafeReference($0, maximumBytes: 256) }) ?? true
    else { throw ApprovalHostError.hostNotInstalled }
    self.hostReference = hostReference
    self.signingKeyReference = signingKeyReference
    self.machServiceName = machServiceName
    self.expectedClientCodeSigningRequirement = expectedClientCodeSigningRequirement
    self.expectedHostCodeSigningRequirement = expectedHostCodeSigningRequirement
    self.signingKeyApplicationTag = signingKeyApplicationTag
    self.signingKeyAccessGroup = signingKeyAccessGroup
    self.keyStorage = keyStorage
    self.initialEnrollment = initialEnrollment
    self.limits = limits
    approvalProtocolConfigurationV1 = protocolConfiguration
  }

  fileprivate static func isSafeReference(_ value: String, maximumBytes: Int) -> Bool {
    !value.isEmpty && value.lengthOfBytes(using: .utf8) <= maximumBytes
      && value.unicodeScalars.allSatisfy {
        $0.value >= 0x20 && $0.value != 0x7F
      }
  }

  private static func isSafeRequirement(_ value: String) -> Bool {
    // Requirement expressions are trusted build constants.  Keep them
    // bounded and single-line so a malformed deployment does not turn into
    // a hidden request-controlled routing mechanism.
    !value.isEmpty && value.lengthOfBytes(using: .utf8) <= 4_096
      && value.unicodeScalars.allSatisfy {
        $0.value >= 0x20 && $0.value != 0x7F
      }
  }
}

private struct ApprovalHostProtocolLimitsV1 {
  let maximumChallengeBytes: Int
  let maximumChallengeLifetimeSeconds: Int
  let maximumIssuedAtSkewSeconds: Int
  let maximumSubplans: Int
  let maximumPolicyItems: Int

  init(_ limits: ApprovalHostLimits) throws {
    guard (1...65_536).contains(limits.maximumChallengeBytes),
      let lifetime = Self.exactSeconds(
        limits.maximumChallengeLifetime,
        minimum: 1,
        maximum: 300
      ),
      let skew = Self.exactSeconds(
        limits.maximumIssuedAtSkew,
        minimum: 0,
        maximum: 300
      ),
      (1...6).contains(limits.maximumSubplans),
      (1...64).contains(limits.maximumPolicyItems)
    else { throw ApprovalHostError.hostNotInstalled }
    maximumChallengeBytes = limits.maximumChallengeBytes
    maximumChallengeLifetimeSeconds = lifetime
    maximumIssuedAtSkewSeconds = skew
    maximumSubplans = limits.maximumSubplans
    maximumPolicyItems = limits.maximumPolicyItems
  }

  private static func exactSeconds(
    _ value: TimeInterval,
    minimum: Int,
    maximum: Int
  ) -> Int? {
    guard value.isFinite,
      value >= TimeInterval(minimum),
      value <= TimeInterval(maximum),
      value.rounded(.towardZero) == value
    else { return nil }
    return Int(value)
  }
}

/// Fixed selectors for the one protected provenance record that accompanies
/// the configured signing-key slot.  They are build configuration, never an
/// XPC/request/config-file input.  This type deliberately has no client pin,
/// receipt configuration digest, replacement slot, or rotation metadata.
public struct InitialEnrollmentConfiguration: Equatable, Sendable {
  public let provenanceService: String
  public let provenanceAccount: String

  public init(provenanceService: String, provenanceAccount: String) throws {
    guard Self.isSafeSelector(provenanceService), Self.isSafeSelector(provenanceAccount) else {
      throw ApprovalHostError.hostNotInstalled
    }
    self.provenanceService = provenanceService
    self.provenanceAccount = provenanceAccount
  }

  private static func isSafeSelector(_ value: String) -> Bool {
    !value.isEmpty && value.lengthOfBytes(using: .utf8) <= 256
      && value.unicodeScalars.allSatisfy {
        $0.value >= 0x20 && $0.value != 0x7F
      }
  }
}

/// Secure Enclave use is a separately configured storage choice.  This enum
/// records an intended key location; it is not attestation and does not claim
/// availability until an installed host verifies an enrolled key.
public enum ProtectedKeyStorage: String, Equatable, Sendable {
  case keychainP256 = "KEYCHAIN_P256"
  case secureEnclaveP256 = "SECURE_ENCLAVE_P256"
}
