import Foundation

/// Closed native representation of the one M40 Meta campaign PAUSE action
/// family.  This accepts a complete canonical M40 action only and rebuilds
/// its action identifier, remote-state fingerprints, and single-subplan plan
/// before review or receipt signing.  It is not a generic Meta plan parser,
/// a Graph client, a provider-currentness check, or execution authority.
public struct ExactMetaCampaignPauseAction: Equatable, Sendable {
  public static let fixtureOnlyStatus = "DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED"
  public static let requiredScope = "ads_management"
  public static let host = "graph.facebook.com"
  public static let providerAPIVersion = "v26.0"
  public static let capabilityVersion = "meta-graph-v26-campaign-pause-contract-2026-09-06"
  public static let egressPurpose = "fixed Meta Graph v26 campaign pause"
  public static let recoverySemantics = "RECONCILE_THEN_NEW_EXACT_AUTHORIZATION_REQUIRED"

  public let actionID: String
  public let workspace: ExactMetaCampaignPauseWorkspaceBinding
  public let account: ExactMetaCampaignPauseAccountBinding
  public let execution: ExactMetaCampaignPauseExecutionBinding
  public let observation: ExactMetaCampaignPauseObservationBinding
  public let postcondition: ExactMetaCampaignPausePostcondition
  public let plan: ExactCompositePlan
  public let canonicalBytes: Data
  public let digest: String

  static func decode(
    _ value: CanonicalJSONValue,
    maximumSubplans: Int
  ) throws -> ExactMetaCampaignPauseAction {
    guard maximumSubplans >= 1 else { throw ApprovalHostError.malformedChallenge }
    let object = try ClosedObject(
      value,
      exactly: [
        "schema_version", "status", "action_id", "workspace_binding", "account_binding",
        "execution_binding", "observation_binding", "postcondition", "plan",
        "current_state_status", "configuration_readback", "recovery_semantics",
        "execution_authority", "live_validated", "conditional_write_proof", "idempotency_proof",
      ])
    guard try object.integer("schema_version") == 1,
      try object.string("status") == Self.fixtureOnlyStatus,
      metaDigest(try object.string("action_id")),
      try object.string("current_state_status") == "RECHECK_REQUIRED_BEFORE_EGRESS",
      try object.string("configuration_readback") == "REQUIRED_AFTER_PAUSE",
      try object.string("recovery_semantics") == Self.recoverySemantics,
      try object.bool("execution_authority") == false,
      try object.bool("live_validated") == false,
      try object.bool("conditional_write_proof") == false,
      try object.bool("idempotency_proof") == false
    else { throw ApprovalHostError.malformedChallenge }

    let workspaceValue = try object.value("workspace_binding")
    let accountValue = try object.value("account_binding")
    let executionValue = try object.value("execution_binding")
    let observationValue = try object.value("observation_binding")
    let postconditionValue = try object.value("postcondition")
    let planValue = try object.value("plan")

    let workspace = try ExactMetaCampaignPauseWorkspaceBinding.decode(workspaceValue)
    let account = try ExactMetaCampaignPauseAccountBinding.decode(accountValue)
    let execution = try ExactMetaCampaignPauseExecutionBinding.decode(executionValue)
    let observation = try ExactMetaCampaignPauseObservationBinding.decode(observationValue)
    let postcondition = try ExactMetaCampaignPausePostcondition.decode(postconditionValue)
    try validateMetaPlanTimestampSpellings(planValue)
    let plan = try ExactCompositePlan.decode(planValue, maximumSubplans: maximumSubplans)
    let actionID = try object.string("action_id")

    try validateOuterBindings(
      account: account,
      execution: execution,
      observation: observation,
      postcondition: postcondition,
      plan: plan
    )
    let expectedActionID = try expectedActionID(
      workspaceValue: workspaceValue,
      accountValue: accountValue,
      executionValue: executionValue,
      observationValue: observationValue,
      postconditionValue: postconditionValue,
      expiresAtText: plan.expiresAtText
    )
    guard actionID == expectedActionID else { throw ApprovalHostError.malformedChallenge }
    let expectedPlan = try expectedPlanValue(
      actionID: actionID,
      workspace: workspace,
      account: account,
      execution: execution,
      observationValue: observationValue,
      postconditionValue: postconditionValue,
      expiresAtText: plan.expiresAtText
    )
    guard planValue == expectedPlan else { throw ApprovalHostError.malformedChallenge }

    let canonicalBytes = try CanonicalJSON.encode(value)
    return ExactMetaCampaignPauseAction(
      actionID: actionID,
      workspace: workspace,
      account: account,
      execution: execution,
      observation: observation,
      postcondition: postcondition,
      plan: plan,
      canonicalBytes: canonicalBytes,
      digest: CanonicalJSON.sha256Hex(canonicalBytes)
    )
  }

