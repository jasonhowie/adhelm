import Foundation

/// The one fixed signing-key slot supported by the initial enrollment
/// protocol.  This is deliberately a closed, canonical record rather than a
/// general Keychain metadata format.  It contains no private key, public-key
/// bytes, provider data, receipt, or authority grant.
public enum InitialEnrollmentState: String, Equatable, Sendable {
  case pending = "PENDING"
  case active = "ACTIVE"
  case revoked = "REVOKED"
}

/// Compact errors used by the enrollment coordinator.  They are intentionally
/// distinct from raw Security-framework errors, which must never leave the
/// host's local boundary.
public enum InitialEnrollmentProvenanceError: Error, Equatable, Sendable {
  case malformed
  case alreadyEnrolled
  case recoveryRequired
  case revoked
  case replacementUnsupported
  case configurationMismatch
}

/// Facts fixed in the reviewed host build configuration and repeated in every
/// provenance record.  There is no slot selector supplied by a request: this
/// v1 record represents the one configured signing-key slot only.
public struct InitialEnrollmentIntent: Equatable, Sendable {
  public let hostReference: String
  public let signingKeyReference: String
  public let keyStorage: ProtectedKeyStorage

  public init(
    hostReference: String,
    signingKeyReference: String,
    keyStorage: ProtectedKeyStorage
  ) throws {
    guard EnrollmentProvenanceFields.isSafeReference(hostReference),
      EnrollmentProvenanceFields.isSafeReference(signingKeyReference)
    else { throw InitialEnrollmentProvenanceError.malformed }
    self.hostReference = hostReference
    self.signingKeyReference = signingKeyReference
    self.keyStorage = keyStorage
  }
}

/// `PENDING` records represent only a durable intent and transaction ID.  A
/// fingerprint or creation time in this state would falsely claim a key was
/// already created and observed.
public struct InitialEnrollmentPending: Equatable, Sendable {
  public let enrollmentID: String
  public let intent: InitialEnrollmentIntent

  public init(enrollmentID: String, intent: InitialEnrollmentIntent) throws {
    guard EnrollmentProvenanceFields.isEnrollmentID(enrollmentID) else {
      throw InitialEnrollmentProvenanceError.malformed
    }
    self.enrollmentID = enrollmentID
    self.intent = intent
  }
}

/// An `ACTIVE` record is creation-time continuity evidence.  The fingerprint
/// is not an ACL inspection result and does not become a remote-client trust
/// pin through this type.
public struct InitialEnrollmentActive: Equatable, Sendable {
  public let enrollmentID: String
  public let intent: InitialEnrollmentIntent
  public let publicKeyX963SHA256: String
  public let createdAtText: String

  public init(
    enrollmentID: String,
    intent: InitialEnrollmentIntent,
    publicKeyX963SHA256: String,
    createdAtText: String
  ) throws {
    guard EnrollmentProvenanceFields.isEnrollmentID(enrollmentID),
      EnrollmentProvenanceFields.isFingerprint(publicKeyX963SHA256),
      EnrollmentProvenanceFields.isCanonicalTimestamp(createdAtText)
    else { throw InitialEnrollmentProvenanceError.malformed }
    self.enrollmentID = enrollmentID
    self.intent = intent
    self.publicKeyX963SHA256 = publicKeyX963SHA256
    self.createdAtText = createdAtText
  }
}

/// The one-way setup marker for the fixed receipt-redemption global slot.
/// It lives only in schema-v2 ACTIVE provenance; schema-v1 ACTIVE records
/// remain byte-stable legacy records and are never inferred to be fresh setup
/// candidates.
public enum InitialEnrollmentRedemptionGlobalSetupState: String, Equatable, Sendable {
  case notStarted = "NOT_STARTED"
  case intentPending = "INTENT_PENDING"
  case complete = "COMPLETE"
}

/// Exact non-secret genesis facts for the one fixed redemption global slot.
/// The setup transition duplicates the active-key fingerprint so the durable
/// marker cannot be detached from the ACTIVE record it was written beside.
public struct InitialEnrollmentRedemptionGlobalSetupBinding: Equatable, Sendable {
  public let slotDigest: String
  public let initialGlobalDigest: String
  public let initialGeneration: Int64
  public let initialObservedAtText: String
  public let activeKeyX963SHA256: String

