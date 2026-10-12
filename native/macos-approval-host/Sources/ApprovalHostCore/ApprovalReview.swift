import Foundation

/// The locally displayed review derives only from the closed, independently
/// parsed challenge.  It has no caller-supplied prose and never substitutes a
/// file, URL, command argument, or model assertion for an explicit local action.
public struct DeterministicApprovalReview: Equatable, Sendable {
    public let text: String
    public let facts: Data
    public let digest: String

    public static func make(for challenge: ApprovalChallenge) throws -> DeterministicApprovalReview {
        let facts = reviewFacts(for: challenge)
        let encodedFacts = try CanonicalJSON.encode(facts)
        let text = try render(challenge: challenge, facts: facts)
        guard text.lengthOfBytes(using: .utf8) <= 16_384 else {
            throw ApprovalHostError.malformedChallenge
        }
        return DeterministicApprovalReview(
            text: text,
            facts: encodedFacts,
            digest: CanonicalJSON.sha256Hex(encodedFacts)
        )
    }

    private static func reviewFacts(for challenge: ApprovalChallenge) -> CanonicalJSONValue {
        var root: [String: CanonicalJSONValue] = [
            "approval_kind": .string(challenge.kind.rawValue),
            "challenge_digest": .string(challenge.digest),
            "challenge_expires_at": .string(challenge.expiresAtText),
            "challenge_nonce": .string(base64URLEncoded(challenge.nonce)),
        ]
        switch challenge.subject {
        case let .openAIPausePlan(grantorRef, action):
            root["grantor_ref"] = .string(grantorRef)
            root["openai_pause"] = openAIPauseFacts(action)
        case let .ga4KeyEventCreatePlan(grantorRef, action):
            root["grantor_ref"] = .string(grantorRef)
            root["ga4_key_event_create"] = ga4KeyEventCreateFacts(action)
        case let .metaCampaignPausePlan(grantorRef, action):
            root["grantor_ref"] = .string(grantorRef)
            root["meta_campaign_pause"] = metaCampaignPauseFacts(action)
        case let .delegationPolicyInstall(policy):
            root["policy"] = policyFacts(policy)
        case let .delegationPolicyRevoke(policy, _, revokedAtText):
            root["policy"] = policyFacts(policy)
            root["revoked_at"] = .string(revokedAtText)
        }
        return .object(root)
    }

    private static func openAIPauseFacts(_ action: ExactOpenAIPauseAction) -> CanonicalJSONValue {
        let binding = action.binding
        let campaign = binding.campaign
        let subplan = action.plan.subplans[0]
        return .object([
            "account": .object([
                "account_client_ref": .string(binding.accountClientRef),
                "account_currency": .string(binding.accountCurrency),
                "account_ref": .string(binding.accountRef),
                "account_status": .string(binding.accountStatus),
                "account_timezone": .string(binding.accountTimezone),
                "credential_binding_status": .string("BOUND_NOT_DISPLAYED"),
                "profile_ref": .string(binding.profileRef),
            ]),
            "campaign": .object([
                "bidding_type": .string(campaign.biddingType),
                "campaign_client_ref": .string(campaign.campaignClientRef),
                "campaign_ref": .string(campaign.campaignRef),
                "end_at": campaign.endAtText.map(CanonicalJSONValue.string) ?? .null,
                "lifetime_spend_limit_micros": .string(campaign.lifetimeSpendLimitMicros),
                "start_at": .string(campaign.startAtText),
                "status_before": .string(campaign.status),
            ]),
            "fixture_status": .string(ExactOpenAIPauseAction.fixtureOnlyStatus),
            "plan": .object([
                "capability_version": .string(subplan.capabilityVersion),
                "egress": .array(subplan.egress.map {
                    .object(["host": .string($0.host), "purpose": .string($0.purpose)])
                }),
                "expires_at": .string(action.plan.expiresAtText),
                "field_changes": .array(subplan.fieldChanges.map {
                    .object([
                        "after_state_digest": .string($0.after.digest),
                        "before_state_digest": .string($0.before.digest),
                        "path": .string($0.path),
                    ])
                }),
                "maximum_incremental_spend": .object([
                    "currency": .string(subplan.maximumSpend.currency),
                    "minor_units": .integer(subplan.maximumSpend.minorUnits),
                ]),
                "plan_digest": .string(action.plan.digest),
                "plan_id": .string(action.plan.planID),
                "profile_ref": .string(action.plan.profileRef),
                "provider_api_version": .string(subplan.providerAPIVersion),
                "recovery_actions": .array(subplan.recoveryActions.map { .string($0.rawValue) }),
                "subplan_digest": .string(subplan.digest),
                "subplan_id": .string(subplan.subplanID),
            ]),
            "residual_delivery_or_billing": .string("NOT_PROVEN_STOPPED"),
            "transition": .object([
                "operation": .string(ApprovalOperation.pause.rawValue),
                "status_after": .string("paused"),
                "status_before": .string("active"),
            ]),
            "workspace_binding": action.workspaceBinding.map {
                .object([
                    "binding_kind": .string("LOCAL_LEDGER_WORKSPACE"),
                    "workspace_profile_ref": .string($0.workspaceProfileRef),
                ])
            } ?? .null,
        ])
    }

