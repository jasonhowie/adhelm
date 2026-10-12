import CryptoKit
import Foundation
import XCTest

@testable import ApprovalHostCore

/// M42 consumes only a synthetic, cross-language protocol vector. These tests
/// use no configured host, OS credential store, authentication prompt, XPC
/// connection, provider request, or provider mutation.
final class MetaCampaignPauseApprovalTests: XCTestCase {
  private let now = Date(timeIntervalSince1970: 1_893_456_000)

  func testSyntheticVectorRebuildsExactMetaActionReviewAndUnsignedReceipt() throws {
    let vector = try fixtureObject()
    try assertFixtureEnvelope(vector, fixtureKind: "META_CAMPAIGN_PAUSE_APPROVAL_V1")
    let configuration = try fixtureConfiguration(vector)
    let challengeBytes = try encodedFixtureValue(vector, key: "challenge")
    let challenge = try ApprovalChallenge.decode(
      challengeBytes,
      now: now,
      limits: configuration.limits
    )

    XCTAssertEqual(challenge.kind, .metaCampaignPausePlan)
    XCTAssertEqual(challenge.digest, try fixtureString(vector, key: "challenge_digest"))
    guard case .metaCampaignPausePlan(let grantorRef, let action) = challenge.subject else {
      return XCTFail("expected Meta campaign PAUSE subject")
    }
    XCTAssertEqual(grantorRef, "customer-grantor-1")
    XCTAssertEqual(
      action.actionID, "0c1eb224cf7f533c78efd02f75dbfff104ba7da3acbd7639eb039cf7cf389dfb")
    XCTAssertEqual(
      action.digest, "0050cbc327a69e984cd4b048a0be4d4c35da1492c3d1ed72db18beb732286d31")
    XCTAssertEqual(
      action.plan.planID,
      "meta-campaign-pause-0c1eb224cf7f533c78efd02f75dbfff104ba7da3acbd7639eb039cf7cf389dfb"
    )
    XCTAssertEqual(action.plan.subplans.count, 1)
    let subplan = try XCTUnwrap(action.plan.subplans.first)
    XCTAssertEqual(subplan.operation, .pause)
    XCTAssertEqual(subplan.provider, "meta_ads")
    XCTAssertEqual(subplan.maximumSpend.minorUnits, 0)
    XCTAssertEqual(subplan.maximumSpend.currency, "USD")
    XCTAssertNil(subplan.before.revision)
    XCTAssertNil(subplan.after.revision)
    XCTAssertEqual(action.observation.campaign.effectiveStatus, "WITH_ISSUES")

    let review = try DeterministicApprovalReview.make(for: challenge)
    XCTAssertEqual(review.facts, try encodedFixtureValue(vector, key: "review_facts"))
    XCTAssertEqual(review.digest, try fixtureString(vector, key: "review_digest"))
    XCTAssertTrue(review.text.contains("configured status ACTIVE → PAUSED"))
    XCTAssertTrue(review.text.contains("Effective delivery remains NOT_PREDICTED"))
    XCTAssertTrue(review.text.contains("zero residual spend"))
    let reviewText = try XCTUnwrap(String(data: review.facts, encoding: .utf8))
    XCTAssertFalse(reviewText.contains("credential-binding:sha256:"))
    XCTAssertFalse(reviewText.contains("meta_token_principal_v1."))
    XCTAssertFalse(reviewText.contains("configuration_digest"))
    XCTAssertFalse(reviewText.contains("profile_configuration_digest"))

    let unsigned = try ApprovalReceiptFactory.makeUnsigned(
      challenge: challenge,
      review: review,
      configuration: configuration,
      now: now,
      receiptIdentifier: "approval-receipt:" + String(repeating: "M", count: 43)
    )
    XCTAssertEqual(
      unsigned.canonicalUnsignedReceipt, try encodedFixtureValue(vector, key: "unsigned_receipt"))
    XCTAssertEqual(
      CanonicalJSON.sha256Hex(unsigned.canonicalUnsignedReceipt),
      try fixtureString(vector, key: "unsigned_receipt_digest")
    )
    XCTAssertEqual(
      CanonicalJSON.sha256Hex(unsigned.signingPayload.domainSeparatedBytes),
      try fixtureString(vector, key: "domain_separated_payload_sha256")
    )

    let key = P256.Signing.PrivateKey()
    let signed = try ApprovalReceiptFactory.finalize(
      unsigned,
      signature: try key.signature(for: unsigned.signingPayload.domainSeparatedBytes)
        .derRepresentation
    )
    let verifier = try PinnedReceiptVerifier(
      configuration: configuration,
      trustedPublicKeyX963: key.publicKey.x963Representation
    )
    XCTAssertEqual(
      try verifier.verify(signed.canonicalReceipt, expectedChallenge: challenge, now: now),
      signed
    )
  }