  private static func validateOuterBindings(
    account: ExactMetaCampaignPauseAccountBinding,
    execution: ExactMetaCampaignPauseExecutionBinding,
    observation: ExactMetaCampaignPauseObservationBinding,
    postcondition: ExactMetaCampaignPausePostcondition,
    plan: ExactCompositePlan
  ) throws {
    let campaign = observation.campaign
    guard campaign.accountClientRef == account.nativeAccountID,
      postcondition.accountRef == account.accountRef,
      postcondition.accountClientRef == account.accountClientRef,
      postcondition.timezone == account.timezone,
      postcondition.currency == account.currency,
      postcondition.accountStatus == account.accountStatus,
      postcondition.campaignRef == campaign.campaignRef,
      postcondition.campaignClientRef == campaign.campaignClientRef,
      postcondition.campaignAccountClientRef == campaign.accountClientRef,
      execution.inspectedAt <= observation.readStartedAt,
      observation.readFinishedAt < execution.expiresAt,
      observation.readFinishedAt < execution.dataAccessExpiresAt,
      plan.expiresAt > observation.readFinishedAt,
      plan.expiresAt <= execution.expiresAt,
      plan.expiresAt <= execution.dataAccessExpiresAt,
      plan.expiresAt.timeIntervalSince(observation.readFinishedAt) <= 900,
      plan.subplans.count == 1
    else { throw ApprovalHostError.malformedChallenge }
  }

  private static func expectedActionID(
    workspaceValue: CanonicalJSONValue,
    accountValue: CanonicalJSONValue,
    executionValue: CanonicalJSONValue,
    observationValue: CanonicalJSONValue,
    postconditionValue: CanonicalJSONValue,
    expiresAtText: String
  ) throws -> String {
    let reference = CanonicalJSONValue.object([
      "kind": .string("meta-campaign-pause-action-v1"),
      "workspace": workspaceValue,
      "account": accountValue,
      "execution": executionValue,
      "observation": observationValue,
      "postcondition": postconditionValue,
      "expires_at": .string(metaPythonUTCISO8601(expiresAtText)),
      "status": .string(Self.fixtureOnlyStatus),
      "current_state_status": .string("RECHECK_REQUIRED_BEFORE_EGRESS"),
      "configuration_readback": .string("REQUIRED_AFTER_PAUSE"),
      "recovery_semantics": .string(Self.recoverySemantics),
      "execution_authority": .bool(false),
      "live_validated": .bool(false),
      "conditional_write_proof": .bool(false),
      "idempotency_proof": .bool(false),
    ])
    return CanonicalJSON.sha256Hex(try CanonicalJSON.encode(reference))
  }

