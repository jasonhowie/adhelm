import Foundation
import XCTest

@testable import ApprovalHostCore

/// Independent closed-wire cases for the GA4 CREATE approval subject.
///
/// These tests only exercise canonical parsing, review, and ephemeral software
/// receipt material. They never initialize Security, LocalAuthentication, XPC,
/// a Keychain item, or a provider transport.
final class Ga4KeyEventCreateApprovalUltraReviewTests: XCTestCase {
  private let now = Date(timeIntervalSince1970: 1_893_456_060)

  func testRehashedOncePerSessionActionIsRejectedBeforeNativeReview() throws {
    let forged = try rehashedChallenge { action in
      var candidate = try object(try required(action, "candidate"))
      var compilation = try object(try required(candidate, "compilation"))
      var compiler = try object(try required(compilation, "compiler_candidate"))
      var request = try object(try required(compiler, "request"))
      request["countingMethod"] = .string("ONCE_PER_SESSION")
      compiler["request"] = .object(request)

      let compilerValue = CanonicalJSONValue.object(compiler)
      compilation["compiler_candidate"] = compilerValue
      compilation["compiler_candidate_digest"] = .string(try digest(compilerValue))

      let compilationValue = CanonicalJSONValue.object(compilation)
      candidate["compilation"] = compilationValue
      candidate["candidate_digest"] = .string(try digest(compilationValue))
      action["candidate"] = .object(candidate)

      var semantic = try object(try required(action, "semantic_state"))
      let compilerDigest = try string(try required(compilation, "compiler_candidate_digest"))
      semantic["candidate_digest"] = .string(compilerDigest)
      var preconditions = try object(try required(semantic, "preconditions"))
      preconditions["candidate_digest"] = .string(compilerDigest)
      semantic["preconditions"] = .object(preconditions)
      action["semantic_state"] = .object(semantic)
    }

    XCTAssertThrowsError(try ApprovalChallenge.decode(forged, now: now))
  }

  func testRehashedDivergentBooleanFieldBindingIsRejectedBeforeNativeReview() throws {
    let forged = try rehashedChallenge { action in
      var candidate = try object(try required(action, "candidate"))
      var compilation = try object(try required(candidate, "compilation"))
      var compiler = try object(try required(compilation, "compiler_candidate"))
      compiler["declared_field_names"] = .array([.string("other_boolean")])

      let compilerValue = CanonicalJSONValue.object(compiler)
      compilation["compiler_candidate"] = compilerValue
      compilation["compiler_candidate_digest"] = .string(try digest(compilerValue))

      let compilationValue = CanonicalJSONValue.object(compilation)
      candidate["compilation"] = compilationValue
      candidate["candidate_digest"] = .string(try digest(compilationValue))
      action["candidate"] = .object(candidate)

      var semantic = try object(try required(action, "semantic_state"))
      let compilerDigest = try string(try required(compilation, "compiler_candidate_digest"))
      semantic["candidate_digest"] = .string(compilerDigest)
      var preconditions = try object(try required(semantic, "preconditions"))
      preconditions["candidate_digest"] = .string(compilerDigest)
      semantic["preconditions"] = .object(preconditions)
      action["semantic_state"] = .object(semantic)
    }

    XCTAssertThrowsError(try ApprovalChallenge.decode(forged, now: now))
  }

  func testNativeDecoderRejectsRawFalseAliasesNullDriftAndMissingNestedActionData() throws {
    let falseAlias = try changedChallenge { action in
      action["execution_authority"] = .integer(0)
    }
    XCTAssertThrowsError(try ApprovalChallenge.decode(falseAlias, now: now))

    let nullDrift = try changedChallenge { action in
      var candidate = try object(try required(action, "candidate"))
      var compilation = try object(try required(candidate, "compilation"))
      var compiler = try object(try required(compilation, "compiler_candidate"))
      var request = try object(try required(compiler, "request"))
      request["defaultValue"] = .integer(0)
      compiler["request"] = .object(request)
      compilation["compiler_candidate"] = .object(compiler)
      candidate["compilation"] = .object(compilation)
      action["candidate"] = .object(candidate)
    }
    XCTAssertThrowsError(try ApprovalChallenge.decode(nullDrift, now: now))

    let missing = try changedChallenge { action in
      action.removeValue(forKey: "native_request_binding")
    }
    XCTAssertThrowsError(try ApprovalChallenge.decode(missing, now: now))
  }