  public init(
    slotDigest: String,
    initialGlobalDigest: String,
    initialGeneration: Int64,
    initialObservedAtText: String,
    activeKeyX963SHA256: String
  ) throws {
    guard EnrollmentProvenanceFields.isFingerprint(slotDigest),
      EnrollmentProvenanceFields.isFingerprint(initialGlobalDigest),
      initialGeneration == 1,
      EnrollmentProvenanceFields.isCanonicalSixFractionTimestamp(initialObservedAtText),
      EnrollmentProvenanceFields.isFingerprint(activeKeyX963SHA256)
    else { throw InitialEnrollmentProvenanceError.malformed }
    self.slotDigest = slotDigest
    self.initialGlobalDigest = initialGlobalDigest
    self.initialGeneration = initialGeneration
    self.initialObservedAtText = initialObservedAtText
    self.activeKeyX963SHA256 = activeKeyX963SHA256
  }
}

/// A closed setup state. `NOT_STARTED` deliberately carries no binding; both
/// later states carry the complete same-key/same-slot genesis binding.
public enum InitialEnrollmentRedemptionGlobalSetup: Equatable, Sendable {
  case notStarted
  case intentPending(InitialEnrollmentRedemptionGlobalSetupBinding)
  case complete(InitialEnrollmentRedemptionGlobalSetupBinding)

  public var state: InitialEnrollmentRedemptionGlobalSetupState {
    switch self {
    case .notStarted: .notStarted
    case .intentPending: .intentPending
    case .complete: .complete
    }
  }

  fileprivate var binding: InitialEnrollmentRedemptionGlobalSetupBinding? {
    switch self {
    case .notStarted: nil
    case .intentPending(let binding), .complete(let binding): binding
    }
  }

  fileprivate var canonicalValue: CanonicalJSONValue {
    switch self {
    case .notStarted:
      .object([
        "schema_version": .integer(1),
        "state": .string(InitialEnrollmentRedemptionGlobalSetupState.notStarted.rawValue),
      ])
    case .intentPending(let binding), .complete(let binding):
      .object([
        "schema_version": .integer(1),
        "state": .string(state.rawValue),
        "slot_digest": .string(binding.slotDigest),
        "initial_global_digest": .string(binding.initialGlobalDigest),
        "initial_global_generation": .integer(binding.initialGeneration),
        "initial_global_observed_at": .string(binding.initialObservedAtText),
        "active_key_x963_sha256": .string(binding.activeKeyX963SHA256),
      ])
    }
  }

  fileprivate static func decode(_ value: CanonicalJSONValue) throws
    -> InitialEnrollmentRedemptionGlobalSetup
  {
    guard case .object(let fields) = value,
      EnrollmentProvenanceFields.integer(fields["schema_version"]) == 1,
      let stateText = EnrollmentProvenanceFields.string(fields["state"]),
      let state = InitialEnrollmentRedemptionGlobalSetupState(rawValue: stateText)
    else { throw InitialEnrollmentProvenanceError.malformed }

    switch state {
    case .notStarted:
      guard EnrollmentProvenanceFields.hasExactly(fields, ["schema_version", "state"])
      else { throw InitialEnrollmentProvenanceError.malformed }
      return .notStarted
    case .intentPending, .complete:
      let expected = [
        "schema_version", "state", "slot_digest", "initial_global_digest",
        "initial_global_generation", "initial_global_observed_at", "active_key_x963_sha256",
      ]
      guard EnrollmentProvenanceFields.hasExactly(fields, expected),
        let generation = EnrollmentProvenanceFields.integer(fields["initial_global_generation"])
      else { throw InitialEnrollmentProvenanceError.malformed }
      let binding = try InitialEnrollmentRedemptionGlobalSetupBinding(
        slotDigest: try EnrollmentProvenanceFields.requiredString(fields, "slot_digest"),
        initialGlobalDigest: try EnrollmentProvenanceFields.requiredString(
          fields, "initial_global_digest"),
        initialGeneration: generation,
        initialObservedAtText: try EnrollmentProvenanceFields.requiredString(
          fields, "initial_global_observed_at"),
        activeKeyX963SHA256: try EnrollmentProvenanceFields.requiredString(
          fields, "active_key_x963_sha256")
      )
      switch state {
      case .intentPending: return .intentPending(binding)
      case .complete: return .complete(binding)
      case .notStarted: throw InitialEnrollmentProvenanceError.malformed
      }
    }
  }
}