  private static func expectedPlanValue(
    actionID: String,
    workspace: ExactMetaCampaignPauseWorkspaceBinding,
    account: ExactMetaCampaignPauseAccountBinding,
    execution: ExactMetaCampaignPauseExecutionBinding,
    observationValue: CanonicalJSONValue,
    postconditionValue: CanonicalJSONValue,
    expiresAtText: String
  ) throws -> CanonicalJSONValue {
    let campaignValue = try metaCampaignValue(observationValue)
    let beforeState = CanonicalJSONValue.object([
      "state_kind": .string("META_GRAPH_V26_ALLOWLISTED_REMOTE_STATE_V1"),
      "account": account.canonicalValue,
      "campaign": campaignValue,
    ])
    let afterState = CanonicalJSONValue.object([
      "state_kind": .string("META_GRAPH_V26_PAUSE_CONFIGURATION_POSTCONDITION_V1"),
      "postcondition": postconditionValue,
    ])
    let before = CanonicalJSONValue.object([
      "fingerprint": .string(CanonicalJSON.sha256Hex(try CanonicalJSON.encode(beforeState))),
      "revision": .null,
    ])
    let after = CanonicalJSONValue.object([
      "fingerprint": .string(CanonicalJSON.sha256Hex(try CanonicalJSON.encode(afterState))),
      "revision": .null,
    ])
    let planID = "meta-campaign-pause-\(actionID)"
    let accountIdentity = CanonicalJSONValue.object([
      "account_ref": .string(account.accountRef),
      "stable_account_id": .string(account.accountClientRef),
      "auth_profile_ref": .string(execution.profileRef),
      "principal_ref": .string(execution.principalRef),
      "client_application_ref": .string(execution.clientApplicationRef),
      "scopes": .array([.string(Self.requiredScope)]),
    ])
    let subplan = CanonicalJSONValue.object([
      "subplan_id": .string(planID),
      "provider": .string("meta_ads"),
      "operation": .string(ApprovalOperation.pause.rawValue),
      "account": accountIdentity,
      "provider_api_version": .string(Self.providerAPIVersion),
      "capability_version": .string(Self.capabilityVersion),
      "before": before,
      "after": after,
      "field_changes": .array([
        .object([
          "path": .string("campaign/configured-status"),
          "before": before,
          "after": after,
        ])
      ]),
      "max_spend": .object([
        "minor_units": .integer(0),
        "currency": .string(account.currency),
      ]),
      "egress": .array([
        .object([
          "host": .string(Self.host),
          "purpose": .string(Self.egressPurpose),
        ])
      ]),
      "recovery_actions": .array([.string(ApprovalOperation.pause.rawValue)]),
      "expires_at": .string(expiresAtText),
    ])
    return .object([
      "schema_version": .integer(1),
      "plan_id": .string(planID),
      "profile_ref": .string(workspace.workspaceProfileRef),
      "subplans": .array([subplan]),
      "expires_at": .string(expiresAtText),
      "stop_on_non_verified": .bool(true),
    ])
  }
}

public struct ExactMetaCampaignPauseWorkspaceBinding: Equatable, Sendable {
  public let workspaceProfileRef: String

  static func decode(_ value: CanonicalJSONValue) throws -> ExactMetaCampaignPauseWorkspaceBinding {
    let object = try ClosedObject(
      value, exactly: ["schema_version", "binding_kind", "workspace_profile_ref"])
    let reference = try object.boundedString("workspace_profile_ref", maximumBytes: 160)
    guard try object.integer("schema_version") == 1,
      try object.string("binding_kind") == "LOCAL_LEDGER_WORKSPACE",
      metaWorkspaceReference(reference)
    else { throw ApprovalHostError.malformedChallenge }
    return ExactMetaCampaignPauseWorkspaceBinding(workspaceProfileRef: reference)
  }
}

public struct ExactMetaCampaignPauseAccountBinding: Equatable, Sendable {
  public let accountRef: String
  public let accountClientRef: String
  public let timezone: String
  public let currency: String
  public let accountStatus: String
  let canonicalValue: CanonicalJSONValue

  var nativeAccountID: String { String(accountClientRef.dropFirst(4)) }

