import ApprovalHostCore
import Foundation
@preconcurrency import LocalAuthentication
import XCTest

@testable import ApprovalHostMacOS

/// Fake-only coverage for the optional generated Meta V2 route. These tests
/// construct no Keychain query, code-identity check, XPC connection, helper,
/// or LocalAuthentication evaluation.
final class NativeMetaCampaignPauseApprovalRouteBindingRuntimeTests: XCTestCase {
  func testPairedFactoryKeepsV1AndAddsV2OnlyForAnExactBinding() throws {
    let metadata = try fixtureMetadata()
    let configuration = try metadata.makeTrustedHostConfiguration()
    let material = fakeStore(configuration: configuration)
    let gate = InstalledApprovalHostOperationGate()

    let v1Only = try material.store.makeNativeApprovalBridgePostSigningConsumerFactories(
      metadata: metadata,
      configuration: configuration,
      operationGate: gate,
      metaCampaignPauseRouteBinding: nil
    )
    XCTAssertNil(v1Only.metaCampaignPauseV2)

    let descriptor = try NativeApprovalHostArtifactDescriptorV1(metadata: metadata)
    let binding = try NativeMetaCampaignPauseApprovalRouteBindingV2(
      metadata: metadata,
      configuration: configuration,
      expectedMetadataDigest: descriptor.metadataDigest,
      expectedProtocolConfigurationDigest: descriptor.protocolConfigurationDigest
    )
    let enabled = try material.store.makeNativeApprovalBridgePostSigningConsumerFactories(
      metadata: metadata,
      configuration: configuration,
      operationGate: gate,
      metaCampaignPauseRouteBinding: binding
    )

    XCTAssertNotNil(enabled.metaCampaignPauseV2)
    XCTAssertEqual(material.provenance.calls, 0)
    XCTAssertEqual(material.keys.calls, 0)
  }

  func testConfigurationMismatchFailsBeforeAnyFakeProtectedCollaborator() throws {
    let metadata = try fixtureMetadata()
    let configuration = try metadata.makeTrustedHostConfiguration()
    let material = fakeStore(configuration: configuration)
    let mismatchedMetadata = try fixtureMetadata(signingKeyReference: "key:replacement")
    let mismatchedConfiguration = try mismatchedMetadata.makeTrustedHostConfiguration()

    XCTAssertThrowsError(
      try material.store.makeNativeApprovalBridgePostSigningConsumerFactories(
        metadata: mismatchedMetadata,
        configuration: mismatchedConfiguration,
        operationGate: InstalledApprovalHostOperationGate(),
        metaCampaignPauseRouteBinding: nil
      )
    ) { error in
      XCTAssertEqual(error as? ProtectedKeyStoreError, .unavailable)
    }
    XCTAssertEqual(material.provenance.calls, 0)
    XCTAssertEqual(material.keys.calls, 0)
  }