  func testPythonGeneratedNonzeroFractionVectorRoundTripsExactActionReviewAndReceipt() throws {
    let vector = try fixtureObject(named: "meta-campaign-pause-approval-fractional-v1")
    try assertFixtureEnvelope(vector, fixtureKind: "META_CAMPAIGN_PAUSE_APPROVAL_FRACTIONAL_V1")
    let configuration = try fixtureConfiguration(vector)
    let challengeIssuedAt = try fractionalChallengeIssuedAt(vector)
    let challenge = try ApprovalChallenge.decode(
      try encodedFixtureValue(vector, key: "challenge"),
      now: challengeIssuedAt,
      limits: configuration.limits
    )

    XCTAssertEqual(
      challenge.digest, "b7307726df09750b9876cbf381760a0bfcc20dc47b6789f81943d0b45ea883ef")
    guard case .metaCampaignPausePlan(_, let action) = challenge.subject else {
      return XCTFail("expected fractional Meta campaign PAUSE subject")
    }
    XCTAssertEqual(
      action.actionID, "9db1d4a7de24e251eb13f68a88432c0df8cee7bf947619ffbfc1d0c9e2f6c46f")
    XCTAssertEqual(
      action.plan.digest, "2ccaac4d859ebf635f66c77945acf04197008b7b39bba54ae0cea32590f32285")
    XCTAssertEqual(
      try XCTUnwrap(action.plan.subplans.first).digest,
      "b019282f253229aa2208d7bf4c55fafdc44d2d4bdaf9fc98c1f295e6ffc6ef4e"
    )
    XCTAssertEqual(action.execution.inspectedAtText, "2029-12-31T23:59:58.123456Z")
    XCTAssertEqual(action.execution.expiresAtText, "2030-01-01T01:00:00.123456Z")
    XCTAssertEqual(action.execution.dataAccessExpiresAtText, "2030-01-01T02:00:00.123456Z")
    XCTAssertEqual(action.observation.readStartedAtText, "2029-12-31T23:59:59.123456Z")
    XCTAssertEqual(action.observation.readFinishedAtText, "2030-01-01T00:00:00.123456Z")
    XCTAssertEqual(action.plan.expiresAtText, "2030-01-01T00:10:00.123456Z")
    XCTAssertEqual(
      try XCTUnwrap(action.plan.subplans.first).expiresAtText, "2030-01-01T00:10:00.123456Z")

    let review = try DeterministicApprovalReview.make(for: challenge)
    XCTAssertEqual(review.facts, try encodedFixtureValue(vector, key: "review_facts"))
    XCTAssertEqual(
      review.digest, "7fd9ea369c2bdb187a19a74e1793e0f5cfa4144f04a8fe642a8ef7be752d43ca")
    let unsigned = try ApprovalReceiptFactory.makeUnsigned(
      challenge: challenge,
      review: review,
      configuration: configuration,
      now: try fractionalReceiptIssuedAt(vector),
      receiptIdentifier: "approval-receipt:" + String(repeating: "M", count: 43)
    )
    XCTAssertEqual(
      unsigned.canonicalUnsignedReceipt, try encodedFixtureValue(vector, key: "unsigned_receipt"))
    XCTAssertEqual(
      CanonicalJSON.sha256Hex(unsigned.canonicalUnsignedReceipt),
      "e606232f9920231613537ba64a1451d62ed8fe97e1ffc4dc96867b268bc98313"
    )
    XCTAssertEqual(
      CanonicalJSON.sha256Hex(unsigned.signingPayload.domainSeparatedBytes),
      "6236b514408ae458088db397f70f001c37a5b1130719df9917520eafe560b1e9"
    )
  }