  static func decode(_ value: CanonicalJSONValue) throws -> ExactMetaCampaignPauseAccountBinding {
    let object = try ClosedObject(
      value,
      exactly: [
        "schema_version", "binding_kind", "account_ref", "account_client_ref", "timezone",
        "currency", "account_status", "provider_api_version",
      ])
    let accountRef = try object.boundedString("account_ref", maximumBytes: 160)
    let accountClientRef = try object.boundedString("account_client_ref", maximumBytes: 44)
    let timezone = try object.boundedString("timezone", maximumBytes: 80)
    let currency = try object.boundedString("currency", maximumBytes: 3)
    let accountStatus = try object.boundedString("account_status", maximumBytes: 10)
    guard try object.integer("schema_version") == 1,
      try object.string("binding_kind") == "META_GRAPH_V26_ACCOUNT_BASELINE",
      try object.string("provider_api_version") == ExactMetaCampaignPauseAction.providerAPIVersion,
      metaAccountClientReference(accountClientRef),
      accountRef == "meta_ads.account.\(accountClientRef)",
      metaTimezone(timezone),
      metaCurrency(currency),
      metaAccountStatus(accountStatus)
    else { throw ApprovalHostError.malformedChallenge }
    return ExactMetaCampaignPauseAccountBinding(
      accountRef: accountRef,
      accountClientRef: accountClientRef,
      timezone: timezone,
      currency: currency,
      accountStatus: accountStatus,
      canonicalValue: value
    )
  }
}

public struct ExactMetaCampaignPauseExecutionBinding: Equatable, Sendable {
  public let profileRef: String
  public let clientApplicationRef: String
  let principalRef: String
  let credentialBindingRef: String
  let configurationDigest: String
  let profileConfigurationDigest: String
  let inspectedAt: Date
  let inspectedAtText: String
  let expiresAt: Date
  let expiresAtText: String
  let dataAccessExpiresAt: Date
  let dataAccessExpiresAtText: String

  static func decode(_ value: CanonicalJSONValue) throws -> ExactMetaCampaignPauseExecutionBinding {
    let object = try ClosedObject(
      value,
      exactly: [
        "schema_version", "binding_kind", "profile_ref", "client_application_ref", "principal_ref",
        "required_scope", "credential_binding_ref", "configuration_digest",
        "profile_configuration_digest", "inspected_at", "expires_at", "data_access_expires_at",
        "local_association_status", "provider_write_permission",
      ])
    let profileRef = try object.boundedString("profile_ref", maximumBytes: 160)
    let clientApplicationRef = try object.boundedString("client_application_ref", maximumBytes: 40)
    let principalRef = try object.boundedString("principal_ref", maximumBytes: 128)
    let credentialBindingRef = try object.boundedString("credential_binding_ref", maximumBytes: 96)
    let configurationDigest = try object.boundedString("configuration_digest", maximumBytes: 64)
    let profileConfigurationDigest = try object.boundedString(
      "profile_configuration_digest", maximumBytes: 64)
    let inspectedAtText = try object.string("inspected_at")
    let expiresAtText = try object.string("expires_at")
    let dataAccessExpiresAtText = try object.string("data_access_expires_at")
    let inspectedAt = try metaCanonicalTimestamp(inspectedAtText)
    let expiresAt = try metaCanonicalTimestamp(expiresAtText)
    let dataAccessExpiresAt = try metaCanonicalTimestamp(dataAccessExpiresAtText)
    guard try object.integer("schema_version") == 1,
      try object.string("binding_kind") == "META_ADS_EDIT_LOCAL_TOKEN_ASSOCIATION",
      metaProfileReference(profileRef),
      metaNativeID(clientApplicationRef),
      metaPrincipalReference(principalRef),
      try object.string("required_scope") == ExactMetaCampaignPauseAction.requiredScope,
      metaCredentialBindingReference(credentialBindingRef),
      metaDigest(configurationDigest),
      metaDigest(profileConfigurationDigest),
      try object.string("local_association_status") == "LOCAL_INSPECTED_TOKEN_ASSOCIATION_ONLY",
      try object.string("provider_write_permission") == "NOT_PREPROVABLE",
      inspectedAt < expiresAt,
      inspectedAt < dataAccessExpiresAt
    else { throw ApprovalHostError.malformedChallenge }
    return ExactMetaCampaignPauseExecutionBinding(
      profileRef: profileRef,
      clientApplicationRef: clientApplicationRef,
      principalRef: principalRef,
      credentialBindingRef: credentialBindingRef,
      configurationDigest: configurationDigest,
      profileConfigurationDigest: profileConfigurationDigest,
      inspectedAt: inspectedAt,
      inspectedAtText: inspectedAtText,
      expiresAt: expiresAt,
      expiresAtText: expiresAtText,
      dataAccessExpiresAt: dataAccessExpiresAt,
      dataAccessExpiresAtText: dataAccessExpiresAtText
    )
  }
}

