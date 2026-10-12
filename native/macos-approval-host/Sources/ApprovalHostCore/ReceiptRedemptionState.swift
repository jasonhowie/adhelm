import Foundation

/// Closed, package-only errors for the future native receipt-redemption
/// boundary.  They deliberately carry no Security, Keychain, parser, or
/// cryptographic implementation detail.
package enum ReceiptRedemptionContractError: Error, Equatable, Sendable {
  case malformed
  case expired
  case clockRollback
}

/// The exact non-secret source facts reserved by a protected native receipt
/// redemption.  This is a protected-store record, not an XPC response, Python
/// trust value, or execution authorization.
package struct ReceiptRedemptionSourceBindingV1: Equatable, Sendable {
  package let configurationDigest: String
  package let challengeDigest: String
  package let challengeNonceDigest: String
  package let receiptDigest: String
  package let receiptIdentifier: String
  package let kind: ApprovalKind
  package let expiresAtText: String

  /// Binds canonical but not-yet-trusted receipt bytes before a PENDING
  /// reservation.  The later already-leased current-key verifier must return
  /// a typed receipt that reconstructs exactly this binding before promotion.
  package init(
    configuration: TrustedHostConfiguration,
    expectedChallenge: ApprovalChallenge,
    canonicalReceipt: Data
  ) throws {
    guard !canonicalReceipt.isEmpty, canonicalReceipt.count <= 16_384 else {
      throw ReceiptRedemptionContractError.malformed
    }
    let fields = try Self.receiptFields(
      canonicalReceipt,
      configuration: configuration,
      expectedChallenge: expectedChallenge
    )
    guard
      Self.exactInteger(fields["schema_version"], 1),
      Self.exactString(fields["receipt_id"]).map(Self.isReceiptIdentifier) == true,
      Self.exactString(fields["receipt_kind"]) == expectedChallenge.kind.rawValue,
      Self.exactString(fields["challenge_digest"]) == expectedChallenge.digest,
      Self.exactString(fields["challenge_nonce"]) == base64URLEncoded(expectedChallenge.nonce),
      Self.exactString(fields["host_ref"]) == configuration.hostReference,
      Self.exactString(fields["key_ref"]) == configuration.signingKeyReference,
      Self.exactString(fields["expires_at"]) == expectedChallenge.expiresAtText,
      Self.isSignatureText(Self.exactString(fields["signature"]))
    else { throw ReceiptRedemptionContractError.malformed }

    try self.init(
      configurationDigest: configuration.approvalProtocolConfigurationV1.configurationDigest,
      challengeDigest: expectedChallenge.digest,
      challengeNonceDigest: CanonicalJSON.sha256Hex(expectedChallenge.nonce),
      receiptDigest: CanonicalJSON.sha256Hex(canonicalReceipt),
      receiptIdentifier: Self.exactString(fields["receipt_id"])!,
      kind: expectedChallenge.kind,
      expiresAtText: expectedChallenge.expiresAtText
    )
  }

  /// Checks the opaque receipt returned by the protected current-key
  /// collaborator against the PENDING source binding.  It does not verify a
  /// signature itself; the collaborator is responsible for that operation.
  package func matchesVerifiedReceipt(
    _ receipt: SignedApprovalReceipt,
    configuration: TrustedHostConfiguration,
    expectedChallenge: ApprovalChallenge
  ) -> Bool {
    guard receipt.digest == CanonicalJSON.sha256Hex(receipt.canonicalReceipt) else { return false }
    guard
      let reconstructed = try? ReceiptRedemptionSourceBindingV1(
        configuration: configuration,
        expectedChallenge: expectedChallenge,
        canonicalReceipt: receipt.canonicalReceipt
      )
    else { return false }
    return reconstructed == self
      && receipt.kind == kind
      && receipt.receiptIdentifier == receiptIdentifier
      && receipt.digest == receiptDigest
  }

  package var expiryOrderingKey: String {
    ReceiptRedemptionFields.timestampOrderingKey(expiresAtText)
  }

  package func expiryDate() throws -> Date {
    try ReceiptRedemptionFields.parsedTimestamp(expiresAtText)
  }

  fileprivate init(
    configurationDigest: String,
    challengeDigest: String,
    challengeNonceDigest: String,
    receiptDigest: String,
    receiptIdentifier: String,
    kind: ApprovalKind,
    expiresAtText: String
  ) throws {
    _ = try ReceiptRedemptionFields.parsedTimestamp(expiresAtText)
    guard
      ReceiptRedemptionFields.isHexDigest(configurationDigest),
      ReceiptRedemptionFields.isHexDigest(challengeDigest),
      ReceiptRedemptionFields.isHexDigest(challengeNonceDigest),
      ReceiptRedemptionFields.isHexDigest(receiptDigest),
      Self.isReceiptIdentifier(receiptIdentifier)
    else { throw ReceiptRedemptionContractError.malformed }
    self.configurationDigest = configurationDigest
    self.challengeDigest = challengeDigest
    self.challengeNonceDigest = challengeNonceDigest
    self.receiptDigest = receiptDigest
    self.receiptIdentifier = receiptIdentifier
    self.kind = kind
    self.expiresAtText = expiresAtText
  }

  fileprivate var canonicalFields: [String: CanonicalJSONValue] {
    [
      "challenge_digest": .string(challengeDigest),
      "challenge_nonce_digest": .string(challengeNonceDigest),
      "configuration_digest": .string(configurationDigest),
      "expires_at": .string(expiresAtText),
      "receipt_digest": .string(receiptDigest),
      "receipt_id": .string(receiptIdentifier),
      "receipt_kind": .string(kind.rawValue),
    ]
  }

  fileprivate static func decode(fields: [String: CanonicalJSONValue]) throws
    -> ReceiptRedemptionSourceBindingV1
  {
    guard
      ReceiptRedemptionFields.hasExactly(
        fields,
        [
          "challenge_digest", "challenge_nonce_digest", "configuration_digest", "expires_at",
          "receipt_digest", "receipt_id", "receipt_kind",
        ]
      ),
      let kind = ApprovalKind(
        rawValue: try ReceiptRedemptionFields.requiredString(fields, "receipt_kind"))
    else { throw ReceiptRedemptionContractError.malformed }
    return try ReceiptRedemptionSourceBindingV1(
      configurationDigest: try ReceiptRedemptionFields.requiredString(
        fields, "configuration_digest"),
      challengeDigest: try ReceiptRedemptionFields.requiredString(fields, "challenge_digest"),
      challengeNonceDigest: try ReceiptRedemptionFields.requiredString(
        fields, "challenge_nonce_digest"),
      receiptDigest: try ReceiptRedemptionFields.requiredString(fields, "receipt_digest"),
      receiptIdentifier: try ReceiptRedemptionFields.requiredString(fields, "receipt_id"),
      kind: kind,
      expiresAtText: try ReceiptRedemptionFields.requiredString(fields, "expires_at")
    )
  }

  private static func receiptFields(
    _ data: Data,
    configuration: TrustedHostConfiguration,
    expectedChallenge: ApprovalChallenge
  ) throws -> [String: CanonicalJSONValue] {
    let limits = CanonicalJSONLimits(
      maximumBytes: 16_384,
      maximumDepth: 20,
      maximumContainerItems: 64,
      maximumStringBytes: 4_096
    )
    do {
      guard case .object(let fields) = try CanonicalJSON.parseCanonical(data, limits: limits) else {
        throw ReceiptRedemptionContractError.malformed
      }
      var unsignedFields = fields
      guard
        let signature = Self.exactString(unsignedFields.removeValue(forKey: "signature")),
        Self.isSignatureText(signature),
        let receiptIdentifier = Self.exactString(unsignedFields["receipt_id"]),
        let issuedAtText = Self.exactString(unsignedFields["issued_at"])
      else { throw ReceiptRedemptionContractError.malformed }
      let unsigned = try ApprovalReceiptFactory.makeUnsigned(
        challenge: expectedChallenge,
        review: try DeterministicApprovalReview.make(for: expectedChallenge),
        configuration: configuration,
        now: try Self.receiptIssueTime(issuedAtText),
        receiptIdentifier: receiptIdentifier
      )
      guard try CanonicalJSON.encode(.object(unsignedFields)) == unsigned.canonicalUnsignedReceipt
      else {
        throw ReceiptRedemptionContractError.malformed
      }
      return fields
    } catch let error as ReceiptRedemptionContractError {
      throw error
    } catch {
      throw ReceiptRedemptionContractError.malformed
    }
  }

  private static func receiptIssueTime(_ raw: String) throws -> Date {
    guard raw.utf8.count == 24 else { throw ReceiptRedemptionContractError.malformed }
    let formatter = ISO8601DateFormatter()
    formatter.timeZone = TimeZone(secondsFromGMT: 0)
    formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    guard let date = formatter.date(from: raw), ReceiptTimestamp.encode(date) == raw else {
      throw ReceiptRedemptionContractError.malformed
    }
    return date
  }

  private static func exactInteger(_ value: CanonicalJSONValue?, _ expected: Int64) -> Bool {
    value == .integer(expected)
  }

  private static func exactString(_ value: CanonicalJSONValue?) -> String? {
    guard case .string(let result) = value else { return nil }
    return result
  }

  private static func isReceiptIdentifier(_ value: String) -> Bool {
    isExactApprovalReceiptIdentifier(value)
  }

  private static func isSignatureText(_ value: String?) -> Bool {
    guard let value, !value.isEmpty, value.utf8.count <= 1_024 else { return false }
    return value.utf8.allSatisfy {
      (48...57).contains($0) || (65...90).contains($0) || (97...122).contains($0)
        || $0 == 45 || $0 == 95
    }
  }
}