  func testRejectsMetaActionAliasesStaticSemanticDriftAndReconstructedPlanDrift() throws {
    let vector = try fixtureObject()
    let configuration = try fixtureConfiguration(vector)
    let source = try fixtureChallengeObject(vector)

    var aliasChallenge = source
    var aliasAction = try actionFields(source)
    guard case .object(var aliasAccount)? = aliasAction["account_binding"] else {
      return XCTFail("fixture account binding unavailable")
    }
    aliasAccount["account_status"] = .integer(1)
    aliasAction["account_binding"] = .object(aliasAccount)
    aliasChallenge["action"] = .object(aliasAction)
    XCTAssertThrowsError(
      try ApprovalChallenge.decode(
        CanonicalJSON.encode(.object(aliasChallenge)), now: now, limits: configuration.limits
      )
    )

    var staticDriftChallenge = source
    var staticDriftAction = try actionFields(source)
    staticDriftAction["execution_authority"] = .bool(true)
    staticDriftChallenge["action"] = .object(staticDriftAction)
    XCTAssertThrowsError(
      try ApprovalChallenge.decode(
        CanonicalJSON.encode(.object(staticDriftChallenge)), now: now, limits: configuration.limits
      )
    )

    var baselineDriftChallenge = source
    var baselineDriftAction = try actionFields(source)
    guard case .object(var observation)? = baselineDriftAction["observation_binding"],
      case .object(var campaign)? = observation["campaign"]
    else { return XCTFail("fixture campaign baseline unavailable") }
    campaign["configured_status"] = .string("PAUSED")
    observation["campaign"] = .object(campaign)
    baselineDriftAction["observation_binding"] = .object(observation)
    baselineDriftChallenge["action"] = .object(baselineDriftAction)
    XCTAssertThrowsError(
      try ApprovalChallenge.decode(
        CanonicalJSON.encode(.object(baselineDriftChallenge)), now: now,
        limits: configuration.limits
      )
    )

    var planDriftChallenge = source
    var planDriftAction = try actionFields(source)
    guard case .object(var plan)? = planDriftAction["plan"] else {
      return XCTFail("fixture plan unavailable")
    }
    plan["plan_id"] = .string("meta-campaign-pause-forged")
    planDriftAction["plan"] = .object(plan)
    planDriftChallenge["action"] = .object(planDriftAction)
    XCTAssertThrowsError(
      try ApprovalChallenge.decode(
        CanonicalJSON.encode(.object(planDriftChallenge)), now: now, limits: configuration.limits
      )
    )
  }

