import ApprovalHostCore
import Foundation

/// Testable construction seam for the one package-only Meta campaign PAUSE V2
/// post-signing handoff. It receives no request-selected key, store, service,
/// account, host, provider, or transport facts.
protocol NativeMetaCampaignPauseApprovalBridgeRedemptionFactory: AnyObject {
  func makeCoordinator(
    configuration: TrustedHostConfiguration,
    session: any AuthenticatedApprovalSession,
    shouldContinue: @escaping () -> Bool
  ) throws -> ReceiptRedemptionCoordinator
}

/// Closed factory surface for the separate M47 XPC selector. It carries only
/// held, reviewed metadata and accepts one already-decoded Meta V2
/// request. It does not expose a raw receipt, session, transport, or generic
/// consumer construction API.
protocol NativeMetaCampaignPauseApprovalBridgePostSigningConsumerProviding: AnyObject {
  var metadata: ApprovalHostBuildMetadataV1 { get }

  func makeConsumer(
    request: NativeMetaCampaignPauseApprovalBridgeRequestV2,
    exchangeStartedAt: Date
  ) throws -> any NativeMetaCampaignPauseApprovalBridgePostSigningConsumer
}

/// Builds the closed V2 consumer after a request has been structurally and
/// metadata-bound. This is intentionally not an XPC service or runtime
/// factory. A later installed route must opt in separately and derive its
/// protected collaborators from reviewed static configuration.
final class NativeMetaCampaignPauseApprovalBridgePostSigningConsumerFactory:
  NativeMetaCampaignPauseApprovalBridgePostSigningConsumerProviding
{
  let metadata: ApprovalHostBuildMetadataV1
  private let configuration: TrustedHostConfiguration
  private let redemptionFactory: any NativeMetaCampaignPauseApprovalBridgeRedemptionFactory

  init(
    metadata: ApprovalHostBuildMetadataV1,
    redemptionFactory: any NativeMetaCampaignPauseApprovalBridgeRedemptionFactory
  ) throws {
    self.metadata = metadata
    configuration = try metadata.makeTrustedHostConfiguration()
    self.redemptionFactory = redemptionFactory
  }

  func makeConsumer(
    request: NativeMetaCampaignPauseApprovalBridgeRequestV2,
    exchangeStartedAt: Date
  ) throws -> any NativeMetaCampaignPauseApprovalBridgePostSigningConsumer {
    let challenge = try request.boundChallenge(
      metadata: metadata,
      exchangeStartedAt: exchangeStartedAt
    )
    guard challenge.kind == .metaCampaignPausePlan else {
      throw NativeApprovalBridgeProtocolError.bindingMismatch
    }
    return NativeMetaCampaignPauseApprovalBridgePostSigningConsumerImpl(
      configuration: configuration,
      metadata: metadata,
      request: request,
      exchangeStartedAt: exchangeStartedAt,
      redemptionFactory: redemptionFactory
    )
  }
}

/// The only M46 consumer passed to the separate engine V2 route. It receives
/// one signed receipt after review, fresh authentication, and signing, and
/// keeps the fresh session only inside the engine-provided synchronous lease.
/// It returns opaque V2 candidate bytes only after durable receipt/nonce
/// consumption; it has no provider, grant, XPC, or serialization surface.
private final class NativeMetaCampaignPauseApprovalBridgePostSigningConsumerImpl:
  NativeMetaCampaignPauseApprovalBridgePostSigningConsumer
{
  private let configuration: TrustedHostConfiguration
  private let metadata: ApprovalHostBuildMetadataV1
  private let request: NativeMetaCampaignPauseApprovalBridgeRequestV2
  private let exchangeStartedAt: Date
  private let redemptionFactory: any NativeMetaCampaignPauseApprovalBridgeRedemptionFactory

  init(
    configuration: TrustedHostConfiguration,
    metadata: ApprovalHostBuildMetadataV1,
    request: NativeMetaCampaignPauseApprovalBridgeRequestV2,
    exchangeStartedAt: Date,
    redemptionFactory: any NativeMetaCampaignPauseApprovalBridgeRedemptionFactory
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
  ) -> Result<NativeMetaCampaignPauseApprovalBridgeCandidateV2, ApprovalHostError> {
    do {
      let requestChallenge = try request.boundChallenge(
        metadata: metadata,
        exchangeStartedAt: exchangeStartedAt
      )
      guard requestChallenge.kind == .metaCampaignPausePlan,
        requestChallenge.canonicalBytes == expectedChallenge.canonicalBytes,
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
        return try coordinator.redeemMetaCampaignPauseBridgeCandidateUsingSession(
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
      // Protected receipt, key, session, and fixed-slot diagnostics remain
      // local. A cancellation or expiry after durable consumption produces no
      // candidate and has no retry path through this component.
      return .failure(.enrollmentStateUnverified)
    }
  }
}