/// The schema-v2 ACTIVE representation. Its identity is a separate immutable
/// schema-v1-shaped value so normal signing/current-verification code can use
/// the same key continuity facts without serializing a v2 record as v1.
public struct InitialEnrollmentActiveV2: Equatable, Sendable {
  public let identity: InitialEnrollmentActive
  public let redemptionGlobalSetup: InitialEnrollmentRedemptionGlobalSetup

  public init(
    enrollmentID: String,
    intent: InitialEnrollmentIntent,
    publicKeyX963SHA256: String,
    createdAtText: String,
    redemptionGlobalSetup: InitialEnrollmentRedemptionGlobalSetup
  ) throws {
    let identity = try InitialEnrollmentActive(
      enrollmentID: enrollmentID,
      intent: intent,
      publicKeyX963SHA256: publicKeyX963SHA256,
      createdAtText: createdAtText
    )
    if let binding = redemptionGlobalSetup.binding,
      binding.activeKeyX963SHA256 != identity.publicKeyX963SHA256
    {
      throw InitialEnrollmentProvenanceError.malformed
    }
    self.identity = identity
    self.redemptionGlobalSetup = redemptionGlobalSetup
  }
}

/// A `REVOKED` record preserves the previously active identity while denying
/// signing.  This initial fixed-slot protocol never deletes, replaces, or
/// rotates the key automatically after revocation.
public struct InitialEnrollmentRevoked: Equatable, Sendable {
  public let enrollmentID: String
  public let intent: InitialEnrollmentIntent
  public let publicKeyX963SHA256: String
  public let createdAtText: String
  public let revokedAtText: String

  public init(
    enrollmentID: String,
    intent: InitialEnrollmentIntent,
    publicKeyX963SHA256: String,
    createdAtText: String,
    revokedAtText: String
  ) throws {
    guard EnrollmentProvenanceFields.isEnrollmentID(enrollmentID),
      EnrollmentProvenanceFields.isFingerprint(publicKeyX963SHA256),
      EnrollmentProvenanceFields.isCanonicalTimestamp(createdAtText),
      EnrollmentProvenanceFields.isCanonicalTimestamp(revokedAtText),
      EnrollmentProvenanceFields.timestampOrderKey(revokedAtText)
        >= EnrollmentProvenanceFields.timestampOrderKey(createdAtText)
    else { throw InitialEnrollmentProvenanceError.malformed }
    self.enrollmentID = enrollmentID
    self.intent = intent
    self.publicKeyX963SHA256 = publicKeyX963SHA256
    self.createdAtText = createdAtText
    self.revokedAtText = revokedAtText
  }
}

/// Strict, bounded canonical JSON persisted in the protected provenance item.
/// State-specific forms make incomplete or forged constructed values fail at
/// the decoding boundary instead of being normalized into an `ACTIVE` claim.
public enum InitialEnrollmentProvenance: Equatable, Sendable {
  public static let maximumCanonicalBytes = 4_096

  case pending(InitialEnrollmentPending)
  /// Strict schema-v1 ACTIVE. It remains byte-stable and cannot carry a
  /// redemption setup marker.
  case active(InitialEnrollmentActive)
  /// Strict schema-v2 ACTIVE used only for explicit redemption-global setup.
  case activeV2(InitialEnrollmentActiveV2)
  case revoked(InitialEnrollmentRevoked)

  public var state: InitialEnrollmentState {
    switch self {
    case .pending: .pending
    case .active, .activeV2: .active
    case .revoked: .revoked
    }
  }

  public var intent: InitialEnrollmentIntent {
    switch self {
    case .pending(let record): record.intent
    case .active(let record): record.intent
    case .activeV2(let record): record.identity.intent
    case .revoked(let record): record.intent
    }
  }

  public func canonicalBytes() throws -> Data {
    do {
      let data = try CanonicalJSON.encode(value)
      guard data.count <= Self.maximumCanonicalBytes else {
        throw InitialEnrollmentProvenanceError.malformed
      }
      return data
    } catch let error as InitialEnrollmentProvenanceError {
      throw error
    } catch {
      throw InitialEnrollmentProvenanceError.malformed
    }
  }

