import ApprovalHostCore
import Foundation
import XCTest

@testable import ApprovalHostMacOS

/// Independent M37 checks using only in-memory stores, sessions, and runtime
/// gates. They do not create AppKit UI, LocalAuthentication contexts,
/// Security/Keychain items, XPC objects, a helper, an installed runtime, or a
/// provider request.
final class ReceiptRedemptionGlobalSetupUltraReviewTests: XCTestCase {
  func testRuntimeInvalidationAfterStoreReturnCannotReportSetupSuccess() throws {
    let rawStore = M37UltraSetupStore()
    let operationGate = InstalledApprovalHostOperationGate()
    let interactionGate = InstalledApprovalHostInteractionGate { $0() }
    operationGate.activate()
    interactionGate.activate()
    let interactionLease = try interactionGate.acquire()
    let store = RuntimeBoundProtectedKeyStore(store: rawStore, gate: operationGate)
    let session = M37UltraSetupSession()

    // A shutdown can invalidate both gates after an underlying protected call
    // returns but before the UI's terminal result. The UI must not present
    // that result as a safe successful setup.
    rawStore.beforeReturn = {
      operationGate.invalidate()
      interactionGate.invalidate()
    }
    let result = ReceiptRedemptionGlobalSetupExecution.perform(
      store: store,
      authenticatedSession: session,
      shouldContinue: {
        do {
          try interactionGate.requireLease(interactionLease)
          return true
        } catch {
          return false
        }
      }
    )

    XCTAssertEqual(result, .failure(.enrollmentStateUnverified))
    XCTAssertEqual(rawStore.setupCalls, 1)
    interactionLease.finish()
  }

  func testUnexpectedProtectedFailureIsSanitizedAndNeverRetried() {
    let store = M37UltraSetupStore()
    store.error = M37UltraPrivateFailure.unexpected

    let result = ReceiptRedemptionGlobalSetupExecution.perform(
      store: store,
      authenticatedSession: M37UltraSetupSession(),
      shouldContinue: { true }
    )

    XCTAssertEqual(result, .failure(.signingUnavailable))
    XCTAssertEqual(store.setupCalls, 1)
  }

  func testRetainedRecoveryFailureStaysInspectionOnly() {
    let store = M37UltraSetupStore()
    store.error = ProtectedKeyStoreError.recoveryRequired

    let result = ReceiptRedemptionGlobalSetupExecution.perform(
      store: store,
      authenticatedSession: M37UltraSetupSession(),
      shouldContinue: { true }
    )

    XCTAssertEqual(result, .failure(.enrollmentStateUnverified))
    XCTAssertEqual(store.setupCalls, 1)
  }

  func testCancelledBeforeProtectedHandoffDoesNotTouchFixedStore() {
    let store = M37UltraSetupStore()

    let result = ReceiptRedemptionGlobalSetupExecution.perform(
      store: store,
      authenticatedSession: M37UltraSetupSession(),
      shouldContinue: { false }
    )

    XCTAssertEqual(result, .failure(.enrollmentStateUnverified))
    XCTAssertEqual(store.setupCalls, 0)
  }
}

private enum M37UltraPrivateFailure: Error {
  case unexpected
}

private final class M37UltraSetupSession: AuthenticatedApprovalSession, @unchecked Sendable {
  func invalidate() {}
}

private final class M37UltraSetupStore: ProtectedKeyStore, ReceiptRedemptionGlobalSetupStore,
  @unchecked Sendable
{
  var beforeReturn: (() -> Void)?
  var error: Error?
  private(set) var setupCalls = 0

  func validateConfiguredHostCodeIdentity() throws {}

  func enrollInitialPrivateKey(
    authenticatedSession _: LocalAuthenticationApprovalSession,
    shouldContinue _: () -> Bool
  ) throws -> InitialEnrollmentActive {
    throw ProtectedKeyStoreError.unavailable
  }

  func revokeInitialPrivateKey(
    authenticatedSession _: LocalAuthenticationApprovalSession,
    shouldContinue _: () -> Bool
  ) throws -> InitialEnrollmentRevoked {
    throw ProtectedKeyStoreError.unavailable
  }

  func signApprovedReceiptPayload(
    _: ReceiptSigningPayload,
    authenticatedSession _: LocalAuthenticationApprovalSession,
    claimPermit _: () -> Bool
  ) throws -> Data {
    throw ProtectedKeyStoreError.unavailable
  }

  func initializeReceiptRedemptionGlobalState(
    authenticatedSession _: any AuthenticatedApprovalSession,
    shouldContinue _: @escaping () -> Bool
  ) throws -> ReceiptRedemptionGlobalInitializationResult {
    setupCalls += 1
    beforeReturn?()
    if let error { throw error }
    return .initialized
  }
}
