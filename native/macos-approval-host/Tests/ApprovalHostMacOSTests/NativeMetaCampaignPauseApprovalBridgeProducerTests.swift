import CryptoKit
import Foundation
import XCTest

@testable import ApprovalHostCore
@testable import ApprovalHostMacOS

/// Fake-only coverage for M46's private V2 producer. No test opens XPC,
/// LocalAuthentication, Keychain, a helper, or a provider connection.
final class NativeMetaCampaignPauseApprovalBridgeProducerTests: XCTestCase {
  func testMetaV2CandidateAppearsOnlyAfterCurrentKeyAndDurableNonceConsumption() throws {
    let fixture = try MetaV2ProducerFixture()
    let session = BridgeProducerTestSession()

    let candidate = try fixture.produce(session: session)

    XCTAssertEqual(candidate.receiptKind, .metaCampaignPausePlan)
    XCTAssertEqual(candidate.requestDigest, fixture.request.digest)
    XCTAssertEqual(candidate.sourceContextDigest, fixture.request.sourceContextDigest)
    XCTAssertEqual(candidate.activePublicKeyX963, fixture.key.publicKey.x963Representation)
    XCTAssertEqual(candidate.nativeObservedAtText, "2030-01-01T00:00:30.000000Z")
    XCTAssertEqual(
      fixture.lease.events,
      [
        "global.read", "nonce.read", "verify", "global.replace", "global.read",
        "pending.add", "nonce.read", "verify", "verified.replace", "nonce.read",
        "global.replace", "global.read",
      ]
    )
    XCTAssertFalse(try XCTUnwrap(fixture.lease.nonce).isPending)
    XCTAssertFalse(try XCTUnwrap(fixture.lease.global).isIntentPending)
    XCTAssertEqual(session.invalidations, 1)
  }

  func testCancellationAfterDurableConsumptionReturnsNoCandidateAndPreservesRecoveryState() throws {
    let fixture = try MetaV2ProducerFixture()
    let session = BridgeProducerTestSession()

    XCTAssertThrowsError(
      try fixture.coordinator.redeemMetaCampaignPauseBridgeCandidate(
        request: fixture.request,
        metadata: fixture.metadata,
        exchangeStartedAt: fixture.now,
        receiptData: fixture.receipt.canonicalReceipt,
        session: session,
        shouldContinue: { fixture.lease.globalReplacements < 2 }
      )
    ) { error in
      XCTAssertEqual(error as? ReceiptRedemptionError, .postReservationUnverified)
    }

    XCTAssertEqual(fixture.lease.globalReplacements, 2)
    XCTAssertFalse(try XCTUnwrap(fixture.lease.nonce).isPending)
    XCTAssertFalse(try XCTUnwrap(fixture.lease.global).isIntentPending)
    XCTAssertEqual(session.invalidations, 1)
  }

  func testCurrentKeyMismatchStopsBeforeAnyReservation() throws {
    let fixture = try MetaV2ProducerFixture()
    fixture.lease.returnedKey = P256.Signing.PrivateKey().publicKey.x963Representation

    XCTAssertThrowsError(try fixture.produce(session: BridgeProducerTestSession())) { error in
      XCTAssertEqual(error as? ReceiptRedemptionError, .currentStateUnavailable)
    }
    XCTAssertEqual(fixture.lease.events, ["global.read", "nonce.read", "verify"])
    XCTAssertNil(fixture.lease.nonce)
    XCTAssertEqual(fixture.lease.globalReplacements, 0)
  }
}

struct MetaV2ProducerFixture {
  let now = Date(timeIntervalSince1970: 1_893_456_030)
  let metadata: ApprovalHostBuildMetadataV1
  let configuration: TrustedHostConfiguration
  let request: NativeMetaCampaignPauseApprovalBridgeRequestV2
  let challenge: ApprovalChallenge
  let receipt: SignedApprovalReceipt
  let key: P256.Signing.PrivateKey
  let lease: BridgeProducerTestLease
  let coordinator: ReceiptRedemptionCoordinator