  func testRejectsRebuiltActionWithRedundantWholeSecondFractionSpellings() throws {
    let vector = try fixtureObject()
    let configuration = try fixtureConfiguration(vector)
    var challenge = try fixtureChallengeObject(vector)
    var action = try actionFields(challenge)

    guard case .object(var execution)? = action["execution_binding"],
      case .object(var observation)? = action["observation_binding"],
      case .object(var plan)? = action["plan"],
      case .array(var subplans)? = plan["subplans"],
      subplans.count == 1,
      case .object(var subplan) = subplans[0]
    else { return XCTFail("fixture timestamp bindings unavailable") }

    for key in ["inspected_at", "expires_at", "data_access_expires_at"] {
      execution[key] = try redundantWholeSecondSpelling(execution, key: key)
    }
    for key in ["read_started_at", "read_finished_at"] {
      observation[key] = try redundantWholeSecondSpelling(observation, key: key)
    }
    plan["expires_at"] = try redundantWholeSecondSpelling(plan, key: "expires_at")
    subplan["expires_at"] = try redundantWholeSecondSpelling(subplan, key: "expires_at")
    subplans[0] = .object(subplan)
    plan["subplans"] = .array(subplans)
    action["execution_binding"] = .object(execution)
    action["observation_binding"] = .object(observation)
    action["plan"] = .object(plan)

    let rebuiltActionID = try actionIdentifier(action)
    action["action_id"] = .string(rebuiltActionID)
    action["plan"] = try planBound(to: rebuiltActionID, from: action)
    challenge["action"] = .object(action)

    XCTAssertThrowsError(
      try ApprovalChallenge.decode(
        CanonicalJSON.encode(.object(challenge)), now: now, limits: configuration.limits
      )
    )
  }

  func testMetaChallengeCannotConstructOrDecodeNativeBridgeRequest() throws {
    let vector = try fixtureObject()
    let challenge = try encodedFixtureValue(vector, key: "challenge")
    let metadata = try fixtureMetadata()
    let descriptor = try NativeApprovalHostArtifactDescriptorV1(metadata: metadata)
    let requestID = Data(repeating: 0x5A, count: 32)

    XCTAssertThrowsError(
      try NativeApprovalBridgeRequestV1(
        requestID: requestID,
        sourceContextDigest: String(repeating: "a", count: 64),
        metadataDigest: descriptor.metadataDigest,
        protocolConfigurationDigest: descriptor.protocolConfigurationDigest,
        canonicalChallenge: challenge
      )
    ) { error in
      XCTAssertEqual(error as? NativeApprovalBridgeProtocolError, .malformedRequest)
    }

    let rawRequest = try CanonicalJSON.encode(
      .object([
        "schema_version": .integer(1),
        "message_type": .string("NATIVE_APPROVAL_BRIDGE_REQUEST_V1"),
        "request_id": .string(base64URLEncoded(requestID)),
        "source_context_digest": .string(String(repeating: "a", count: 64)),
        "metadata_digest": .string(descriptor.metadataDigest),
        "protocol_configuration_digest": .string(descriptor.protocolConfigurationDigest),
        "canonical_challenge_b64url": .string(base64URLEncoded(challenge)),
      ]))
    XCTAssertThrowsError(try NativeApprovalBridgeRequestV1.decode(rawRequest)) { error in
      XCTAssertEqual(error as? NativeApprovalBridgeProtocolError, .malformedRequest)
    }
  }

  private func fixtureObject(
    named name: String = "meta-campaign-pause-approval-v1"
  ) throws -> [String: CanonicalJSONValue] {
    let url = try XCTUnwrap(
      Bundle.module.url(
        forResource: name,
        withExtension: "json",
        subdirectory: "Fixtures/MetaPause"
      )
    )
    let data = try Data(contentsOf: url)
    guard case .object(let value) = try CanonicalJSON.parseCanonical(data) else {
      throw FixtureError.invalid
    }
    return value
  }

  private func assertFixtureEnvelope(
    _ vector: [String: CanonicalJSONValue],
    fixtureKind: String
  ) throws {
    XCTAssertEqual(
      Set(vector.keys),
      [
        "schema_version", "fixture_kind", "scope", "configuration", "challenge", "challenge_digest",
        "review_facts", "review_digest", "unsigned_receipt", "unsigned_receipt_digest",
        "domain_separated_payload_sha256",
      ]
    )
    XCTAssertEqual(vector["schema_version"], .integer(1))
    XCTAssertEqual(vector["fixture_kind"], .string(fixtureKind))
    XCTAssertEqual(
      vector["scope"],
      .string("SYNTHETIC_PROTOCOL_INTEROPERABILITY_ONLY_NOT_OS_AUTHENTICATION_OR_EXECUTION")
    )
    _ = try fixtureString(vector, key: "challenge_digest")
    _ = try fixtureString(vector, key: "review_digest")
    _ = try fixtureString(vector, key: "unsigned_receipt_digest")
    _ = try fixtureString(vector, key: "domain_separated_payload_sha256")
  }