  func testRuntimeShutdownInvalidatesTheSharedGateThroughBothRouteProtocols() throws {
    let metadata = try fixtureMetadata()
    let configuration = try metadata.makeTrustedHostConfiguration()
    let provenance = M49Provenance()
    let keys = M49Keys()
    let coordinator = InitialEnrollmentCoordinator(
      configuration: configuration,
      codeIdentityValidator: M49CodeIdentity(),
      provenanceStore: provenance,
      keyOperations: keys,
      identifierGenerator: M49Identifier()
    )
    let fixedSlot = M49FixedSlotStore()
    let gate = InstalledApprovalHostOperationGate()
    let redemptionFactory = try SecurityNativeApprovalBridgeRedemptionFactory(
      configuration: configuration,
      enrollmentCoordinator: coordinator,
      fixedSlotStore: fixedSlot,
      operationGate: gate
    )
    let v1: any NativeApprovalBridgeRedemptionFactory = redemptionFactory
    let v2: any NativeMetaCampaignPauseApprovalBridgeRedemptionFactory = redemptionFactory

    XCTAssertTrue((v1 as AnyObject) === (v2 as AnyObject))
    let runtime = InstalledApprovalHostRuntime(
      configuration: configuration,
      store: SecurityProtectedKeyStore(
        configuration: configuration,
        codeIdentityValidator: M49CodeIdentity(),
        provenanceStore: provenance,
        keyOperations: keys,
        identifierGenerator: M49Identifier(),
        timestampGenerator: { "2030-01-01T00:00:00.000000Z" },
        clock: { Date(timeIntervalSince1970: 1_893_456_000) }
      ),
      engine: ApprovalHostEngine(
        configuration: configuration,
        reviewer: M49Reviewer(),
        authenticator: M49Authenticator(),
        signer: M49Signer()
      ),
      listener: M49Listener(),
      operationGate: gate,
      interactionGate: InstalledApprovalHostInteractionGate(performRelease: { $0() })
    )
    runtime.activateBeforeReturn()
    runtime.shutdown()
    // The context is never evaluated; the invalidated gate rejects before
    // the fixed-slot adapter can touch any protected collaborator.
    let session = LocalAuthenticationApprovalSession(context: LAContext())
    var continuationCalls = 0

    for makeCoordinator in [
      {
        try v1.makeCoordinator(
          configuration: configuration,
          session: session,
          shouldContinue: {
            continuationCalls += 1
            return true
          }
        )
      },
      {
        try v2.makeCoordinator(
          configuration: configuration,
          session: session,
          shouldContinue: {
            continuationCalls += 1
            return true
          }
        )
      },
    ] {
      XCTAssertThrowsError(try makeCoordinator()) { error in
        XCTAssertEqual(error as? ReceiptRedemptionFixedSlotError, .unavailable)
      }
    }
    XCTAssertEqual(continuationCalls, 0)
    XCTAssertEqual(provenance.calls, 0)
    XCTAssertEqual(keys.calls, 0)
    XCTAssertEqual(fixedSlot.calls, 0)
  }

  private func fakeStore(
    configuration: TrustedHostConfiguration
  ) -> (store: SecurityProtectedKeyStore, provenance: M49Provenance, keys: M49Keys) {
    let provenance = M49Provenance()
    let keys = M49Keys()
    return (
      SecurityProtectedKeyStore(
        configuration: configuration,
        codeIdentityValidator: M49CodeIdentity(),
        provenanceStore: provenance,
        keyOperations: keys,
        identifierGenerator: M49Identifier(),
        timestampGenerator: { "2030-01-01T00:00:00.000000Z" },
        clock: { Date(timeIntervalSince1970: 1_893_456_000) }
      ),
      provenance,
      keys
    )
  }

  private func fixtureMetadata(
    signingKeyReference: String = "key:fixture"
  ) throws -> ApprovalHostBuildMetadataV1 {
    try ApprovalHostBuildMetadataV1(
      hostReference: "host:adhelm-macos-approval-v1",
      signingKeyReference: signingKeyReference,
      machServiceName: "com.aptsonic.adhelm.approvalhost",
      clientCodeIdentity: try ApprovalHostCodeIdentitySelectorV1(
        role: .client,
        bundleIdentifier: "com.aptsonic.adhelm.client",
        teamIdentifier: "ABCDE12345"
      ),
      hostCodeIdentity: try ApprovalHostCodeIdentitySelectorV1(
        role: .host,
        bundleIdentifier: "com.aptsonic.adhelm.host",
        teamIdentifier: "ABCDE12345"
      ),
      signingKeyApplicationTag: Data([0x00, 0x61, 0x62, 0x63, 0xFF]),
      signingKeyAccessGroup: "ABCDE12345.com.aptsonic.adhelm.host",
      keyStorage: .keychainP256,
      provenanceService: "com.aptsonic.adhelm.provenance",
      provenanceAccount: "approval-host-v1"
    )
  }
}

private final class M49CodeIdentity: HostCodeIdentityValidator, @unchecked Sendable {
  func validateCurrentHost(requirement _: String) throws {}
}

