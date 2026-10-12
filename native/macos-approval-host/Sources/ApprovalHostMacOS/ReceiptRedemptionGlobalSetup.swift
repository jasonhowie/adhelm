import ApprovalHostCore
import Foundation

/// Narrow local-only capability for the explicit receipt-redemption global
/// setup action. It is intentionally separate from `ProtectedKeyStore`: no
/// XPC request, challenge, CLI, runtime configuration value, or caller-owned
/// selector can obtain it. The configured runtime supplies the one reviewed
/// store; XCTest may use an in-memory fake.
protocol ReceiptRedemptionGlobalSetupStore: AnyObject, Sendable {
  func validateConfiguredHostCodeIdentity() throws

  /// The concrete Security implementation accepts only a freshly authenticated
  /// `LocalAuthenticationApprovalSession`. The wider type here permits this
  /// offline orchestration seam to use synthetic sessions in tests; it never
  /// relaxes the production implementation's exact-session check.
  func initializeReceiptRedemptionGlobalState(
    authenticatedSession: any AuthenticatedApprovalSession,
    shouldContinue: @escaping () -> Bool
  ) throws -> ReceiptRedemptionGlobalInitializationResult
}

/// Pure result classification for a protected setup handoff. It neither
/// creates an authentication session nor touches Security, Keychain, AppKit,
/// XPC, a helper, or a provider. The caller owns fresh authentication and
/// session invalidation.
enum ReceiptRedemptionGlobalSetupExecution {
  static func perform(
    store: any ReceiptRedemptionGlobalSetupStore,
    authenticatedSession: any AuthenticatedApprovalSession,
    shouldContinue: @escaping () -> Bool
  ) -> Result<ReceiptRedemptionGlobalInitializationResult, ApprovalHostError> {
    // The caller reaches this only after its protected-work handoff. A
    // cancellation or stale runtime lease at either boundary cannot safely
    // claim that no durable V2 marker/global write occurred.
    guard shouldContinue() else { return .failure(.enrollmentStateUnverified) }
    do {
      let result = try store.initializeReceiptRedemptionGlobalState(
        authenticatedSession: authenticatedSession,
        shouldContinue: shouldContinue
      )
      guard shouldContinue() else { return .failure(.enrollmentStateUnverified) }
      return .success(result)
    } catch {
      return .failure(publicFailure(error, shouldContinue: shouldContinue))
    }
  }

  static func publicFailure(
    _ error: Error,
    shouldContinue: () -> Bool
  ) -> ApprovalHostError {
    guard shouldContinue() else { return .enrollmentStateUnverified }
    guard let protected = error as? ProtectedKeyStoreError else {
      return .signingUnavailable
    }
    switch protected {
    case .recoveryRequired, .replacementUnsupported, .revoked:
      // These results either name retained ambiguity directly or show that
      // current V2/key facts no longer support a safe setup attempt. The menu
      // must not offer a blind retry/reset.
      return .enrollmentStateUnverified
    case .unavailable, .alreadyEnrolled, .codeIdentityInvalid, .permitNotClaimed:
      return .signingUnavailable
    }
  }
}