  private func fixtureConfiguration(_ vector: [String: CanonicalJSONValue]) throws
    -> TrustedHostConfiguration
  {
    guard case .object(let fields)? = vector["configuration"],
      Set(fields.keys) == [
        "host_ref", "signing_key_ref", "maximum_challenge_bytes",
        "maximum_challenge_lifetime_seconds", "maximum_issued_at_skew_seconds",
        "maximum_subplans", "maximum_policy_items",
      ]
    else { throw FixtureError.invalid }
    return try TrustedHostConfiguration(
      hostReference: try string(fields, key: "host_ref"),
      signingKeyReference: try string(fields, key: "signing_key_ref"),
      machServiceName: "com.aptsonic.adhelm.approval.fixture",
      expectedClientCodeSigningRequirement: "identifier \"com.aptsonic.adhelm.client\"",
      expectedHostCodeSigningRequirement: "identifier \"com.aptsonic.adhelm.host\"",
      signingKeyApplicationTag: Data("com.aptsonic.adhelm.approval.fixture.v1".utf8),
      keyStorage: .keychainP256,
      limits: ApprovalHostLimits(
        maximumChallengeBytes: try positiveInteger(fields, key: "maximum_challenge_bytes"),
        maximumChallengeLifetime: TimeInterval(
          try positiveInteger(fields, key: "maximum_challenge_lifetime_seconds")
        ),
        maximumIssuedAtSkew: TimeInterval(
          try nonnegativeInteger(fields, key: "maximum_issued_at_skew_seconds")
        ),
        maximumSubplans: try positiveInteger(fields, key: "maximum_subplans"),
        maximumPolicyItems: try positiveInteger(fields, key: "maximum_policy_items")
      )
    )
  }

  private func fixtureMetadata() throws -> ApprovalHostBuildMetadataV1 {
    try ApprovalHostBuildMetadataV1(
      hostReference: "host:adhelm-macos-approval-v1",
      signingKeyReference: "approval-key:fixture-v1",
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
      signingKeyApplicationTag: Data("fixture".utf8),
      keyStorage: .keychainP256,
      provenanceService: "com.aptsonic.adhelm.provenance",
      provenanceAccount: "approval-host-v1"
    )
  }

  private func fixtureChallengeObject(
    _ vector: [String: CanonicalJSONValue]
  ) throws -> [String: CanonicalJSONValue] {
    guard case .object(let challenge)? = vector["challenge"] else { throw FixtureError.invalid }
    return challenge
  }

  private func fractionalChallengeIssuedAt(
    _ vector: [String: CanonicalJSONValue]
  ) throws -> Date {
    let challenge = try fixtureChallengeObject(vector)
    return try ClosedTimestamp.parse(try string(challenge, key: "issued_at"))
  }

  private func fractionalReceiptIssuedAt(
    _ vector: [String: CanonicalJSONValue]
  ) throws -> Date {
    guard case .object(let receipt)? = vector["unsigned_receipt"] else {
      throw FixtureError.invalid
    }
    let formatter = ISO8601DateFormatter()
    formatter.timeZone = TimeZone(secondsFromGMT: 0)
    formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    guard let value = formatter.date(from: try string(receipt, key: "issued_at")) else {
      throw FixtureError.invalid
    }
    return value
  }

  private func actionFields(
    _ challenge: [String: CanonicalJSONValue]
  ) throws -> [String: CanonicalJSONValue] {
    guard case .object(let action)? = challenge["action"] else { throw FixtureError.invalid }
    return action
  }