/// The protected-state write that reserves an exact receipt nonce before the
/// current-key verifier can promote it.  A retained PENDING record is a
/// recovery condition, never a reusable authority.
package struct ReceiptRedemptionPendingV1: Equatable, Sendable {
  package let binding: ReceiptRedemptionSourceBindingV1
  package let reservedAtText: String

  package init(binding: ReceiptRedemptionSourceBindingV1, reservedAtText: String) throws {
    _ = try ReceiptRedemptionFields.parsedTimestamp(reservedAtText)
    guard ReceiptRedemptionFields.timestampOrderingKey(reservedAtText) < binding.expiryOrderingKey
    else {
      throw ReceiptRedemptionContractError.expired
    }
    self.binding = binding
    self.reservedAtText = reservedAtText
  }
}

/// The protected-state write that proves only that the exact PENDING record
/// was later checked by an already-leased current-key collaborator. It is not
/// an execution grant and cannot survive a later revocation without a fresh
/// installed acceptance/currentness check before Python egress.
package struct ReceiptRedemptionVerifiedV1: Equatable, Sendable {
  package let pending: ReceiptRedemptionPendingV1
  package let verifiedAtText: String

  package init(pending: ReceiptRedemptionPendingV1, verifiedAtText: String) throws {
    _ = try ReceiptRedemptionFields.parsedTimestamp(verifiedAtText)
    guard
      ReceiptRedemptionFields.timestampOrderingKey(verifiedAtText)
        >= ReceiptRedemptionFields.timestampOrderingKey(pending.reservedAtText),
      ReceiptRedemptionFields.timestampOrderingKey(verifiedAtText)
        < pending.binding.expiryOrderingKey
    else { throw ReceiptRedemptionContractError.expired }
    self.pending = pending
    self.verifiedAtText = verifiedAtText
  }
}