  public static func decode(_ data: Data) throws -> InitialEnrollmentProvenance {
    let limits = CanonicalJSONLimits(
      maximumBytes: maximumCanonicalBytes,
      maximumDepth: 8,
      maximumContainerItems: 12,
      maximumStringBytes: 512
    )
    let root: CanonicalJSONValue
    do {
      root = try CanonicalJSON.parseCanonical(data, limits: limits)
    } catch {
      throw InitialEnrollmentProvenanceError.malformed
    }
    guard case .object(let fields) = root,
      let schemaVersion = EnrollmentProvenanceFields.integer(fields["schema_version"]),
      schemaVersion == 1 || schemaVersion == 2,
      let stateText = EnrollmentProvenanceFields.string(fields["state"]),
      let state = InitialEnrollmentState(rawValue: stateText)
    else { throw InitialEnrollmentProvenanceError.malformed }

    let pendingFields = [
      "schema_version", "state", "enrollment_id", "host_ref", "key_ref", "key_storage",
    ]
    let activeFields = pendingFields + ["public_key_x963_sha256", "created_at"]
    let activeV2Fields = activeFields + ["redemption_global_setup"]
    let revokedFields = activeFields + ["revoked_at"]
    let enrollmentID = try EnrollmentProvenanceFields.requiredString(fields, "enrollment_id")
    let intent = try InitialEnrollmentIntent(
      hostReference: EnrollmentProvenanceFields.requiredString(fields, "host_ref"),
      signingKeyReference: EnrollmentProvenanceFields.requiredString(fields, "key_ref"),
      keyStorage: try EnrollmentProvenanceFields.requiredStorage(fields, "key_storage")
    )
    switch state {
    case .pending:
      guard schemaVersion == 1, EnrollmentProvenanceFields.hasExactly(fields, pendingFields) else {
        throw InitialEnrollmentProvenanceError.malformed
      }
      return .pending(try InitialEnrollmentPending(enrollmentID: enrollmentID, intent: intent))
    case .active:
      let fingerprint = try EnrollmentProvenanceFields.requiredString(
        fields, "public_key_x963_sha256")
      let createdAtText = try EnrollmentProvenanceFields.requiredString(fields, "created_at")
      switch schemaVersion {
      case 1:
        guard EnrollmentProvenanceFields.hasExactly(fields, activeFields) else {
          throw InitialEnrollmentProvenanceError.malformed
        }
        return .active(
          try InitialEnrollmentActive(
            enrollmentID: enrollmentID,
            intent: intent,
            publicKeyX963SHA256: fingerprint,
            createdAtText: createdAtText
          ))
      case 2:
        guard EnrollmentProvenanceFields.hasExactly(fields, activeV2Fields),
          let setupValue = fields["redemption_global_setup"]
        else { throw InitialEnrollmentProvenanceError.malformed }
        return .activeV2(
          try InitialEnrollmentActiveV2(
            enrollmentID: enrollmentID,
            intent: intent,
            publicKeyX963SHA256: fingerprint,
            createdAtText: createdAtText,
            redemptionGlobalSetup: try InitialEnrollmentRedemptionGlobalSetup.decode(setupValue)
          ))
      default:
        throw InitialEnrollmentProvenanceError.malformed
      }
    case .revoked:
      guard schemaVersion == 1, EnrollmentProvenanceFields.hasExactly(fields, revokedFields) else {
        throw InitialEnrollmentProvenanceError.malformed
      }
      return .revoked(
        try InitialEnrollmentRevoked(
          enrollmentID: enrollmentID,
          intent: intent,
          publicKeyX963SHA256: EnrollmentProvenanceFields.requiredString(
            fields, "public_key_x963_sha256"),
          createdAtText: EnrollmentProvenanceFields.requiredString(fields, "created_at"),
          revokedAtText: EnrollmentProvenanceFields.requiredString(fields, "revoked_at")
        ))
    }
  }

