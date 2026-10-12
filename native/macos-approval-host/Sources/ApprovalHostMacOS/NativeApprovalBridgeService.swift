import ApprovalHostCore
import Foundation

/// Builds the one session-bound M33 coordinator used by the dedicated native
/// bridge route. It is an internal construction seam for offline tests; the
/// installed factory below derives every selector and protected collaborator
/// from reviewed static metadata. It has no request-selected store, key,
/// service, account, provider, or Python input.
protocol NativeApprovalBridgeRedemptionFactory: AnyObject {
  func makeCoordinator(
    configuration: TrustedHostConfiguration,
    session: any AuthenticatedApprovalSession,
    shouldContinue: @escaping () -> Bool
  ) throws -> ReceiptRedemptionCoordinator
}

/// Closed factory surface accepted by the dedicated XPC service. It has no
/// raw receipt entry point and only creates a post-signing consumer for one
/// already-decoded, direct M32 request.
protocol NativeApprovalBridgePostSigningConsumerProviding: AnyObject {
  var metadata: ApprovalHostBuildMetadataV1 { get }
  func makeConsumer(
    request: NativeApprovalBridgeRequestV1,
    exchangeStartedAt: Date
  ) throws -> any ApprovalPostSigningConsumer
}

/// Fixed configuration for the dedicated post-signing bridge route. The
/// service validates a request against `metadata` before it can acquire the
/// interaction gate or invoke the approval engine.
final class NativeApprovalBridgePostSigningConsumerFactory:
  NativeApprovalBridgePostSigningConsumerProviding
{
  let metadata: ApprovalHostBuildMetadataV1
  private let configuration: TrustedHostConfiguration
  private let redemptionFactory: any NativeApprovalBridgeRedemptionFactory

  init(
    metadata: ApprovalHostBuildMetadataV1,
    redemptionFactory: any NativeApprovalBridgeRedemptionFactory
  ) throws {
    self.metadata = metadata
    configuration = try metadata.makeTrustedHostConfiguration()
    self.redemptionFactory = redemptionFactory
  }

  func makeConsumer(
    request: NativeApprovalBridgeRequestV1,
    exchangeStartedAt: Date
  ) throws -> any ApprovalPostSigningConsumer {
    _ = try request.boundChallenge(metadata: metadata, exchangeStartedAt: exchangeStartedAt)
    return NativeApprovalBridgePostSigningConsumer(
      configuration: configuration,
      metadata: metadata,
      request: request,
      exchangeStartedAt: exchangeStartedAt,
      redemptionFactory: redemptionFactory
    )
  }
}

/// The only consumer the dedicated bridge XPC route supplies to the engine.
/// It receives a signed receipt solely after review, fresh authentication, and
/// signing. It performs no provider execution and returns no reusable permit.
private final class NativeApprovalBridgePostSigningConsumer: ApprovalPostSigningConsumer {
  private let configuration: TrustedHostConfiguration
  private let metadata: ApprovalHostBuildMetadataV1
  private let request: NativeApprovalBridgeRequestV1
  private let exchangeStartedAt: Date
  private let redemptionFactory: any NativeApprovalBridgeRedemptionFactory

  init(
    configuration: TrustedHostConfiguration,
    metadata: ApprovalHostBuildMetadataV1,
    request: NativeApprovalBridgeRequestV1,
    exchangeStartedAt: Date,
    redemptionFactory: any NativeApprovalBridgeRedemptionFactory
  ) {
    self.configuration = configuration
    self.metadata = metadata
    self.request = request
    self.exchangeStartedAt = exchangeStartedAt
    self.redemptionFactory = redemptionFactory
  }

  func consume(
    signedReceipt: SignedApprovalReceipt,
    expectedChallenge: ApprovalChallenge,
    sessionLease: ApprovalPostSigningSessionLease
  ) -> Result<NativeApprovalBridgeCandidateV1, ApprovalHostError> {
    do {
      let requestChallenge = try request.boundChallenge(
        metadata: metadata,
        exchangeStartedAt: exchangeStartedAt
      )
      guard requestChallenge.canonicalBytes == expectedChallenge.canonicalBytes,
        sessionLease.shouldContinue()
      else { throw ReceiptRedemptionError.unavailable }

      let candidate = try sessionLease.withAuthenticatedSession { session in
        guard let redemptionSession = session as? any ReceiptRedemptionSession,
          sessionLease.shouldContinue()
        else { throw ReceiptRedemptionError.unavailable }
        let coordinator = try redemptionFactory.makeCoordinator(
          configuration: configuration,
          session: session,
          shouldContinue: { sessionLease.shouldContinue() }
        )
        return try coordinator.redeemBridgeCandidateUsingSession(
          request: request,
          metadata: metadata,
          exchangeStartedAt: exchangeStartedAt,
          receiptData: signedReceipt.canonicalReceipt,
          session: redemptionSession,
          shouldContinue: { sessionLease.shouldContinue() }
        )
      }
      return .success(candidate)
    } catch {
      // Receipt, key, store, and session details remain within the protected
      // handoff. The XPC service serializes only this fixed public failure.
      return .failure(.enrollmentStateUnverified)
    }
  }
}

/// The real installed bridge binding. Construction derives fixed selectors
/// only; it does not create an LAContext, call Keychain, verify a receipt, or
/// initialize/migrate/delete a redemption record.
final class SecurityNativeApprovalBridgeRedemptionFactory:
  NativeApprovalBridgeRedemptionFactory,
  NativeMetaCampaignPauseApprovalBridgeRedemptionFactory
{
  private let enrollmentCoordinator: InitialEnrollmentCoordinator
  private let fixedSlotStore: any ReceiptRedemptionFixedSlotStore
  private let operationGate: InstalledApprovalHostOperationGate?
  private let clock: () -> Date

  init(
    configuration: TrustedHostConfiguration,
    enrollmentCoordinator: InitialEnrollmentCoordinator,
    fixedSlotStore: any ReceiptRedemptionFixedSlotStore,
    operationGate: InstalledApprovalHostOperationGate? = nil,
    clock: @escaping () -> Date = Date.init
  ) throws {
    guard enrollmentCoordinator.hasExactConfiguration(configuration) else {
      throw ReceiptRedemptionFixedSlotError.unavailable
    }
    self.enrollmentCoordinator = enrollmentCoordinator
    self.fixedSlotStore = fixedSlotStore
    self.operationGate = operationGate
    self.clock = clock
  }

  func makeCoordinator(
    configuration: TrustedHostConfiguration,
    session: any AuthenticatedApprovalSession,
    shouldContinue: @escaping () -> Bool
  ) throws -> ReceiptRedemptionCoordinator {
    let continueWhileRuntimeActive = { [operationGate] in
      (operationGate?.isActive ?? true) && shouldContinue()
    }
    guard let localSession = session as? LocalAuthenticationApprovalSession,
      enrollmentCoordinator.hasExactConfiguration(configuration),
      continueWhileRuntimeActive()
    else { throw ReceiptRedemptionFixedSlotError.unavailable }
    let adapter = try ReceiptRedemptionFixedSlotAdapter(
      configuration: configuration,
      enrollmentCoordinator: enrollmentCoordinator,
      store: fixedSlotStore,
      enrollmentSession: localSession,
      redemptionSession: localSession,
      shouldContinue: continueWhileRuntimeActive
    )
    return ReceiptRedemptionCoordinator(
      configuration: configuration,
      fixedSlot: adapter,
      clock: clock
    )
  }
}