    private static func policyFacts(_ policy: ExactDelegationPolicy) -> CanonicalJSONValue {
        .object([
            "account_stable_ids": .array(policy.accountStableIDs.map(CanonicalJSONValue.string)),
            "allowed_egress_hosts": .array(policy.allowedEgressHosts.map(CanonicalJSONValue.string)),
            "allowed_operations": .array(policy.allowedOperations.map { .string($0.rawValue) }),
            "authorized_agent_or_host_ref": .string(policy.authorizedAgentOrHostRef),
            "configuration_host_ref": .string(policy.configurationHostRef),
            "configuration_interaction_ref": .string(policy.configurationInteractionRef),
            "cumulative_change_bounds": .array(policy.cumulativeChangeBounds.map(moneyLimitFacts)),
            "effective_from": .string(policy.effectiveFromText),
            "expires_at": .string(policy.expiresAtText),
            "grantor_ref": .string(policy.grantorRef),
            "maximum_change_per_operation": .array(policy.maximumChangePerOperation.map(moneyLimitFacts)),
            "policy_digest": .string(policy.digest),
            "policy_id": .string(policy.policyID),
            "policy_version": .integer(policy.policyVersion),
            "source_authorization_ref": .string(policy.sourceAuthorizationRef),
        ])
    }

    private static func ga4KeyEventCreateFacts(
        _ action: ExactGa4KeyEventCreateAction
    ) -> CanonicalJSONValue {
        let semantic = action.semanticState
        let execution = action.execution
        let target = execution.target
        return .object([
            "action_digest": .string(action.digest),
            "fixture_status": .string(ExactGa4KeyEventCreateAction.fixtureOnlyStatus),
            "source_bindings": .object([
                "candidate_record_digest": .string(action.selection.candidateRecordDigest),
                "contract_record_digest": .string(action.selection.contractRecordDigest),
                "selection_checkpoint_digest": .string(action.selection.checkpointDigest),
                "observation_record_digest": .string(action.observation.observationRecordDigest),
                "observation_checkpoint_digest": .string(action.observation.checkpointDigest),
                "inventory_digest": .string(action.observation.inventoryDigest),
                "configuration_digest": .string(execution.configurationDigest),
                "profile_configuration_digest": .string(execution.profileConfigurationDigest),
            ]),
            "oauth_association": .object([
                "provider": .string("ga4_admin"),
                "purpose": .string("EDIT"),
                "credential_source": .string("OS_OAUTH_GOOGLE"),
                "scopes": .array([.string(ExactGa4KeyEventCreateAction.editScope)]),
                "local_association_status": .string("LOCAL_SEALED_OAUTH_ASSOCIATION_ONLY"),
                "provider_scope_status": .string("NOT_PROVEN_BY_RESOURCE_READ"),
                "provider_create_permission": .string("NOT_PREPROVABLE"),
                "profile_ref": .string(execution.authProfileRef),
            ]),
            "target": .object([
                "target_digest": .string(target.digest),
                "admin_api_version": .string("v1alpha"),
                "expected_currency": .string(target.expectedCurrency),
                "expected_timezone": .string(target.expectedTimezone),
                "property_client_ref": .string(target.identity.propertyClientRef),
                "required_scope": .string(ExactGa4KeyEventCreateAction.editScope),
                "web_stream_client_ref": .string(target.identity.webStreamClientRef),
            ]),
            "semantic_transition": .object([
                "counting_method": .string("ONCE_PER_EVENT"),
                "default_value": .string("NONE"),
                "event_name": .string(action.candidate.eventName),
                "before_member_count": .integer(semantic.beforeMemberCount),
                "after_member_count": .integer(semantic.afterMemberCount),
                "before_state_digest": .string(semantic.beforeFingerprint),
                "after_state_digest": .string(semantic.afterFingerprint),
                "inventory_scope": .string(semantic.inventoryScope),
                "keyed_content_integrity": .string(semantic.keyedContentIntegrity),
                "duplicate_status": .string(semantic.duplicateStatus),
                "capacity_status": .string(semantic.capacityStatus),
                "documented_key_event_cap": .integer(semantic.documentedKeyEventCap),
                "remaining_documented_slots": .integer(semantic.remainingDocumentedSlots),
                "current_provider_state": .string(semantic.currentProviderState),
                "reservation_status": .string(semantic.reservationStatus),
                "permission_status": .string(semantic.permissionStatus),
            ]),
            "native_request": .object([
                "host": .string(ExactGa4KeyEventCreateAction.host),
                "method": .string("POST"),
                "api_version": .string("v1alpha"),
                "target_digest": .string(action.nativeRequest.targetDigest),
                "body_digest": .string(action.nativeRequest.bodyDigest),
                "body": .string("BOUND_NOT_DISPLAYED"),
            ]),
            "plan": .object([
                "plan_digest": .string(action.plan.digest),
                "subplan_digest": .string(action.plan.subplanDigest),
                "operation": .string("CREATE"),
                "provider": .string("ga4_admin"),
                "maximum_incremental_spend": .object([
                    "currency": .string(action.plan.maximumCurrency),
                    "minor_units": .integer(0),
                ]),
                "egress": .array([
                    .object([
                        "host": .string(ExactGa4KeyEventCreateAction.host),
                        "purpose": .string(ExactGa4KeyEventCreateAction.egressPurpose),
                    ]),
                ]),
                "recovery_actions": .array([]),
                "recovery_disposition": .string(ExactGa4KeyEventCreateAction.recoveryDisposition),
                "expires_at": .string(action.plan.expiresAtText),
            ]),
            "known_limits": .object([
                "current_state_status": .string("RECHECK_REQUIRED_BEFORE_EGRESS"),
                "configuration_readback": .string("REQUIRED_AFTER_CREATE"),
                "provider_create_permission": .string("NOT_PREPROVABLE"),
                "semantic_source_mapping": .string("NOT_VERIFIED"),
                "browser_network_verification": .string("NOT_EVALUATED"),
                "realtime_verification": .string("NOT_EVALUATED"),
                "processed_report_verification": .string("NOT_EVALUATED"),
                "native_conversion_mapping": .string("NONE_CREATED"),
                "execution_authority": .bool(false),
            ]),
            "workspace_binding": .object([
                "binding_kind": .string("LOCAL_LEDGER_WORKSPACE"),
                "workspace_profile_ref": .string(action.workspace.workspaceProfileRef),
            ]),
        ])
    }