  func testTrustedLocalReviewShowsSafeIdentityAndChangeWithoutRequestBody() throws {
    let challenge = try ApprovalChallenge.decode(
      try fixture("ga4-key-event-create-challenge-v1"), now: now
    )
    let review = try DeterministicApprovalReview.make(for: challenge)
    let text = try XCTUnwrap(String(data: review.facts, encoding: .utf8))

    for visible in [
      "\"profile_ref\":\"ga4.edit.profile\"",
      "\"property_client_ref\":\"123\"",
      "\"web_stream_client_ref\":\"456\"",
      "\"event_name\":\"qualified_request\"",
      "\"counting_method\":\"ONCE_PER_EVENT\"",
      "\"default_value\":\"NONE\"",
    ] {
      XCTAssertTrue(text.contains(visible), "missing trusted local review fact: \(visible)")
    }
    for hidden in [
      "properties/123/keyEvents",
      "/v1alpha/properties/123/keyEvents",
      "principal.fixture",
      "client.fixture",
      "customer-defined qualified request",
      "fixture-edit-token",
    ] {
      XCTAssertFalse(text.contains(hidden), "private action material leaked: \(hidden)")
    }
    XCTAssertTrue(review.text.contains("Configured OAuth profile: ga4.edit.profile."))
    XCTAssertTrue(review.text.contains("GA4 property 123, web stream 456."))
    XCTAssertTrue(
      review.text.contains(
        "Key event: qualified_request; count each qualifying occurrence once per event; no default value."
      ))
  }

  func testNativeGa4ReceiptUsesExactKindMillisecondTimeAndDigestOnlyActionBinding() throws {
    let challenge = try ApprovalChallenge.decode(
      try fixture("ga4-key-event-create-challenge-v1"), now: now
    )
    let review = try DeterministicApprovalReview.make(for: challenge)
    let configuration = try TrustedHostConfiguration(
      hostReference: "host:adhelm-macos-approval-v1",
      signingKeyReference: "approval-key:fixture-v1",
      machServiceName: "com.aptsonic.adhelm.approval.fixture",
      expectedClientCodeSigningRequirement: "identifier \"com.aptsonic.adhelm.client\"",
      expectedHostCodeSigningRequirement: "identifier \"com.aptsonic.adhelm.host\"",
      signingKeyApplicationTag: Data("com.aptsonic.adhelm.approval.fixture.v1".utf8),
      keyStorage: .keychainP256
    )
    let receipt = try ApprovalReceiptFactory.makeUnsigned(
      challenge: challenge,
      review: review,
      configuration: configuration,
      now: now,
      receiptIdentifier: "approval-receipt:" + String(repeating: "A", count: 43)
    )
    let text = try XCTUnwrap(String(data: receipt.canonicalUnsignedReceipt, encoding: .utf8))

    XCTAssertTrue(text.contains("\"receipt_kind\":\"GA4_KEY_EVENT_CREATE_PLAN\""))
    XCTAssertTrue(text.contains("\"issued_at\":\"2030-01-01T00:01:00.000Z\""))
    XCTAssertTrue(text.contains("\"action_digest\":"))
    XCTAssertTrue(text.contains("\"plan_digest\":"))
    XCTAssertTrue(text.contains("\"subplan_digests\":["))
    for privateValue in [
      "qualified_request",
      "properties/123/keyEvents",
      "principal.fixture",
      "client.fixture",
      "fixture-edit-token",
    ] {
      XCTAssertFalse(
        text.contains(privateValue), "private receipt material leaked: \(privateValue)"
      )
    }
  }

  private func rehashedChallenge(
    mutate: (inout [String: CanonicalJSONValue]) throws -> Void
  ) throws -> Data {
    let source = try CanonicalJSON.parseCanonical(try fixture("ga4-key-event-create-challenge-v1"))
    var challenge = try object(source)
    var action = try object(try required(challenge, "action"))
    try mutate(&action)
    action["plan"] = try rebuiltPlan(action)
    challenge["action"] = .object(action)
    return try CanonicalJSON.encode(.object(challenge))
  }

  private func changedChallenge(
    mutate: (inout [String: CanonicalJSONValue]) throws -> Void
  ) throws -> Data {
    let source = try CanonicalJSON.parseCanonical(try fixture("ga4-key-event-create-challenge-v1"))
    var challenge = try object(source)
    var action = try object(try required(challenge, "action"))
    try mutate(&action)
    challenge["action"] = .object(action)
    return try CanonicalJSON.encode(.object(challenge))
  }

