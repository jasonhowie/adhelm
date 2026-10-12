import ApprovalHostCore
import Foundation

/// Closed result of the explicit, local-only fixed-global setup operation.
/// It is not a bridge capability, enrollment attestation, permit, or
/// authority signal. No runtime, XPC, CLI, or helper route invokes it here.
enum ReceiptRedemptionGlobalInitializationResult: Equatable, Sendable {
  case initialized
  case alreadyInitialized
}

/// Facts sampled under the one existing enrollment coordinator lease before a
/// fixed-global setup or redemption operation. They are internal plumbing,
/// never a stored authority result or a bridge response.
struct ReceiptRedemptionGlobalSetupEvidence: Sendable {
  let active: InitialEnrollmentActiveV2
  let observedPublicKeyX963SHA256: String
}

/// Pure construction and validation for the genesis global record bound into
/// schema-v2 enrollment provenance. The coordinator owns every protected
/// read/write and maps all ambiguity to recovery-required; this helper has no
/// store, session, selector input, or side effect.
enum ReceiptRedemptionGlobalInitialization {
  static func makeGenesis(
    configuration: TrustedHostConfiguration,
    active: InitialEnrollmentActiveV2,
    observedAt: Date
  ) throws -> (
    state: ReceiptRedemptionGlobalStateV1,
    canonicalBytes: Data,
    binding: InitialEnrollmentRedemptionGlobalSetupBinding
  ) {
    guard case .notStarted = active.redemptionGlobalSetup else {
      throw ReceiptRedemptionContractError.malformed
    }
    let derivation = try ReceiptRedemptionFixedSlotSelectorDerivation(
      configuration: configuration
    )
    let ready = try ReceiptRedemptionGlobalReadyV1(generation: 1, observedAt: observedAt)
    let state = ReceiptRedemptionGlobalStateV1.ready(ready)
    let canonicalBytes = try state.canonicalBytes()
    let binding = try InitialEnrollmentRedemptionGlobalSetupBinding(
      slotDigest: derivation.slotDigest,
      initialGlobalDigest: CanonicalJSON.sha256Hex(canonicalBytes),
      initialGeneration: ready.generation,
      initialObservedAtText: ready.observedAtText,
      activeKeyX963SHA256: active.identity.publicKeyX963SHA256
    )
    return (state, canonicalBytes, binding)
  }

  /// Requires the exact canonical genesis bytes written under a durable setup
  /// intent. This is deliberately narrower than the normal M31 global-state
  /// reader: later generations are not valid at this point because setup has
  /// not yet reached COMPLETE.
  static func requireExactGenesis(
    _ bytes: Data,
    expectedState: ReceiptRedemptionGlobalStateV1,
    expectedCanonicalBytes: Data
  ) throws {
    guard !bytes.isEmpty,
      bytes.count <= ReceiptRedemptionGlobalStateV1.maximumCanonicalBytes,
      let snapshot = bytes.withUnsafeBytes({ raw -> Data? in
        guard let baseAddress = raw.baseAddress else { return nil }
        return Data(bytes: baseAddress, count: raw.count)
      }),
      snapshot == expectedCanonicalBytes
    else { throw ReceiptRedemptionContractError.malformed }
    let state = try ReceiptRedemptionGlobalStateV1.decode(snapshot)
    guard state == expectedState, try state.canonicalBytes() == snapshot else {
      throw ReceiptRedemptionContractError.malformed
    }
  }

  /// Validates a COMPLETE marker against a presently readable global value.
  /// Later normal M31 generations are allowed, but the exact genesis bytes
  /// must still match if the value remains at generation one. A missing,
  /// quarantined, malformed, reset, or time-regressed global record is not a
  /// new setup opportunity.
  static func requirePresentCompleteGlobal(
    _ bytes: Data,
    configuration: TrustedHostConfiguration,
    active: InitialEnrollmentActiveV2,
    observedPublicKeyX963SHA256: String
  ) throws -> InitialEnrollmentRedemptionGlobalSetupBinding {
    guard !bytes.isEmpty,
      bytes.count <= ReceiptRedemptionGlobalStateV1.maximumCanonicalBytes,
      let snapshot = bytes.withUnsafeBytes({ raw -> Data? in
        guard let baseAddress = raw.baseAddress else { return nil }
        return Data(bytes: baseAddress, count: raw.count)
      })
    else { throw ReceiptRedemptionContractError.malformed }
    let derivation = try ReceiptRedemptionFixedSlotSelectorDerivation(
      configuration: configuration
    )
    let binding = try InitialEnrollmentTransitions.requireCompletedRedemptionGlobalSetup(
      active: active,
      expectedSlotDigest: derivation.slotDigest,
      observedPublicKeyX963SHA256: observedPublicKeyX963SHA256
    )
    guard binding.slotDigest == derivation.slotDigest,
      binding.activeKeyX963SHA256 == observedPublicKeyX963SHA256
    else { throw ReceiptRedemptionContractError.malformed }

    let state = try ReceiptRedemptionGlobalStateV1.decode(snapshot)
    guard try state.canonicalBytes() == snapshot,
      case .ready(let ready) = state,
      ready.generation >= binding.initialGeneration,
      ready.orderingKey >= binding.initialObservedAtText
    else { throw ReceiptRedemptionContractError.malformed }
    if ready.generation == binding.initialGeneration,
      CanonicalJSON.sha256Hex(snapshot) != binding.initialGlobalDigest
    {
      throw ReceiptRedemptionContractError.malformed
    }
    return binding
  }
}