    private static func metaCampaignPauseFacts(_ action: ExactMetaCampaignPauseAction) -> CanonicalJSONValue {
        let account = action.account
        let execution = action.execution
        let campaign = action.observation.campaign
        let subplan = action.plan.subplans[0]
        return .object([
            "action_digest": .string(action.digest),
            "fixture_status": .string(ExactMetaCampaignPauseAction.fixtureOnlyStatus),
            "workspace_binding": .object([
                "binding_kind": .string("LOCAL_LEDGER_WORKSPACE"),
                "workspace_profile_ref": .string(action.workspace.workspaceProfileRef),
            ]),
            "local_association": .object([
                "profile_ref": .string(execution.profileRef),
                "client_application_ref": .string(execution.clientApplicationRef),
                "required_scope": .string(ExactMetaCampaignPauseAction.requiredScope),
                "credential_binding_status": .string("BOUND_NOT_DISPLAYED"),
                "local_association_status": .string("LOCAL_INSPECTED_TOKEN_ASSOCIATION_ONLY"),
                "provider_write_permission": .string("NOT_PREPROVABLE"),
            ]),
            "account": .object([
                "account_ref": .string(account.accountRef),
                "account_client_ref": .string(account.accountClientRef),
                "currency": .string(account.currency),
                "timezone": .string(account.timezone),
                "account_status": .string(account.accountStatus),
                "provider_api_version": .string(ExactMetaCampaignPauseAction.providerAPIVersion),
            ]),
            "campaign_baseline": .object([
                "campaign_ref": .string(campaign.campaignRef),
                "campaign_client_ref": .string(campaign.campaignClientRef),
                "account_client_ref": .string(campaign.accountClientRef),
                "configured_status": .string("ACTIVE"),
                "effective_status": .string(campaign.effectiveStatus),
            ]),
            "observation": .object([
                "read_started_at": .string(action.observation.readStartedAtText),
                "read_finished_at": .string(action.observation.readFinishedAtText),
                "consistency": .string("SEQUENTIAL_NONATOMIC"),
            ]),
            "transition": .object([
                "operation": .string(ApprovalOperation.pause.rawValue),
                "configured_status_before": .string("ACTIVE"),
                "configured_status_after": .string("PAUSED"),
                "postcondition_kind": .string("CONFIGURATION_ONLY"),
                "effective_delivery": .string("NOT_PREDICTED"),
            ]),
            "plan": .object([
                "plan_id": .string(action.plan.planID),
                "plan_digest": .string(action.plan.digest),
                "profile_ref": .string(action.plan.profileRef),
                "subplan_id": .string(subplan.subplanID),
                "subplan_digest": .string(subplan.digest),
                "provider": .string(subplan.provider),
                "provider_api_version": .string(subplan.providerAPIVersion),
                "capability_version": .string(subplan.capabilityVersion),
                "maximum_incremental_spend": .object([
                    "currency": .string(subplan.maximumSpend.currency),
                    "minor_units": .integer(subplan.maximumSpend.minorUnits),
                ]),
                "egress": .array(subplan.egress.map {
                    .object(["host": .string($0.host), "purpose": .string($0.purpose)])
                }),
                "recovery_actions": .array(subplan.recoveryActions.map { .string($0.rawValue) }),
                "recovery_semantics": .string(ExactMetaCampaignPauseAction.recoverySemantics),
                "expires_at": .string(action.plan.expiresAtText),
            ]),
            "known_limits": .object([
                "current_state_status": .string("RECHECK_REQUIRED_BEFORE_EGRESS"),
                "configuration_readback": .string("REQUIRED_AFTER_PAUSE"),
                "conditional_write_proof": .bool(false),
                "idempotency_proof": .bool(false),
                "execution_authority": .bool(false),
                "live_validated": .bool(false),
            ]),
        ])
    }