  private func rebuiltPlan(_ action: [String: CanonicalJSONValue]) throws -> CanonicalJSONValue {
    let workspace = try object(try required(action, "workspace_binding"))
    let candidate = try object(try required(action, "candidate"))
    let compilation = try object(try required(candidate, "compilation"))
    let compiler = try object(try required(compilation, "compiler_candidate"))
    let identity = try object(try required(compiler, "identity"))
    let selection = try required(action, "selection_binding")
    let observation = try required(action, "observation_binding")
    let execution = try object(try required(action, "execution_binding"))
    let target = try object(try required(execution, "target"))
    let nativeRequest = try required(action, "native_request_binding")
    let semantic = try object(try required(action, "semantic_state"))
    let originalPlan = try object(try required(action, "plan"))
    let expiresAt = try string(try required(originalPlan, "expires_at"))
    guard expiresAt.hasSuffix("Z") else { throw ReviewTestError.malformed }

    let reference = CanonicalJSONValue.object([
      "kind": .string("ga4-key-event-create-action-v1"),
      "workspace": .object(workspace),
      "candidate": .object(candidate),
      "selection": selection,
      "observation": observation,
      "execution": .object(execution),
      "native_request": nativeRequest,
      "semantic_state": .object(semantic),
      "expires_at": .string(String(expiresAt.dropLast()) + "+00:00"),
    ])
    let planID = "ga4-key-event-create-\(try digest(reference))"
    let account = CanonicalJSONValue.object([
      "account_ref": try required(identity, "property_ref"),
      "stable_account_id": try required(identity, "property_client_ref"),
      "auth_profile_ref": try required(execution, "auth_profile_ref"),
      "principal_ref": try required(execution, "principal_ref"),
      "client_application_ref": try required(execution, "client_application_ref"),
      "scopes": .array([.string("https://www.googleapis.com/auth/analytics.edit")]),
    ])
    let before = try required(semantic, "before")
    let after = try required(semantic, "after")
    let subplan = CanonicalJSONValue.object([
      "subplan_id": .string(planID),
      "provider": .string("ga4_admin"),
      "operation": .string("CREATE"),
      "account": account,
      "provider_api_version": .string("v1alpha"),
      "capability_version": try required(compiler, "capability_version"),
      "before": before,
      "after": after,
      "field_changes": .array([
        .object([
          "path": .string("key-event/semantic-collection"),
          "before": before,
          "after": after,
        ])
      ]),
      "max_spend": .object([
        "minor_units": .integer(0),
        "currency": try required(target, "expected_currency"),
      ]),
      "egress": .array([
        .object([
          "host": .string("analyticsadmin.googleapis.com"),
          "purpose": .string("fixed GA4 Admin key-event create"),
        ])
      ]),
      "recovery_actions": .array([]),
      "recovery_disposition": .string("RECONCILE_EXPORT_STOP_NO_REMOTE_COMPENSATION"),
      "expires_at": .string(expiresAt),
    ])
    return .object([
      "schema_version": .integer(1),
      "plan_id": .string(planID),
      "profile_ref": try required(workspace, "workspace_profile_ref"),
      "subplans": .array([subplan]),
      "expires_at": .string(expiresAt),
      "stop_on_non_verified": .bool(true),
    ])
  }

  private func fixture(_ name: String) throws -> Data {
    let url = try XCTUnwrap(
      Bundle.module.url(forResource: name, withExtension: "json", subdirectory: "Fixtures")
    )
    return try Data(contentsOf: url)
  }

  private func digest(_ value: CanonicalJSONValue) throws -> String {
    CanonicalJSON.sha256Hex(try CanonicalJSON.encode(value))
  }

  private func object(_ value: CanonicalJSONValue) throws -> [String: CanonicalJSONValue] {
    guard case .object(let result) = value else { throw ReviewTestError.malformed }
    return result
  }

  private func required(
    _ object: [String: CanonicalJSONValue], _ key: String
  ) throws -> CanonicalJSONValue {
    guard let value = object[key] else { throw ReviewTestError.malformed }
    return value
  }

  private func string(_ value: CanonicalJSONValue) throws -> String {
    guard case .string(let result) = value else { throw ReviewTestError.malformed }
    return result
  }
}

private enum ReviewTestError: Error {
  case malformed
}