/// Strict, bounded canonical state for one fixed receipt-redemption store.
/// The store selects records only by the nonce digest embedded in this closed
/// value; callers cannot select an arbitrary persistence location.
package enum ReceiptRedemptionStateV1: Equatable, Sendable {
  package static let maximumCanonicalBytes = 4_096

  case pending(ReceiptRedemptionPendingV1)
  case verified(ReceiptRedemptionVerifiedV1)

  package var binding: ReceiptRedemptionSourceBindingV1 {
    switch self {
    case .pending(let record): record.binding
    case .verified(let record): record.pending.binding
    }
  }

  package var isPending: Bool {
    if case .pending = self { return true }
    return false
  }

  package func canonicalBytes() throws -> Data {
    do {
      let data = try CanonicalJSON.encode(canonicalValue)
      guard data.count <= Self.maximumCanonicalBytes else {
        throw ReceiptRedemptionContractError.malformed
      }
      return data
    } catch let error as ReceiptRedemptionContractError {
      throw error
    } catch {
      throw ReceiptRedemptionContractError.malformed
    }
  }

  package static func decode(_ data: Data) throws -> ReceiptRedemptionStateV1 {
    let limits = CanonicalJSONLimits(
      maximumBytes: maximumCanonicalBytes,
      maximumDepth: 8,
      maximumContainerItems: 16,
      maximumStringBytes: 1_024
    )
    do {
      guard case .object(let fields) = try CanonicalJSON.parseCanonical(data, limits: limits),
        fields["schema_version"] == .integer(1),
        case .string(let stateText) = fields["state"]
      else { throw ReceiptRedemptionContractError.malformed }

      switch stateText {
      case "PENDING":
        let expected = [
          "schema_version", "state", "binding", "reserved_at",
        ]
        guard ReceiptRedemptionFields.hasExactly(fields, expected),
          case .object(let bindingFields) = fields["binding"]
        else { throw ReceiptRedemptionContractError.malformed }
        return .pending(
          try ReceiptRedemptionPendingV1(
            binding: ReceiptRedemptionSourceBindingV1.decode(fields: bindingFields),
            reservedAtText: try ReceiptRedemptionFields.requiredString(fields, "reserved_at")
          )
        )
      case "VERIFIED":
        let expected = [
          "schema_version", "state", "binding", "reserved_at", "verified_at",
        ]
        guard ReceiptRedemptionFields.hasExactly(fields, expected),
          case .object(let bindingFields) = fields["binding"]
        else { throw ReceiptRedemptionContractError.malformed }
        let pending = try ReceiptRedemptionPendingV1(
          binding: ReceiptRedemptionSourceBindingV1.decode(fields: bindingFields),
          reservedAtText: try ReceiptRedemptionFields.requiredString(fields, "reserved_at")
        )
        return .verified(
          try ReceiptRedemptionVerifiedV1(
            pending: pending,
            verifiedAtText: try ReceiptRedemptionFields.requiredString(fields, "verified_at")
          )
        )
      default:
        throw ReceiptRedemptionContractError.malformed
      }
    } catch let error as ReceiptRedemptionContractError {
      throw error
    } catch {
      throw ReceiptRedemptionContractError.malformed
    }
  }

  private var canonicalValue: CanonicalJSONValue {
    switch self {
    case .pending(let record):
      .object([
        "schema_version": .integer(1),
        "state": .string("PENDING"),
        "binding": .object(record.binding.canonicalFields),
        "reserved_at": .string(record.reservedAtText),
      ])
    case .verified(let record):
      .object([
        "schema_version": .integer(1),
        "state": .string("VERIFIED"),
        "binding": .object(record.pending.binding.canonicalFields),
        "reserved_at": .string(record.pending.reservedAtText),
        "verified_at": .string(record.verifiedAtText),
      ])
    }
  }
}