  private func redundantWholeSecondSpelling(
    _ fields: [String: CanonicalJSONValue],
    key: String
  ) throws -> CanonicalJSONValue {
    let raw = try string(fields, key: key)
    guard raw.hasSuffix("Z"), !raw.contains(".") else { throw FixtureError.invalid }
    return .string(String(raw.dropLast()) + ".000000Z")
  }

  private func actionIdentifier(_ action: [String: CanonicalJSONValue]) throws -> String {
    guard let workspace = action["workspace_binding"],
      let account = action["account_binding"],
      let execution = action["execution_binding"],
      let observation = action["observation_binding"],
      let postcondition = action["postcondition"],
      case .object(let plan)? = action["plan"]
    else { throw FixtureError.invalid }
    let expiresAt = try string(plan, key: "expires_at")
    guard expiresAt.hasSuffix("Z") else { throw FixtureError.invalid }
    let reference = CanonicalJSONValue.object([
      "kind": .string("meta-campaign-pause-action-v1"),
      "workspace": workspace,
      "account": account,
      "execution": execution,
      "observation": observation,
      "postcondition": postcondition,
      "expires_at": .string(String(expiresAt.dropLast()) + "+00:00"),
      "status": .string("DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED"),
      "current_state_status": .string("RECHECK_REQUIRED_BEFORE_EGRESS"),
      "configuration_readback": .string("REQUIRED_AFTER_PAUSE"),
      "recovery_semantics": .string("RECONCILE_THEN_NEW_EXACT_AUTHORIZATION_REQUIRED"),
      "execution_authority": .bool(false),
      "live_validated": .bool(false),
      "conditional_write_proof": .bool(false),
      "idempotency_proof": .bool(false),
    ])
    return CanonicalJSON.sha256Hex(try CanonicalJSON.encode(reference))
  }

  private func planBound(
    to actionID: String,
    from action: [String: CanonicalJSONValue]
  ) throws -> CanonicalJSONValue {
    guard case .object(var plan)? = action["plan"],
      case .array(var subplans)? = plan["subplans"],
      subplans.count == 1,
      case .object(var subplan) = subplans[0]
    else { throw FixtureError.invalid }
    let planID = "meta-campaign-pause-\(actionID)"
    plan["plan_id"] = .string(planID)
    subplan["subplan_id"] = .string(planID)
    subplans[0] = .object(subplan)
    plan["subplans"] = .array(subplans)
    return .object(plan)
  }

  private func encodedFixtureValue(
    _ vector: [String: CanonicalJSONValue],
    key: String
  ) throws -> Data {
    guard let value = vector[key] else { throw FixtureError.invalid }
    return try CanonicalJSON.encode(value)
  }

  private func fixtureString(
    _ vector: [String: CanonicalJSONValue],
    key: String
  ) throws -> String {
    try string(vector, key: key)
  }

  private func string(
    _ fields: [String: CanonicalJSONValue],
    key: String
  ) throws -> String {
    guard case .string(let value)? = fields[key] else { throw FixtureError.invalid }
    return value
  }

  private func positiveInteger(
    _ fields: [String: CanonicalJSONValue],
    key: String
  ) throws -> Int {
    let value = try integer(fields, key: key)
    guard value > 0, value <= Int64(Int.max) else { throw FixtureError.invalid }
    return Int(value)
  }

  private func nonnegativeInteger(
    _ fields: [String: CanonicalJSONValue],
    key: String
  ) throws -> Int {
    let value = try integer(fields, key: key)
    guard value >= 0, value <= Int64(Int.max) else { throw FixtureError.invalid }
    return Int(value)
  }

  private func integer(
    _ fields: [String: CanonicalJSONValue],
    key: String
  ) throws -> Int64 {
    guard case .integer(let value)? = fields[key] else { throw FixtureError.invalid }
    return value
  }
}

private enum FixtureError: Error {
  case invalid
}