  private var value: CanonicalJSONValue {
    switch self {
    case .pending(let record):
      .object([
        "schema_version": .integer(1),
        "state": .string(InitialEnrollmentState.pending.rawValue),
        "enrollment_id": .string(record.enrollmentID),
        "host_ref": .string(record.intent.hostReference),
        "key_ref": .string(record.intent.signingKeyReference),
        "key_storage": .string(record.intent.keyStorage.rawValue),
      ])
    case .active(let record):
      .object([
        "schema_version": .integer(1),
        "state": .string(InitialEnrollmentState.active.rawValue),
        "enrollment_id": .string(record.enrollmentID),
        "host_ref": .string(record.intent.hostReference),
        "key_ref": .string(record.intent.signingKeyReference),
        "key_storage": .string(record.intent.keyStorage.rawValue),
        "public_key_x963_sha256": .string(record.publicKeyX963SHA256),
        "created_at": .string(record.createdAtText),
      ])
    case .activeV2(let record):
      .object([
        "schema_version": .integer(2),
        "state": .string(InitialEnrollmentState.active.rawValue),
        "enrollment_id": .string(record.identity.enrollmentID),
        "host_ref": .string(record.identity.intent.hostReference),
        "key_ref": .string(record.identity.intent.signingKeyReference),
        "key_storage": .string(record.identity.intent.keyStorage.rawValue),
        "public_key_x963_sha256": .string(record.identity.publicKeyX963SHA256),
        "created_at": .string(record.identity.createdAtText),
        "redemption_global_setup": record.redemptionGlobalSetup.canonicalValue,
      ])
    case .revoked(let record):
      .object([
        "schema_version": .integer(1),
        "state": .string(InitialEnrollmentState.revoked.rawValue),
        "enrollment_id": .string(record.enrollmentID),
        "host_ref": .string(record.intent.hostReference),
        "key_ref": .string(record.intent.signingKeyReference),
        "key_storage": .string(record.intent.keyStorage.rawValue),
        "public_key_x963_sha256": .string(record.publicKeyX963SHA256),
        "created_at": .string(record.createdAtText),
        "revoked_at": .string(record.revokedAtText),
      ])
    }
  }
}

/// Pure state transitions used by the macOS coordinator.  The coordinator is
/// responsible for durable writes and Security operations; these functions
/// only preserve the closed transition contract.
public enum InitialEnrollmentTransitions {
  public static func activate(
    pending: InitialEnrollmentPending,
    observedPublicKeyX963SHA256: String,
    createdAtText: String
  ) throws -> InitialEnrollmentActive {
    try InitialEnrollmentActive(
      enrollmentID: pending.enrollmentID,
      intent: pending.intent,
      publicKeyX963SHA256: observedPublicKeyX963SHA256,
      createdAtText: createdAtText
    )
  }

  /// Creates the schema-v2 ACTIVE record used by all newly enrolled native
  /// slots. The durable NOT_STARTED marker is what later permits exactly one
  /// explicit global setup attempt; schema-v1 ACTIVE is never inferred to
  /// mean the same thing.
  public static func activateForRedemptionGlobalSetup(
    pending: InitialEnrollmentPending,
    observedPublicKeyX963SHA256: String,
    createdAtText: String
  ) throws -> InitialEnrollmentActiveV2 {
    try InitialEnrollmentActiveV2(
      enrollmentID: pending.enrollmentID,
      intent: pending.intent,
      publicKeyX963SHA256: observedPublicKeyX963SHA256,
      createdAtText: createdAtText,
      redemptionGlobalSetup: .notStarted
    )
  }

  public static func requireUsableActive(
    _ provenance: InitialEnrollmentProvenance,
    expectedIntent: InitialEnrollmentIntent,
    observedPublicKeyX963SHA256: String
  ) throws -> InitialEnrollmentActive {
    guard expectedIntent == provenance.intent else {
      throw InitialEnrollmentProvenanceError.configurationMismatch
    }
    switch provenance {
    case .pending:
      throw InitialEnrollmentProvenanceError.recoveryRequired
    case .revoked:
      throw InitialEnrollmentProvenanceError.revoked
    case .active(let active):
      guard active.publicKeyX963SHA256 == observedPublicKeyX963SHA256 else {
        throw InitialEnrollmentProvenanceError.replacementUnsupported
      }
      return active
    case .activeV2(let active):
      guard active.identity.publicKeyX963SHA256 == observedPublicKeyX963SHA256 else {
        throw InitialEnrollmentProvenanceError.replacementUnsupported
      }
      return active.identity
    }
  }