/// A preinitialized global protected-store record for the one fixed
/// receipt-redemption slot.  It carries both the monotonic clock observation
/// and an exact reservation intent.  The intent is durably read back before a
/// nonce PENDING write starts, so a later missing or mismatched nonce record
/// cannot look fresh across coordinator reconstruction.
///
/// The record is intentionally separate from the nonce record.  A future
/// protected-store collaborator supplies the compare-and-replace operation
/// while its one fixed-slot lease is held; this value does not claim an atomic
/// transaction across the two records or rollback resistance against arbitrary
/// restoration of an older valid global value.
package struct ReceiptRedemptionGlobalReadyV1: Equatable, Sendable {
  package let generation: Int64
  package let observedAtText: String

  package init(generation: Int64, observedAt: Date) throws {
    try self.init(
      generation: generation,
      observedAtText: ReceiptRedemptionFields.canonicalTimestamp(observedAt)
    )
  }

  package init(generation: Int64, observedAtText: String) throws {
    guard generation > 0, generation <= CanonicalJSON.maximumSafeInteger else {
      throw ReceiptRedemptionContractError.malformed
    }
    guard try ReceiptRedemptionFields.isCanonicalGeneratedTimestamp(observedAtText) else {
      throw ReceiptRedemptionContractError.malformed
    }
    self.generation = generation
    self.observedAtText = observedAtText
  }

  package var orderingKey: String {
    ReceiptRedemptionFields.timestampOrderingKey(observedAtText)
  }

  package func date() throws -> Date {
    try ReceiptRedemptionFields.parsedTimestamp(observedAtText)
  }
}

/// A global reservation quarantine.  It is never automatically cleared in
/// this increment: a reconstructed coordinator must treat it as recovery
/// required until a future installed acceptance integration performs bounded
/// repair.
package struct ReceiptRedemptionGlobalIntentPendingV1: Equatable, Sendable {
  package let generation: Int64
  package let observedAtText: String
  package let binding: ReceiptRedemptionSourceBindingV1

  package init(
    generation: Int64,
    observedAt: Date,
    binding: ReceiptRedemptionSourceBindingV1
  ) throws {
    try self.init(
      generation: generation,
      observedAtText: ReceiptRedemptionFields.canonicalTimestamp(observedAt),
      binding: binding
    )
  }

  package init(
    generation: Int64,
    observedAtText: String,
    binding: ReceiptRedemptionSourceBindingV1
  ) throws {
    guard generation > 0, generation <= CanonicalJSON.maximumSafeInteger else {
      throw ReceiptRedemptionContractError.malformed
    }
    guard try ReceiptRedemptionFields.isCanonicalGeneratedTimestamp(observedAtText) else {
      throw ReceiptRedemptionContractError.malformed
    }
    guard ReceiptRedemptionFields.timestampOrderingKey(observedAtText) < binding.expiryOrderingKey
    else { throw ReceiptRedemptionContractError.expired }
    self.generation = generation
    self.observedAtText = observedAtText
    self.binding = binding
  }

  package var orderingKey: String {
    ReceiptRedemptionFields.timestampOrderingKey(observedAtText)
  }

  package func date() throws -> Date {
    try ReceiptRedemptionFields.parsedTimestamp(observedAtText)
  }
}