    private static func moneyLimitFacts(_ limit: ExactMoneyLimit) -> CanonicalJSONValue {
        .object([
            "currency": .string(limit.currency),
            "maximum_minor_units": .integer(limit.maximumMinorUnits),
        ])
    }

    private static func render(
        challenge: ApprovalChallenge,
        facts: CanonicalJSONValue
    ) throws -> String {
        let summary: [String]
        switch challenge.subject {
        case let .openAIPausePlan(grantorRef, action):
            summary = openAIPauseSummary(grantorRef: grantorRef, action: action, challenge: challenge)
        case let .ga4KeyEventCreatePlan(grantorRef, action):
            summary = ga4KeyEventCreateSummary(grantorRef: grantorRef, action: action, challenge: challenge)
        case let .metaCampaignPausePlan(grantorRef, action):
            summary = metaCampaignPauseSummary(grantorRef: grantorRef, action: action, challenge: challenge)
        case let .delegationPolicyInstall(policy):
            summary = policySummary(policy: policy, operation: "Install delegation policy", challenge: challenge)
        case let .delegationPolicyRevoke(policy, _, revokedAtText):
            summary = policySummary(
                policy: policy,
                operation: "Revoke delegation policy at \(revokedAtText)",
                challenge: challenge
            )
        }
        guard let detail = String(data: try CanonicalJSON.encode(facts), encoding: .utf8) else {
            throw ApprovalHostError.malformedChallenge
        }
        return (summary + [
            "",
            "Technical detail — canonical facts bound by the review digest",
            detail,
        ]).joined(separator: "\n")
    }