public struct ExactMetaCampaignPauseCampaignBaseline: Equatable, Sendable {
  public let campaignRef: String
  public let campaignClientRef: String
  public let accountClientRef: String
  public let effectiveStatus: String

  static func decode(_ value: CanonicalJSONValue) throws -> ExactMetaCampaignPauseCampaignBaseline {
    let object = try ClosedObject(
      value,
      exactly: [
        "schema_version", "binding_kind", "campaign_ref", "campaign_client_ref",
        "account_client_ref",
        "configured_status", "effective_status",
      ])
    let campaignRef = try object.boundedString("campaign_ref", maximumBytes: 160)
    let campaignClientRef = try object.boundedString("campaign_client_ref", maximumBytes: 40)
    let accountClientRef = try object.boundedString("account_client_ref", maximumBytes: 40)
    let effectiveStatus = try object.boundedString("effective_status", maximumBytes: 16)
    guard try object.integer("schema_version") == 1,
      try object.string("binding_kind") == "META_GRAPH_V26_CAMPAIGN_BASELINE",
      metaNativeID(campaignClientRef),
      metaNativeID(accountClientRef),
      campaignRef == "meta_ads.campaign.\(campaignClientRef)",
      try object.string("configured_status") == "ACTIVE",
      metaEffectiveStatus(effectiveStatus)
    else { throw ApprovalHostError.malformedChallenge }
    return ExactMetaCampaignPauseCampaignBaseline(
      campaignRef: campaignRef,
      campaignClientRef: campaignClientRef,
      accountClientRef: accountClientRef,
      effectiveStatus: effectiveStatus
    )
  }
}

public struct ExactMetaCampaignPauseObservationBinding: Equatable, Sendable {
  public let campaign: ExactMetaCampaignPauseCampaignBaseline
  public let readStartedAt: Date
  public let readStartedAtText: String
  public let readFinishedAt: Date
  public let readFinishedAtText: String

  static func decode(_ value: CanonicalJSONValue) throws -> ExactMetaCampaignPauseObservationBinding
  {
    let object = try ClosedObject(
      value,
      exactly: [
        "schema_version", "binding_kind", "campaign", "read_started_at", "read_finished_at",
        "consistency",
      ])
    let readStartedAtText = try object.string("read_started_at")
    let readFinishedAtText = try object.string("read_finished_at")
    let readStartedAt = try metaCanonicalTimestamp(readStartedAtText)
    let readFinishedAt = try metaCanonicalTimestamp(readFinishedAtText)
    guard try object.integer("schema_version") == 1,
      try object.string("binding_kind") == "META_GRAPH_V26_CAMPAIGN_STATE_OBSERVATION",
      try object.string("consistency") == "SEQUENTIAL_NONATOMIC",
      readFinishedAt >= readStartedAt,
      readFinishedAt.timeIntervalSince(readStartedAt) <= 300
    else { throw ApprovalHostError.malformedChallenge }
    return ExactMetaCampaignPauseObservationBinding(
      campaign: try ExactMetaCampaignPauseCampaignBaseline.decode(try object.value("campaign")),
      readStartedAt: readStartedAt,
      readStartedAtText: readStartedAtText,
      readFinishedAt: readFinishedAt,
      readFinishedAtText: readFinishedAtText
    )
  }
}

