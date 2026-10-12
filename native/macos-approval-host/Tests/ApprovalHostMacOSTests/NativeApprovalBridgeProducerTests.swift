import CryptoKit
import Foundation
import XCTest

@testable import ApprovalHostCore
@testable import ApprovalHostMacOS

final class NativeApprovalBridgeProducerTests: XCTestCase {
  func testBothDirectKindsProduceExactCandidateOnlyAfterDurableConsumption() throws {
    for name in ["openai-pause-challenge-v1", "ga4-key-event-create-challenge-v1"] {
      let fixture = try BridgeProducerTestFixture(challengeName: name)
      let session = BridgeProducerTestSession()
      let result = try fixture.produce(session: session)
      XCTAssertEqual(result.canonicalReceipt, fixture.receipt.canonicalReceipt)
      XCTAssertEqual(result.activePublicKeyX963, fixture.key.publicKey.x963Representation)
      XCTAssertEqual(result.requestDigest, fixture.request.digest)
      XCTAssertEqual(result.sourceContextDigest, fixture.request.sourceContextDigest)
      XCTAssertEqual(result.nativeObservedAtText, "2030-01-01T00:00:10.000000Z")
      XCTAssertEqual(
        fixture.lease.events,
        [
          "global.read", "nonce.read", "verify", "global.replace", "global.read",
          "pending.add", "nonce.read", "verify", "verified.replace", "nonce.read",
          "global.replace", "global.read",
        ])
      XCTAssertFalse(try XCTUnwrap(fixture.lease.nonce).isPending)
      XCTAssertFalse(try XCTUnwrap(fixture.lease.global).isIntentPending)
      XCTAssertEqual(session.invalidations, 1)
      XCTAssertThrowsError(try fixture.produce(session: BridgeProducerTestSession())) { error in
        XCTAssertEqual(error as? ReceiptRedemptionError, .replayed)
      }
    }
  }

  func testMismatchedStaticConfigurationFailsBeforeProtectedLease() throws {
    let fixture = try BridgeProducerTestFixture()
    let session = BridgeProducerTestSession()
    let other = try BridgeProducerTestFixture.metadata(keyReference: "key:another-fixed-key")
    XCTAssertThrowsError(
      try fixture.coordinator.redeemBridgeCandidate(
        request: fixture.request, metadata: other, exchangeStartedAt: fixture.now,
        receiptData: fixture.receipt.canonicalReceipt, session: session)
    ) { error in
      XCTAssertEqual(error as? ReceiptRedemptionError, .unavailable)
    }
    XCTAssertEqual(session.invalidations, 1)
    XCTAssertEqual(fixture.lease.events, [])
  }

  func testCorrectReceiptWithUnrelatedReturnedKeyCannotReserveNonce() throws {
    let fixture = try BridgeProducerTestFixture()
    fixture.lease.returnedKey = P256.Signing.PrivateKey().publicKey.x963Representation
    XCTAssertThrowsError(try fixture.produce(session: BridgeProducerTestSession())) { error in
      XCTAssertEqual(error as? ReceiptRedemptionError, .currentStateUnavailable)
    }
    XCTAssertEqual(fixture.lease.events, ["global.read", "nonce.read", "verify"])
    XCTAssertNil(fixture.lease.nonce)
  }

  func testUncertainFinalReadbackCannotProduceCandidate() throws {
    let fixture = try BridgeProducerTestFixture()
    fixture.lease.hideFinalGlobalReadback = true
    XCTAssertThrowsError(try fixture.produce(session: BridgeProducerTestSession())) { error in
      XCTAssertEqual(error as? ReceiptRedemptionError, .postReservationUnverified)
    }
    XCTAssertFalse(try XCTUnwrap(fixture.lease.nonce).isPending)
  }

  func testCurrentReceiptKeySnapshotRejectsMalformedAndDoesNotAliasMutableStorage() throws {
    let fixture = try BridgeProducerTestFixture()
    for bytes in [Data(), Data(repeating: 0, count: 65), Data(repeating: 4, count: 66)] {
      XCTAssertThrowsError(
        try ReceiptRedemptionCurrentReceipt(
          receipt: fixture.receipt, activePublicKeyX963: bytes,
          currentVerificationObservedAt: fixture.now))
    }
    let mutable = NSMutableData(data: fixture.key.publicKey.x963Representation)
    let expected = Data(bytes: mutable.bytes, count: mutable.length)
    let observation = try ReceiptRedemptionCurrentReceipt(
      receipt: fixture.receipt, activePublicKeyX963: mutable as Data,
      currentVerificationObservedAt: fixture.now)
    mutable.resetBytes(in: NSRange(location: 0, length: mutable.length))
    XCTAssertEqual(observation.activePublicKeyX963, expected)
  }
}

struct BridgeProducerTestFixture {
  let now = Date(timeIntervalSince1970: 1_893_456_010)
  let metadata: ApprovalHostBuildMetadataV1
  let request: NativeApprovalBridgeRequestV1
  let receipt: SignedApprovalReceipt
  let key: P256.Signing.PrivateKey
  let lease: BridgeProducerTestLease
  let coordinator: ReceiptRedemptionCoordinator