    private static func openAIPauseSummary(
        grantorRef: String,
        action: ExactOpenAIPauseAction,
        challenge: ApprovalChallenge
    ) -> [String] {
        let binding = action.binding
        let campaign = binding.campaign
        let subplan = action.plan.subplans[0]
        let scheduleEnd = campaign.endAtText ?? "not supplied"
        let egressHosts = subplan.egress.map(\.host).joined(separator: ", ")
        return [
            "OpenAI Ads campaign pause — fixture-only contract",
            "Grantor: \(grantorRef)",
            "Account: \(binding.accountRef) (native: \(binding.accountClientRef))",
            "Profile: \(binding.profileRef); credential binding is present but not displayed",
            "Declared account currency and time zone: \(binding.accountCurrency), \(binding.accountTimezone)",
            "Campaign: \(campaign.campaignRef) (native: \(campaign.campaignClientRef))",
            "Requested transition: Active → Paused",
            "Bid strategy retained: \(campaign.biddingType)",
            "Existing lifetime spend limit: \(campaign.lifetimeSpendLimitMicros) micros (\(binding.accountCurrency))",
            "Incremental spend cap for this pause: \(subplan.maximumSpend.minorUnits) minor units (\(subplan.maximumSpend.currency))",
            "Schedule: start \(campaign.startAtText); end \(scheduleEnd)",
            "Residual delivery or billing: NOT_PROVEN_STOPPED",
            "Provider egress after authorization: \(egressHosts)",
            "Plan expires: \(action.plan.expiresAtText); approval challenge expires: \(challenge.expiresAtText)",
            "This review and a future receipt do not apply a provider operation.",
        ]
    }

    private static func policySummary(
        policy: ExactDelegationPolicy,
        operation: String,
        challenge: ApprovalChallenge
    ) -> [String] {
        let accounts = policy.accountStableIDs.joined(separator: ", ")
        let operations = policy.allowedOperations.map(\.rawValue).joined(separator: ", ")
        let hosts = policy.allowedEgressHosts.joined(separator: ", ")
        return [
            operation,
            "Policy: \(policy.policyID), version \(policy.policyVersion)",
            "Grantor: \(policy.grantorRef)",
            "Authorized agent or host: \(policy.authorizedAgentOrHostRef)",
            "Accounts: \(accounts)",
            "Allowed operations: \(operations)",
            "Per-operation caps: \(moneyLimits(policy.maximumChangePerOperation))",
            "Cumulative caps: \(moneyLimits(policy.cumulativeChangeBounds))",
            "Allowed egress hosts: \(hosts)",
            "Effective window: \(policy.effectiveFromText) through \(policy.expiresAtText)",
            "Approval challenge expires: \(challenge.expiresAtText)",
            "This review does not install, revoke, or grant provider authority by itself.",
        ]
    }