/// Strict, bounded global state for the fixed protected redemption slot.
/// READY provides the monotonic watermark. INTENT_PENDING binds the exact
/// source before the independent nonce PENDING write begins.
package enum ReceiptRedemptionGlobalStateV1: Equatable, Sendable {
  package static let maximumCanonicalBytes = 4_096

  case ready(ReceiptRedemptionGlobalReadyV1)
  case intentPending(ReceiptRedemptionGlobalIntentPendingV1)

  package var generation: Int64 {
    switch self {
    case .ready(let record): record.generation
    case .intentPending(let record): record.generation
    }
  }

  package var observedAtText: String {
    switch self {
    case .ready(let record): record.observedAtText
    case .intentPending(let record): record.observedAtText
    }
  }

  package var orderingKey: String {
    ReceiptRedemptionFields.timestampOrderingKey(observedAtText)
  }

  package var binding: ReceiptRedemptionSourceBindingV1? {
    guard case .intentPending(let record) = self else { return nil }
    return record.binding
  }

  package var isIntentPending: Bool {
    if case .intentPending = self { return true }
    return false
  }

  package func date() throws -> Date {
    try ReceiptRedemptionFields.parsedTimestamp(observedAtText)
  }

  package func nextIntent(
    binding: ReceiptRedemptionSourceBindingV1,
    observedAtText: String
  ) throws -> ReceiptRedemptionGlobalStateV1 {
    guard case .ready(let current) = self,
      // A successful redemption needs both READY -> INTENT_PENDING and
      // INTENT_PENDING -> READY. Do not begin a durable reservation if the
      // final clear could not fit CanonicalJSON's safe integer domain.
      current.generation <= CanonicalJSON.maximumSafeInteger - 2,
      ReceiptRedemptionFields.timestampOrderingKey(observedAtText) >= current.orderingKey
    else { throw ReceiptRedemptionContractError.malformed }
    return .intentPending(
      try ReceiptRedemptionGlobalIntentPendingV1(
        generation: current.generation + 1,
        observedAtText: observedAtText,
        binding: binding
      )
    )
  }

  package func nextReady(
    expectedBinding: ReceiptRedemptionSourceBindingV1,
    observedAtText: String
  ) throws -> ReceiptRedemptionGlobalStateV1 {
    guard case .intentPending(let current) = self,
      current.binding == expectedBinding,
      current.generation < CanonicalJSON.maximumSafeInteger,
      ReceiptRedemptionFields.timestampOrderingKey(observedAtText) >= current.orderingKey,
      ReceiptRedemptionFields.timestampOrderingKey(observedAtText)
        < expectedBinding.expiryOrderingKey
    else { throw ReceiptRedemptionContractError.malformed }
    return .ready(
      try ReceiptRedemptionGlobalReadyV1(
        generation: current.generation + 1,
        observedAtText: observedAtText
      )
    )
  }

  package func canonicalBytes() throws -> Data {
    do {
      let data = try CanonicalJSON.encode(canonicalValue)
      guard data.count <= Self.maximumCanonicalBytes else {
        throw ReceiptRedemptionContractError.malformed
      }
      return data
    } catch let error as ReceiptRedemptionContractError {
      throw error
    } catch {
      throw ReceiptRedemptionContractError.malformed
    }
  }

  package static func decode(_ data: Data) throws -> ReceiptRedemptionGlobalStateV1 {
    let limits = CanonicalJSONLimits(
      maximumBytes: maximumCanonicalBytes,
      maximumDepth: 8,
      maximumContainerItems: 16,
      maximumStringBytes: 1_024
    )
    do {
      guard case .object(let fields) = try CanonicalJSON.parseCanonical(data, limits: limits),
        fields["schema_version"] == .integer(1),
        case .string(let stateText) = fields["state"]
      else { throw ReceiptRedemptionContractError.malformed }

      let generation: Int64
      guard case .integer(let rawGeneration) = fields["generation"],
        rawGeneration > 0,
        rawGeneration <= CanonicalJSON.maximumSafeInteger
      else {
        throw ReceiptRedemptionContractError.malformed
      }
      generation = rawGeneration

      switch stateText {
      case "READY":
        guard
          ReceiptRedemptionFields.hasExactly(
            fields, ["schema_version", "state", "generation", "observed_at"])
        else { throw ReceiptRedemptionContractError.malformed }
        return .ready(
          try ReceiptRedemptionGlobalReadyV1(
            generation: generation,
            observedAtText: try ReceiptRedemptionFields.requiredString(fields, "observed_at")
          )
        )
      case "INTENT_PENDING":
        guard
          ReceiptRedemptionFields.hasExactly(
            fields, ["schema_version", "state", "generation", "observed_at", "binding"]),
          case .object(let bindingFields) = fields["binding"]
        else { throw ReceiptRedemptionContractError.malformed }
        return .intentPending(
          try ReceiptRedemptionGlobalIntentPendingV1(
            generation: generation,
            observedAtText: try ReceiptRedemptionFields.requiredString(fields, "observed_at"),
            binding: try ReceiptRedemptionSourceBindingV1.decode(fields: bindingFields)
          )
        )
      default:
        throw ReceiptRedemptionContractError.malformed
      }
    } catch let error as ReceiptRedemptionContractError {
      throw error
    } catch {
      throw ReceiptRedemptionContractError.malformed
    }
  }

  private var canonicalValue: CanonicalJSONValue {
    switch self {
    case .ready(let record):
      .object([
        "schema_version": .integer(1),
        "state": .string("READY"),
        "generation": .integer(record.generation),
        "observed_at": .string(record.observedAtText),
      ])
    case .intentPending(let record):
      .object([
        "schema_version": .integer(1),
        "state": .string("INTENT_PENDING"),
        "generation": .integer(record.generation),
        "observed_at": .string(record.observedAtText),
        "binding": .object(record.binding.canonicalFields),
      ])
    }
  }
}