private final class M49Provenance: EnrollmentProvenanceStore, @unchecked Sendable {
  private(set) var calls = 0

  func load(session _: any EnrollmentProtectedSession) throws -> InitialEnrollmentProvenance? {
    calls += 1
    return nil
  }

  func addPending(
    _: InitialEnrollmentProvenance,
    session _: any EnrollmentProtectedSession
  ) throws {
    calls += 1
    throw ProtectedKeyStoreError.unavailable
  }

  func replace(
    _: InitialEnrollmentProvenance,
    session _: any EnrollmentProtectedSession
  ) throws {
    calls += 1
    throw ProtectedKeyStoreError.unavailable
  }
}

private final class M49Keys: FixedSlotKeyOperations, @unchecked Sendable {
  private(set) var calls = 0

  func existingPublicKeyFingerprint(session _: any EnrollmentProtectedSession) throws -> String? {
    calls += 1
    return nil
  }

  func createAndObservePublicKeyFingerprint(
    session _: any EnrollmentProtectedSession
  ) throws -> String {
    calls += 1
    throw ProtectedKeyStoreError.unavailable
  }

  func observeExistingPublicKeyFingerprint(
    session _: any EnrollmentProtectedSession
  ) throws -> String {
    calls += 1
    throw ProtectedKeyStoreError.unavailable
  }

  func signExistingKey(
    session _: any EnrollmentProtectedSession,
    expectedPublicKeyFingerprint _: String,
    payload _: Data
  ) throws -> Data {
    calls += 1
    throw ProtectedKeyStoreError.unavailable
  }
}

private final class M49Identifier: EnrollmentIdentifierGenerator, @unchecked Sendable {
  func nextEnrollmentIdentifier() throws -> String {
    throw ProtectedKeyStoreError.unavailable
  }
}

private final class M49FixedSlotStore: ReceiptRedemptionFixedSlotStore, @unchecked Sendable {
  private(set) var calls = 0

  func loadGlobalState(session _: any EnrollmentProtectedSession) throws -> Data? {
    calls += 1
    return nil
  }

  func loadNonceState(
    nonceDigest _: String,
    session _: any EnrollmentProtectedSession
  ) throws -> Data? {
    calls += 1
    return nil
  }

  func addNonceState(
    nonceDigest _: String,
    canonicalState _: Data,
    session _: any EnrollmentProtectedSession
  ) throws {
    calls += 1
    throw ReceiptRedemptionFixedSlotError.unavailable
  }

  func replaceNonceState(
    nonceDigest _: String,
    expected _: Data,
    replacement _: Data,
    session _: any EnrollmentProtectedSession
  ) throws {
    calls += 1
    throw ReceiptRedemptionFixedSlotError.unavailable
  }

  func replaceGlobalState(
    expected _: Data,
    replacement _: Data,
    session _: any EnrollmentProtectedSession
  ) throws {
    calls += 1
    throw ReceiptRedemptionFixedSlotError.unavailable
  }
}

private final class M49Authenticator: FreshAuthenticator {
  func authenticate(
    reason _: String,
    completion _: @escaping (ApprovalAuthenticationResult) -> Void
  ) -> any ApprovalCancellation {
    NoopApprovalCancellation()
  }
}

private final class M49Reviewer: ApprovalReviewer {
  func present(
    review _: DeterministicApprovalReview,
    completion _: @escaping (ApprovalReviewDecision) -> Void
  ) -> any ApprovalCancellation {
    NoopApprovalCancellation()
  }
}

private final class M49Signer: ReceiptSigner {
  func sign(
    _: ReceiptSigningPayload,
    authenticatedSession _: any AuthenticatedApprovalSession,
    permit _: any ReceiptSigningPermit,
    completion _: @escaping (Result<Data, ApprovalHostError>) -> Void
  ) -> any ApprovalCancellation {
    NoopApprovalCancellation()
  }
}

private final class M49Listener: InstalledApprovalHostListener {
  func activate() {}
  func invalidate() {}
}