  /// Advances only the explicit V2 setup state from NOT_STARTED to a durable
  /// intent. The MacOS coordinator is responsible for exact persistence and
  /// readback before it attempts the independent global-slot add.
  public static func beginRedemptionGlobalSetup(
    active: InitialEnrollmentActiveV2,
    binding: InitialEnrollmentRedemptionGlobalSetupBinding
  ) throws -> InitialEnrollmentActiveV2 {
    guard active.redemptionGlobalSetup == .notStarted,
      binding.activeKeyX963SHA256 == active.identity.publicKeyX963SHA256
    else { throw InitialEnrollmentProvenanceError.recoveryRequired }
    return try InitialEnrollmentActiveV2(
      enrollmentID: active.identity.enrollmentID,
      intent: active.identity.intent,
      publicKeyX963SHA256: active.identity.publicKeyX963SHA256,
      createdAtText: active.identity.createdAtText,
      redemptionGlobalSetup: .intentPending(binding)
    )
  }

  /// Completes only the exact durable intent written before the global-slot
  /// add. A caller cannot skip the intent or substitute another genesis
  /// binding after a write attempt.
  public static func completeRedemptionGlobalSetup(
    active: InitialEnrollmentActiveV2,
    binding: InitialEnrollmentRedemptionGlobalSetupBinding
  ) throws -> InitialEnrollmentActiveV2 {
    guard active.redemptionGlobalSetup == .intentPending(binding),
      binding.activeKeyX963SHA256 == active.identity.publicKeyX963SHA256
    else { throw InitialEnrollmentProvenanceError.recoveryRequired }
    return try InitialEnrollmentActiveV2(
      enrollmentID: active.identity.enrollmentID,
      intent: active.identity.intent,
      publicKeyX963SHA256: active.identity.publicKeyX963SHA256,
      createdAtText: active.identity.createdAtText,
      redemptionGlobalSetup: .complete(binding)
    )
  }

  /// Requires the complete marker for a bridge/global-state reader. This
  /// deliberately rejects V1 and NOT_STARTED/INTENT V2 records without an
  /// upcast or repair attempt.
  public static func requireCompletedRedemptionGlobalSetup(
    active: InitialEnrollmentActiveV2,
    expectedSlotDigest: String,
    observedPublicKeyX963SHA256: String
  ) throws -> InitialEnrollmentRedemptionGlobalSetupBinding {
    guard EnrollmentProvenanceFields.isFingerprint(expectedSlotDigest),
      active.identity.publicKeyX963SHA256 == observedPublicKeyX963SHA256,
      case .complete(let binding) = active.redemptionGlobalSetup,
      binding.slotDigest == expectedSlotDigest,
      binding.activeKeyX963SHA256 == observedPublicKeyX963SHA256
    else { throw InitialEnrollmentProvenanceError.recoveryRequired }
    return binding
  }

  public static func revoke(
    active: InitialEnrollmentActive,
    revokedAtText: String
  ) throws -> InitialEnrollmentRevoked {
    try InitialEnrollmentRevoked(
      enrollmentID: active.enrollmentID,
      intent: active.intent,
      publicKeyX963SHA256: active.publicKeyX963SHA256,
      createdAtText: active.createdAtText,
      revokedAtText: revokedAtText
    )
  }
}

private enum EnrollmentProvenanceFields {
  private static let timestampPattern = try! NSRegularExpression(
    pattern:
      "^([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\\.([0-9]{6}))?Z$"
  )

  static func hasExactly(_ fields: [String: CanonicalJSONValue], _ expected: [String]) -> Bool {
    fields.count == expected.count && Set(fields.keys) == Set(expected)
  }

  static func string(_ value: CanonicalJSONValue?) -> String? {
    guard case .string(let value)? = value else { return nil }
    return value
  }

  static func integer(_ value: CanonicalJSONValue?) -> Int64? {
    guard case .integer(let value)? = value else { return nil }
    return value
  }

  static func requiredString(_ fields: [String: CanonicalJSONValue], _ key: String) throws -> String
  {
    guard let value = string(fields[key]) else { throw InitialEnrollmentProvenanceError.malformed }
    return value
  }

  static func requiredStorage(_ fields: [String: CanonicalJSONValue], _ key: String) throws
    -> ProtectedKeyStorage
  {
    guard let raw = string(fields[key]), let storage = ProtectedKeyStorage(rawValue: raw) else {
      throw InitialEnrollmentProvenanceError.malformed
    }
    return storage
  }

  static func isSafeReference(_ value: String) -> Bool {
    !value.isEmpty && value.lengthOfBytes(using: .utf8) <= 256
      && value.unicodeScalars.allSatisfy {
        $0.value >= 0x20 && $0.value != 0x7F
      }
  }