public struct ExactMetaCampaignPausePostcondition: Equatable, Sendable {
  public let accountRef: String
  public let accountClientRef: String
  public let timezone: String
  public let currency: String
  public let accountStatus: String
  public let campaignRef: String
  public let campaignClientRef: String
  public let campaignAccountClientRef: String

  static func decode(_ value: CanonicalJSONValue) throws -> ExactMetaCampaignPausePostcondition {
    let object = try ClosedObject(
      value,
      exactly: [
        "schema_version", "postcondition_kind", "account_ref", "account_client_ref", "timezone",
        "currency",
        "account_status", "provider_api_version", "campaign_ref", "campaign_client_ref",
        "campaign_account_client_ref", "configured_status", "effective_delivery",
      ])
    let accountRef = try object.boundedString("account_ref", maximumBytes: 160)
    let accountClientRef = try object.boundedString("account_client_ref", maximumBytes: 44)
    let timezone = try object.boundedString("timezone", maximumBytes: 80)
    let currency = try object.boundedString("currency", maximumBytes: 3)
    let accountStatus = try object.boundedString("account_status", maximumBytes: 10)
    let campaignRef = try object.boundedString("campaign_ref", maximumBytes: 160)
    let campaignClientRef = try object.boundedString("campaign_client_ref", maximumBytes: 40)
    let campaignAccountClientRef = try object.boundedString(
      "campaign_account_client_ref", maximumBytes: 40)
    guard try object.integer("schema_version") == 1,
      try object.string("postcondition_kind") == "CONFIGURATION_ONLY",
      try object.string("provider_api_version") == ExactMetaCampaignPauseAction.providerAPIVersion,
      metaAccountClientReference(accountClientRef),
      accountRef == "meta_ads.account.\(accountClientRef)",
      metaTimezone(timezone),
      metaCurrency(currency),
      metaAccountStatus(accountStatus),
      metaNativeID(campaignClientRef),
      metaNativeID(campaignAccountClientRef),
      campaignRef == "meta_ads.campaign.\(campaignClientRef)",
      try object.string("configured_status") == "PAUSED",
      try object.string("effective_delivery") == "NOT_PREDICTED"
    else { throw ApprovalHostError.malformedChallenge }
    return ExactMetaCampaignPausePostcondition(
      accountRef: accountRef,
      accountClientRef: accountClientRef,
      timezone: timezone,
      currency: currency,
      accountStatus: accountStatus,
      campaignRef: campaignRef,
      campaignClientRef: campaignClientRef,
      campaignAccountClientRef: campaignAccountClientRef
    )
  }
}

private func metaCampaignValue(_ observationValue: CanonicalJSONValue) throws -> CanonicalJSONValue
{
  let observation = try ClosedObject(
    observationValue,
    exactly: [
      "schema_version", "binding_kind", "campaign", "read_started_at", "read_finished_at",
      "consistency",
    ])
  return try observation.value("campaign")
}

private func validateMetaPlanTimestampSpellings(_ value: CanonicalJSONValue) throws {
  let plan = try ClosedObject(
    value,
    exactly: [
      "schema_version", "plan_id", "profile_ref", "subplans", "expires_at", "stop_on_non_verified",
    ])
  _ = try metaCanonicalTimestamp(try plan.string("expires_at"))
  let subplans = try plan.array("subplans", minimum: 1, maximum: 1)
  let subplan = try ClosedObject(
    subplans[0],
    exactly: [
      "subplan_id", "provider", "operation", "account", "provider_api_version",
      "capability_version",
      "before", "after", "field_changes", "max_spend", "egress", "recovery_actions", "expires_at",
    ])
  _ = try metaCanonicalTimestamp(try subplan.string("expires_at"))
}