private enum ReceiptRedemptionFields {
  static func isHexDigest(_ value: String) -> Bool {
    value.utf8.count == 64
      && value.utf8.allSatisfy {
        (48...57).contains($0) || (97...102).contains($0)
      }
  }

  static func hasExactly(_ fields: [String: CanonicalJSONValue], _ expected: [String]) -> Bool {
    fields.count == expected.count && Set(fields.keys) == Set(expected)
  }

  static func requiredString(_ fields: [String: CanonicalJSONValue], _ name: String) throws
    -> String
  {
    guard case .string(let value) = fields[name], !value.isEmpty else {
      throw ReceiptRedemptionContractError.malformed
    }
    return value
  }

  static func timestampOrderingKey(_ value: String) -> String {
    value.utf8.count == 20 ? String(value.dropLast()) + ".000000Z" : value
  }

  static func parsedTimestamp(_ value: String) throws -> Date {
    do {
      return try ClosedTimestamp.parse(value)
    } catch {
      throw ReceiptRedemptionContractError.malformed
    }
  }

  static func canonicalTimestamp(_ date: Date) throws -> String {
    guard date.timeIntervalSince1970.isFinite else {
      throw ReceiptRedemptionContractError.malformed
    }
    var calendar = Calendar(identifier: .gregorian)
    calendar.locale = Locale(identifier: "en_US_POSIX")
    calendar.timeZone = TimeZone(secondsFromGMT: 0)!
    let components = calendar.dateComponents(
      [.year, .month, .day, .hour, .minute, .second, .nanosecond], from: date)
    guard let year = components.year, let month = components.month, let day = components.day,
      let hour = components.hour, let minute = components.minute, let second = components.second,
      (1...9999).contains(year)
    else { throw ReceiptRedemptionContractError.malformed }
    let microseconds = max(0, min(999_999, (components.nanosecond ?? 0) / 1_000))
    let value = String(
      format: "%04d-%02d-%02dT%02d:%02d:%02d.%06dZ",
      year, month, day, hour, minute, second, microseconds
    )
    _ = try parsedTimestamp(value)
    return value
  }

  static func isCanonicalGeneratedTimestamp(_ value: String) throws -> Bool {
    try canonicalTimestamp(parsedTimestamp(value)) == value
  }
}