  init(challengeName: String = "openai-pause-challenge-v1") throws {
    metadata = try Self.metadata()
    let configuration = try metadata.makeTrustedHostConfiguration()
    let url = try XCTUnwrap(
      Bundle.module.url(
        forResource: challengeName, withExtension: "json", subdirectory: "Fixtures"))
    let challenge = try ApprovalChallenge.decode(Data(contentsOf: url), now: now)
    let descriptor = try NativeApprovalHostArtifactDescriptorV1(metadata: metadata)
    request = try NativeApprovalBridgeRequestV1(
      requestID: Data(repeating: 0x32, count: 32),
      sourceContextDigest: String(repeating: "a", count: 64),
      metadataDigest: descriptor.metadataDigest,
      protocolConfigurationDigest: descriptor.protocolConfigurationDigest,
      canonicalChallenge: challenge.canonicalBytes)
    key = P256.Signing.PrivateKey()
    let unsigned = try ApprovalReceiptFactory.makeUnsigned(
      challenge: challenge, review: DeterministicApprovalReview.make(for: challenge),
      configuration: configuration, now: now,
      receiptIdentifier: "approval-receipt:" + String(repeating: "A", count: 43))
    receipt = try ApprovalReceiptFactory.finalize(
      unsigned,
      signature: key.signature(for: unsigned.signingPayload.domainSeparatedBytes).derRepresentation)
    lease = try BridgeProducerTestLease(configuration: configuration, key: key, now: now)
    let fixedNow = now
    coordinator = ReceiptRedemptionCoordinator(
      configuration: configuration, fixedSlot: BridgeProducerTestSlot(lease: lease),
      clock: { fixedNow })
  }

  func produce(session: BridgeProducerTestSession) throws -> NativeApprovalBridgeCandidateV1 {
    try coordinator.redeemBridgeCandidate(
      request: request, metadata: metadata, exchangeStartedAt: now,
      receiptData: receipt.canonicalReceipt, session: session)
  }

  static func metadata(keyReference: String = "key:fixture") throws -> ApprovalHostBuildMetadataV1 {
    try ApprovalHostBuildMetadataV1(
      hostReference: "host:adhelm-macos-approval-v1", signingKeyReference: keyReference,
      machServiceName: "com.aptsonic.adhelm.approvalhost",
      clientCodeIdentity: ApprovalHostCodeIdentitySelectorV1(
        role: .client, bundleIdentifier: "com.aptsonic.adhelm.client", teamIdentifier: "ABCDE12345"),
      hostCodeIdentity: ApprovalHostCodeIdentitySelectorV1(
        role: .host, bundleIdentifier: "com.aptsonic.adhelm.host", teamIdentifier: "ABCDE12345"),
      signingKeyApplicationTag: Data("fixture".utf8), keyStorage: .keychainP256,
      provenanceService: "com.aptsonic.adhelm.provenance", provenanceAccount: "approval-host-v1")
  }
}

final class BridgeProducerTestSession: ReceiptRedemptionSession, @unchecked Sendable {
  var invalidations = 0
  func invalidate() { invalidations += 1 }
}

final class BridgeProducerTestSlot: ReceiptRedemptionFixedSlotCoordinator, @unchecked Sendable {
  let lease: BridgeProducerTestLease
  private let lock = NSLock()
  init(lease: BridgeProducerTestLease) { self.lease = lease }
  func withRedemptionLease<T>(_ operation: (any ReceiptRedemptionLease) throws -> T) throws -> T {
    lock.lock()
    defer { lock.unlock() }
    return try operation(lease)
  }
}

final class BridgeProducerTestLease: ReceiptRedemptionLease, @unchecked Sendable {
  let verifier: PinnedReceiptVerifier
  var returnedKey: Data
  var events: [String] = []
  var nonce: ReceiptRedemptionStateV1?
  var global: ReceiptRedemptionGlobalStateV1?
  var hideFinalGlobalReadback = false
  var globalReplacements = 0

  init(configuration: TrustedHostConfiguration, key: P256.Signing.PrivateKey, now: Date) throws {
    returnedKey = key.publicKey.x963Representation
    verifier = try PinnedReceiptVerifier(
      configuration: configuration, trustedPublicKeyX963: returnedKey)
    global = .ready(try ReceiptRedemptionGlobalReadyV1(generation: 1, observedAt: now))
  }

  func verifyCurrentReceipt(
    _ receiptData: Data, expectedChallenge: ApprovalChallenge,
    session: any ReceiptRedemptionSession, now: Date
  ) throws -> ReceiptRedemptionCurrentReceipt {
    events.append("verify")
    return try ReceiptRedemptionCurrentReceipt(
      receipt: verifier.verify(receiptData, expectedChallenge: expectedChallenge, now: now),
      activePublicKeyX963: returnedKey, currentVerificationObservedAt: now)
  }

  func loadRedemptionState(nonceDigest: String) throws -> Data? {
    events.append("nonce.read")
    return try nonce?.canonicalBytes()
  }
  func loadGlobalRedemptionState() throws -> Data? {
    events.append("global.read")
    if hideFinalGlobalReadback, globalReplacements == 2 { return nil }
    return try global?.canonicalBytes()
  }
  func writePendingRedemption(_ canonicalState: Data) throws {
    events.append("pending.add")
    guard nonce == nil else { throw ReceiptRedemptionFixedSlotError.unavailable }
    nonce = try ReceiptRedemptionStateV1.decode(canonicalState)
  }
  func replacePendingRedemption(_ expectedPending: Data, with canonicalVerified: Data) throws {
    events.append("verified.replace")
    guard try nonce?.canonicalBytes() == expectedPending else {
      throw ReceiptRedemptionFixedSlotError.unavailable
    }
    nonce = try ReceiptRedemptionStateV1.decode(canonicalVerified)
  }
  func replaceGlobalRedemptionState(_ expected: Data, with canonicalState: Data) throws {
    events.append("global.replace")
    guard try global?.canonicalBytes() == expected else {
      throw ReceiptRedemptionFixedSlotError.unavailable
    }
    global = try ReceiptRedemptionGlobalStateV1.decode(canonicalState)
    globalReplacements += 1
  }
}