  init() throws {
    metadata = try Self.metadata()
    configuration = try metadata.makeTrustedHostConfiguration()
    challenge = try ApprovalChallenge.decode(
      try Self.challengeData(),
      now: now,
      limits: configuration.limits
    )
    let descriptor = try NativeApprovalHostArtifactDescriptorV1(metadata: metadata)
    request = try NativeMetaCampaignPauseApprovalBridgeRequestV2(
      requestID: Data(repeating: 0x46, count: 32),
      sourceContextDigest: String(repeating: "b", count: 64),
      metadataDigest: descriptor.metadataDigest,
      protocolConfigurationDigest: descriptor.protocolConfigurationDigest,
      canonicalChallenge: challenge.canonicalBytes
    )
    key = P256.Signing.PrivateKey()
    let unsigned = try ApprovalReceiptFactory.makeUnsigned(
      challenge: challenge,
      review: DeterministicApprovalReview.make(for: challenge),
      configuration: configuration,
      now: now,
      receiptIdentifier: "approval-receipt:" + String(repeating: "V", count: 43)
    )
    receipt = try ApprovalReceiptFactory.finalize(
      unsigned,
      signature: key.signature(for: unsigned.signingPayload.domainSeparatedBytes).derRepresentation
    )
    lease = try BridgeProducerTestLease(configuration: configuration, key: key, now: now)
    let fixedNow = now
    coordinator = ReceiptRedemptionCoordinator(
      configuration: configuration,
      fixedSlot: BridgeProducerTestSlot(lease: lease),
      clock: { fixedNow }
    )
  }

  func produce(
    session: BridgeProducerTestSession
  ) throws -> NativeMetaCampaignPauseApprovalBridgeCandidateV2 {
    try coordinator.redeemMetaCampaignPauseBridgeCandidate(
      request: request,
      metadata: metadata,
      exchangeStartedAt: now,
      receiptData: receipt.canonicalReceipt,
      session: session
    )
  }

  static func metadata() throws -> ApprovalHostBuildMetadataV1 {
    try ApprovalHostBuildMetadataV1(
      hostReference: "host:adhelm-macos-approval-v1",
      signingKeyReference: "approval-key:fixture-v1",
      machServiceName: "com.aptsonic.adhelm.approvalhost",
      clientCodeIdentity: ApprovalHostCodeIdentitySelectorV1(
        role: .client,
        bundleIdentifier: "com.aptsonic.adhelm.client",
        teamIdentifier: "ABCDE12345"
      ),
      hostCodeIdentity: ApprovalHostCodeIdentitySelectorV1(
        role: .host,
        bundleIdentifier: "com.aptsonic.adhelm.host",
        teamIdentifier: "ABCDE12345"
      ),
      signingKeyApplicationTag: Data([0x00, 0x61, 0x62, 0x63, 0xff]),
      signingKeyAccessGroup: "ABCDE12345.com.aptsonic.adhelm.host",
      keyStorage: .keychainP256,
      provenanceService: "com.aptsonic.adhelm.provenance",
      provenanceAccount: "approval-host-v1"
    )
  }

  static func challengeData() throws -> Data {
    let testSource = URL(fileURLWithPath: #filePath)
    let packageRoot =
      testSource
      .deletingLastPathComponent()
      .deletingLastPathComponent()
      .deletingLastPathComponent()
    let fixtureURL =
      packageRoot
      .appendingPathComponent("Tests/ApprovalHostCoreTests/Fixtures/MetaPause")
      .appendingPathComponent("meta-campaign-pause-approval-v1.json")
    let root = try CanonicalJSON.parseCanonical(Data(contentsOf: fixtureURL))
    guard case .object(let fields) = root,
      case .object(let challenge)? = fields["challenge"]
    else { throw MetaV2ProducerFixtureError.invalid }
    return try CanonicalJSON.encode(.object(challenge))
  }
}

private enum MetaV2ProducerFixtureError: Error {
  case invalid
}