    private static func ga4KeyEventCreateSummary(
        grantorRef: String,
        action: ExactGa4KeyEventCreateAction,
        challenge: ApprovalChallenge
    ) -> [String] {
        let semantic = action.semanticState
        return [
            "GA4 key-event CREATE — development-fixture-only contract",
            "Grantor: \(grantorRef)",
            "Requested operation: Create one GA4 key event from the exact reviewed local candidate.",
            "Configured OAuth profile: \(action.execution.authProfileRef).",
            "Target: GA4 property \(action.execution.target.identity.propertyClientRef), web stream \(action.execution.target.identity.webStreamClientRef).",
            "Key event: \(action.candidate.eventName); count each qualifying occurrence once per event; no default value.",
            "Configured OAuth association: GA4 Admin EDIT with analytics.edit; local association only, provider CREATE permission is not preproven.",
            "Observed semantic collection: \(semantic.beforeMemberCount) → \(semantic.afterMemberCount) key events; documented Standard cap \(semantic.documentedKeyEventCap), remaining slots \(semantic.remainingDocumentedSlots).",
            "Duplicate check: \(semantic.duplicateStatus). Inventory keyed-content integrity: \(semantic.keyedContentIntegrity).",
            "Incremental spend cap: 0 minor units (\(action.plan.maximumCurrency)).",
            "Provider request after later authorization: POST analyticsadmin.googleapis.com v1alpha; body is bound by digest and not displayed.",
            "Recovery after any ambiguous provider attempt: reconcile, export evidence, and stop; no remote compensation is authorized.",
            "Plan expires: \(action.plan.expiresAtText); approval challenge expires: \(challenge.expiresAtText).",
            "This review and any receipt bind a local fixture-only plan. They do not prove current provider state, create permission, or execute a provider operation.",
        ]
    }

    private static func metaCampaignPauseSummary(
        grantorRef: String,
        action: ExactMetaCampaignPauseAction,
        challenge: ApprovalChallenge
    ) -> [String] {
        let account = action.account
        let campaign = action.observation.campaign
        let subplan = action.plan.subplans[0]
        return [
            "Meta Ads campaign PAUSE — development-fixture-only contract",
            "Grantor: \(grantorRef)",
            "Configured Meta EDIT profile: \(action.execution.profileRef); credential binding is present but not displayed.",
            "Account: \(account.accountRef) (native: \(account.accountClientRef)); declared currency and time zone: \(account.currency), \(account.timezone).",
            "Account status is retained as provider value \(account.accountStatus); write eligibility is not preproven.",
            "Campaign: \(campaign.campaignRef) (native: \(campaign.campaignClientRef)); baseline configured status ACTIVE, observed effective status \(campaign.effectiveStatus).",
            "Requested transition: configured status ACTIVE → PAUSED. Effective delivery remains NOT_PREDICTED.",
            "Observation interval: \(action.observation.readStartedAtText) through \(action.observation.readFinishedAtText), SEQUENTIAL_NONATOMIC.",
            "Incremental spend cap: \(subplan.maximumSpend.minorUnits) minor units (\(subplan.maximumSpend.currency)).",
            "Provider egress after later authorization: graph.facebook.com v26.0; configuration readback is required after pause.",
            "Recovery after an ambiguous provider attempt: reconcile, then obtain new exact authorization; no blind retry is authorized.",
            "Plan expires: \(action.plan.expiresAtText); approval challenge expires: \(challenge.expiresAtText).",
            "This review and any receipt bind a local fixture-only plan. They do not prove current provider state, effective delivery, zero residual spend, or execute a provider operation.",
        ]
    }

    private static func moneyLimits(_ limits: [ExactMoneyLimit]) -> String {
        limits.map { "\($0.maximumMinorUnits) minor units (\($0.currency))" }.joined(separator: "; ")
    }
}