/// M40 canonicalizes zero fractional seconds to the whole-second UTC spelling
/// before it hashes the action. This local rule keeps a Meta action from being
/// accepted with byte-different timestamp input that Python rejects. It does
/// not change the broader challenge timestamp grammar used by earlier kinds.
private func metaCanonicalTimestamp(_ raw: String) throws -> Date {
  guard !raw.hasSuffix(".000000Z") else { throw ApprovalHostError.malformedChallenge }
  return try ClosedTimestamp.parse(raw)
}

private func metaDigest(_ value: String) -> Bool {
  value.utf8.count == 64
    && value.utf8.allSatisfy { byte in
      (48...57).contains(byte) || (97...102).contains(byte)
    }
}

private func metaNativeID(_ value: String) -> Bool {
  let bytes = Array(value.utf8)
  guard (1...40).contains(bytes.count), let first = bytes.first, (49...57).contains(first) else {
    return false
  }
  return bytes.dropFirst().allSatisfy { (48...57).contains($0) }
}

private func metaAccountClientReference(_ value: String) -> Bool {
  value.hasPrefix("act_") && metaNativeID(String(value.dropFirst(4)))
}

private func metaAccountStatus(_ value: String) -> Bool {
  let bytes = Array(value.utf8)
  if bytes == [48] { return true }
  guard (1...10).contains(bytes.count), let first = bytes.first, (49...57).contains(first) else {
    return false
  }
  return bytes.dropFirst().allSatisfy { (48...57).contains($0) }
}

private func metaWorkspaceReference(_ value: String) -> Bool {
  let bytes = Array(value.utf8)
  guard (1...160).contains(bytes.count) else { return false }
  return bytes.allSatisfy { byte in
    (48...57).contains(byte) || (65...90).contains(byte) || (97...122).contains(byte)
      || [46, 95, 58, 45].contains(byte)
  }
}

private func metaProfileReference(_ value: String) -> Bool {
  let bytes = Array(value.utf8)
  guard (1...160).contains(bytes.count) else { return false }
  return bytes.allSatisfy { byte in
    (48...57).contains(byte) || (65...90).contains(byte) || (97...122).contains(byte)
      || [46, 95, 58, 47, 45].contains(byte)
  }
}

private func metaPrincipalReference(_ value: String) -> Bool {
  let prefix = "meta_token_principal_v1."
  return value.hasPrefix(prefix) && metaDigest(String(value.dropFirst(prefix.utf8.count)))
}

private func metaCredentialBindingReference(_ value: String) -> Bool {
  let prefix = "credential-binding:sha256:"
  return value.hasPrefix(prefix) && metaDigest(String(value.dropFirst(prefix.utf8.count)))
}

private func metaTimezone(_ value: String) -> Bool {
  let bytes = Array(value.utf8)
  guard (1...80).contains(bytes.count),
    !value.hasPrefix("/"),
    !value.contains("//"),
    value.split(separator: "/", omittingEmptySubsequences: false).allSatisfy({ component in
      !component.isEmpty && component != "." && component != ".."
    }),
    bytes.allSatisfy({ byte in
      (48...57).contains(byte) || (65...90).contains(byte) || (97...122).contains(byte)
        || [47, 95, 43, 45].contains(byte)
    }),
    TimeZone(identifier: value) != nil
  else { return false }
  return true
}

private func metaCurrency(_ value: String) -> Bool {
  [
    "AUD", "CAD", "CHF", "DKK", "EUR", "GBP", "HKD", "JPY", "MXN", "NOK", "NZD", "SEK", "SGD",
    "USD",
  ].contains(value)
}

private func metaEffectiveStatus(_ value: String) -> Bool {
  ["ACTIVE", "ARCHIVED", "DELETED", "IN_PROCESS", "PAUSED", "WITH_ISSUES"].contains(value)
}

private func metaPythonUTCISO8601(_ canonicalUTC: String) -> String {
  String(canonicalUTC.dropLast()) + "+00:00"
}