  static func isEnrollmentID(_ value: String) -> Bool {
    guard value.range(of: "^[A-Za-z0-9_-]{43}$", options: .regularExpression) != nil else {
      return false
    }
    let base64 =
      value
      .replacingOccurrences(of: "-", with: "+")
      .replacingOccurrences(of: "_", with: "/")
    guard let data = Data(base64Encoded: base64 + "="), data.count == 32 else {
      return false
    }
    return data.base64EncodedString()
      .replacingOccurrences(of: "+", with: "-")
      .replacingOccurrences(of: "/", with: "_")
      .replacingOccurrences(of: "=", with: "") == value
  }

  static func isFingerprint(_ value: String) -> Bool {
    value.utf8.count == 64
      && value.utf8.allSatisfy {
        (48...57).contains($0) || (97...102).contains($0)
      }
  }

  /// Call only after lexical/calendar validation. UTC fields have fixed width;
  /// padding whole seconds preserves exact microsecond order even at dates
  /// whose Foundation floating-point representation loses that precision.
  static func timestampOrderKey(_ value: String) -> String {
    value.utf8.count == 20 ? String(value.dropLast()) + ".000000Z" : value
  }

  static func isCanonicalTimestamp(_ value: String) -> Bool {
    parseCanonicalTimestamp(value) != nil
  }

  /// Global redemption state uses generated timestamps with exactly six
  /// fractional digits. The V2 setup marker copies that genesis observation
  /// verbatim, so a whole-second or differently shaped timestamp cannot be
  /// normalized into the durable binding.
  static func isCanonicalSixFractionTimestamp(_ value: String) -> Bool {
    guard value.utf8.count == 27 else { return false }
    let bytes = Array(value.utf8)
    guard bytes[19] == 46, bytes[26] == 90,
      bytes[20..<26].allSatisfy({ (48...57).contains($0) })
    else { return false }
    return parseCanonicalTimestamp(value) != nil
  }

  static func parseCanonicalTimestamp(_ raw: String) -> Date? {
    let range = NSRange(raw.startIndex..., in: raw)
    // ICU's `$` can match before a final line terminator.  Canonical
    // provenance timestamps must consume the complete string so no hidden
    // control suffix can survive into a protected record.
    guard let match = timestampPattern.firstMatch(in: raw, range: range),
      match.range == range,
      match.numberOfRanges == 8
    else {
      return nil
    }
    func component(_ position: Int) -> Int? {
      let componentRange = match.range(at: position)
      guard componentRange.location != NSNotFound,
        let range = Range(componentRange, in: raw)
      else { return nil }
      return Int(raw[range])
    }
    guard let year = component(1), let month = component(2), let day = component(3),
      let hour = component(4), let minute = component(5), let second = component(6),
      (1...9999).contains(year), (1...12).contains(month), (0...23).contains(hour),
      (0...59).contains(minute), (0...59).contains(second)
    else { return nil }
    let fractionRange = match.range(at: 7)
    let nanosecond: Int
    if fractionRange.location == NSNotFound {
      nanosecond = 0
    } else if let range = Range(fractionRange, in: raw), let fraction = Int(raw[range]) {
      nanosecond = fraction * 1_000
    } else {
      return nil
    }
    var calendar = Calendar(identifier: .gregorian)
    calendar.locale = Locale(identifier: "en_US_POSIX")
    calendar.timeZone = TimeZone(secondsFromGMT: 0)!
    var components = DateComponents()
    components.calendar = calendar
    components.timeZone = calendar.timeZone
    components.year = year
    components.month = month
    components.day = day
    components.hour = hour
    components.minute = minute
    components.second = second
    // Validate calendar fields at the exact whole second. Adding a fraction
    // first can round a valid far-future timestamp into the next second.
    components.nanosecond = 0
    guard let date = calendar.date(from: components) else { return nil }
    let roundTrip = calendar.dateComponents(
      [.year, .month, .day, .hour, .minute, .second], from: date
    )
    guard roundTrip.year == year, roundTrip.month == month, roundTrip.day == day,
      roundTrip.hour == hour, roundTrip.minute == minute, roundTrip.second == second
    else { return nil }
    return date.addingTimeInterval(Double(nanosecond) / 1_000_000_000)
  }
}
