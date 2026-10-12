import Foundation

public enum ApprovalKind: String, CaseIterable, Equatable, Sendable {
    /// The only direct-action contract currently rendered by this host.  A
    /// generic CompositePlan has opaque before/after fingerprints and cannot
    /// provide a meaningful semantic review, so it is deliberately unsupported.
    case openAIPausePlan = "OPENAI_PAUSE_PLAN"
    /// The one separately reconstructed GA4 CREATE action.  This is not a
    /// generic plan parser; the subject carries its complete closed source
    /// action and the decoder independently rebuilds the exact plan.
    case ga4KeyEventCreatePlan = "GA4_KEY_EVENT_CREATE_PLAN"
    /// One independently reconstructed M40 Meta configured-status pause
    /// action. It remains a review-and-receipt subject only; the M32 bridge
    /// allowlist deliberately excludes it.
    case metaCampaignPausePlan = "META_CAMPAIGN_PAUSE_PLAN"
    case delegationPolicyInstall = "DELEGATION_POLICY_INSTALL"
    case delegationPolicyRevoke = "DELEGATION_POLICY_REVOKE"
}

public enum ApprovalOperation: String, CaseIterable, Equatable, Sendable {
    case create = "CREATE"
    case update = "UPDATE"
    case associate = "ASSOCIATE"
    case replace = "REPLACE"
    case activate = "ACTIVATE"
    case pause = "PAUSE"
    case enable = "ENABLE"
    case archive = "ARCHIVE"
    case delete = "DELETE"
    case publish = "PUBLISH"
    case restore = "RESTORE"
}

public struct ApprovalHostLimits: Equatable, Sendable {
    public let maximumChallengeBytes: Int
    public let maximumChallengeLifetime: TimeInterval
    public let maximumIssuedAtSkew: TimeInterval
    public let maximumSubplans: Int
    public let maximumPolicyItems: Int

    public init(
        maximumChallengeBytes: Int = 65_536,
        maximumChallengeLifetime: TimeInterval = 300,
        maximumIssuedAtSkew: TimeInterval = 60,
        maximumSubplans: Int = 6,
        maximumPolicyItems: Int = 64
    ) {
        self.maximumChallengeBytes = maximumChallengeBytes
        self.maximumChallengeLifetime = maximumChallengeLifetime
        self.maximumIssuedAtSkew = maximumIssuedAtSkew
        self.maximumSubplans = maximumSubplans
        self.maximumPolicyItems = maximumPolicyItems
    }
}

public struct ApprovalChallenge: Equatable, Sendable {
    public let kind: ApprovalKind
    public let nonce: Data
    public let issuedAt: Date
    public let issuedAtText: String
    public let expiresAt: Date
    public let expiresAtText: String
    public let subject: ApprovalSubject
    public let canonicalBytes: Data
    public let digest: String

    public var activeRequestKey: String { digest }
    /// A caller can vary harmless challenge bytes while reusing its nonce.  The
    /// nonce, rather than the full request digest, is therefore the one-shot
    /// process-local key.
    public var replayKey: String { CanonicalJSON.sha256Hex(nonce) }

    public static func decode(
        _ data: Data,
        now: Date,
        limits: ApprovalHostLimits = ApprovalHostLimits()
    ) throws -> ApprovalChallenge {
        var jsonLimits = CanonicalJSONLimits.approvalChallenge
        jsonLimits = CanonicalJSONLimits(
            maximumBytes: limits.maximumChallengeBytes,
            maximumDepth: jsonLimits.maximumDepth,
            maximumContainerItems: jsonLimits.maximumContainerItems,
            maximumStringBytes: jsonLimits.maximumStringBytes
        )
        let root = try CanonicalJSON.parseCanonical(data, limits: jsonLimits)
        let object = try ClosedObject(root, fields: [
            "schema_version", "kind", "nonce", "issued_at", "expires_at", "action", "grantor_ref",
            "policy", "revoked_at",
        ], allowingOneOf: [
            ["schema_version", "kind", "nonce", "issued_at", "expires_at", "action", "grantor_ref"],
            ["schema_version", "kind", "nonce", "issued_at", "expires_at", "policy"],
            ["schema_version", "kind", "nonce", "issued_at", "expires_at", "policy", "revoked_at"],
        ])
        guard try object.integer("schema_version") == 1,
              let kind = ApprovalKind(rawValue: try object.string("kind"))
        else { throw ApprovalHostError.malformedChallenge }

        let nonce = try decodeBase64URL(try object.string("nonce"), exactByteCount: 32)
        let issuedAt = try ClosedTimestamp.parse(try object.string("issued_at"))
        let expiresAt = try ClosedTimestamp.parse(try object.string("expires_at"))
        guard expiresAt > issuedAt,
              expiresAt.timeIntervalSince(issuedAt) <= limits.maximumChallengeLifetime
        else { throw ApprovalHostError.malformedChallenge }
        let issuedAtSkew = issuedAt.timeIntervalSince(now)
        guard issuedAtSkew <= limits.maximumIssuedAtSkew else {
            throw ApprovalHostError.challengeNotYetValid
        }
        guard issuedAtSkew >= -limits.maximumIssuedAtSkew else {
            throw ApprovalHostError.malformedChallenge
        }
        guard now < expiresAt else { throw ApprovalHostError.challengeExpired }

        let subject: ApprovalSubject
        switch kind {
        case .openAIPausePlan:
            guard object.hasExactly(["schema_version", "kind", "nonce", "issued_at", "expires_at", "action", "grantor_ref"])
            else { throw ApprovalHostError.malformedChallenge }
            let grantorRef = try object.boundedString("grantor_ref", maximumBytes: 256)
            let action = try ExactOpenAIPauseAction.decode(
                try object.value("action"),
                maximumSubplans: limits.maximumSubplans
            )
            guard expiresAt <= action.plan.expiresAt else { throw ApprovalHostError.malformedChallenge }
            subject = .openAIPausePlan(grantorRef: grantorRef, action: action)
        case .ga4KeyEventCreatePlan:
            guard object.hasExactly(["schema_version", "kind", "nonce", "issued_at", "expires_at", "action", "grantor_ref"])
            else { throw ApprovalHostError.malformedChallenge }
            let grantorRef = try object.boundedString("grantor_ref", maximumBytes: 256)
            let action = try ExactGa4KeyEventCreateAction.decode(
                try object.value("action"), maximumSubplans: limits.maximumSubplans
            )
            guard expiresAt <= action.plan.expiresAt else { throw ApprovalHostError.malformedChallenge }
            subject = .ga4KeyEventCreatePlan(grantorRef: grantorRef, action: action)
        case .metaCampaignPausePlan:
            guard object.hasExactly(["schema_version", "kind", "nonce", "issued_at", "expires_at", "action", "grantor_ref"])
            else { throw ApprovalHostError.malformedChallenge }
            let grantorRef = try object.boundedString("grantor_ref", maximumBytes: 256)
            let action = try ExactMetaCampaignPauseAction.decode(
                try object.value("action"), maximumSubplans: limits.maximumSubplans
            )
            guard expiresAt <= action.plan.expiresAt else { throw ApprovalHostError.malformedChallenge }
            subject = .metaCampaignPausePlan(grantorRef: grantorRef, action: action)
        case .delegationPolicyInstall:
            guard object.hasExactly(["schema_version", "kind", "nonce", "issued_at", "expires_at", "policy"])
            else { throw ApprovalHostError.malformedChallenge }
            let policy = try ExactDelegationPolicy.decode(
                try object.value("policy"),
                maximumItems: limits.maximumPolicyItems
            )
            guard policy.revokedAt == nil else { throw ApprovalHostError.malformedChallenge }
            subject = .delegationPolicyInstall(policy: policy)
        case .delegationPolicyRevoke:
            guard object.hasExactly(["schema_version", "kind", "nonce", "issued_at", "expires_at", "policy", "revoked_at"])
            else { throw ApprovalHostError.malformedChallenge }
            let policy = try ExactDelegationPolicy.decode(
                try object.value("policy"),
                maximumItems: limits.maximumPolicyItems
            )
            guard policy.revokedAt == nil else { throw ApprovalHostError.malformedChallenge }
            let revokedAtText = try object.string("revoked_at")
            let revokedAt = try ClosedTimestamp.parse(revokedAtText)
            guard revokedAt >= issuedAt, revokedAt <= expiresAt else {
                throw ApprovalHostError.malformedChallenge
            }
            subject = .delegationPolicyRevoke(policy: policy, revokedAt: revokedAt, revokedAtText: revokedAtText)
        }

        return ApprovalChallenge(
            kind: kind,
            nonce: nonce,
            issuedAt: issuedAt,
            issuedAtText: try object.string("issued_at"),
            expiresAt: expiresAt,
            expiresAtText: try object.string("expires_at"),
            subject: subject,
            canonicalBytes: data,
            digest: CanonicalJSON.sha256Hex(data)
        )
    }
}

public enum ApprovalSubject: Equatable, Sendable {
    case openAIPausePlan(grantorRef: String, action: ExactOpenAIPauseAction)
    case ga4KeyEventCreatePlan(grantorRef: String, action: ExactGa4KeyEventCreateAction)
    case metaCampaignPausePlan(grantorRef: String, action: ExactMetaCampaignPauseAction)
    case delegationPolicyInstall(policy: ExactDelegationPolicy)
    case delegationPolicyRevoke(policy: ExactDelegationPolicy, revokedAt: Date, revokedAtText: String)

    public var grantorRef: String {
        switch self {
        case let .openAIPausePlan(grantorRef, _): grantorRef
        case let .ga4KeyEventCreatePlan(grantorRef, _): grantorRef
        case let .metaCampaignPausePlan(grantorRef, _): grantorRef
        case let .delegationPolicyInstall(policy), let .delegationPolicyRevoke(policy, _, _): policy.grantorRef
        }
    }
}

public struct ExactCompositePlan: Equatable, Sendable {
    public let planID: String
    public let profileRef: String
    public let subplans: [ExactSubplan]
    public let expiresAt: Date
    public let expiresAtText: String
    public let canonicalBytes: Data
    public let digest: String

    static func decode(_ value: CanonicalJSONValue, maximumSubplans: Int) throws -> ExactCompositePlan {
        let object = try ClosedObject(value, exactly: [
            "schema_version", "plan_id", "profile_ref", "subplans", "expires_at", "stop_on_non_verified",
        ])
        guard try object.integer("schema_version") == 1,
              try object.bool("stop_on_non_verified")
        else { throw ApprovalHostError.malformedChallenge }
        let planID = try object.boundedString("plan_id", maximumBytes: 160)
        let profileRef = try object.boundedString("profile_ref", maximumBytes: 160)
        let expiresAt = try ClosedTimestamp.parse(try object.string("expires_at"))
        let subplanValues = try object.array("subplans", minimum: 1, maximum: maximumSubplans)
        let subplans = try subplanValues.map(ExactSubplan.decode)
        let subplanIDs = subplans.map(\.subplanID)
        let providers = subplans.map(\.provider)
        guard Set(subplanIDs).count == subplanIDs.count,
              Set(providers).count == providers.count,
              subplans.allSatisfy({ $0.expiresAt <= expiresAt })
        else { throw ApprovalHostError.malformedChallenge }
        let canonicalBytes = try CanonicalJSON.encode(value)
        return ExactCompositePlan(
            planID: planID,
            profileRef: profileRef,
            subplans: subplans,
            expiresAt: expiresAt,
            expiresAtText: try object.string("expires_at"),
            canonicalBytes: canonicalBytes,
            digest: CanonicalJSON.sha256Hex(canonicalBytes)
        )
    }
}

/// A semantic direct-approval payload for the one currently closed action
/// family.  It carries the full source baseline and the exact generated safety
/// plan.  The decoder rebuilds the plan itself; matching opaque fingerprints
/// alone is never treated as a user-reviewable pause transition.
public struct ExactOpenAIPauseAction: Equatable, Sendable {
    public static let fixtureOnlyStatus = "DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED"
    public let binding: ExactOpenAIPauseBinding
    public let workspaceBinding: ExactOpenAIPauseWorkspaceBinding?
    public let plan: ExactCompositePlan
    public let canonicalBytes: Data
    public let digest: String

    static func decode(_ value: CanonicalJSONValue, maximumSubplans: Int) throws -> ExactOpenAIPauseAction {
        let object = try ClosedObject(value, fields: ["schema_version", "status", "binding", "workspace_binding", "plan"], allowingOneOf: [
            ["schema_version", "status", "binding", "plan"],
            ["schema_version", "status", "binding", "workspace_binding", "plan"],
        ])
        guard try object.integer("schema_version") == 1,
              try object.string("status") == fixtureOnlyStatus
        else { throw ApprovalHostError.malformedChallenge }
        let binding = try ExactOpenAIPauseBinding.decode(try object.value("binding"))
        let workspaceBinding: ExactOpenAIPauseWorkspaceBinding?
        if object.hasExactly(["schema_version", "status", "binding", "plan"]) {
            workspaceBinding = nil
        } else if object.hasExactly(["schema_version", "status", "binding", "workspace_binding", "plan"]) {
            workspaceBinding = try ExactOpenAIPauseWorkspaceBinding.decode(try object.value("workspace_binding"))
        } else {
            throw ApprovalHostError.malformedChallenge
        }
        let planValue = try object.value("plan")
        let plan = try ExactCompositePlan.decode(planValue, maximumSubplans: maximumSubplans)
        guard plan.subplans.count == 1 else { throw ApprovalHostError.malformedChallenge }
        let expectedPlan = try expectedPlanValue(
            binding: binding,
            workspaceBinding: workspaceBinding,
            expiresAtText: plan.expiresAtText
        )
        guard planValue == expectedPlan else { throw ApprovalHostError.malformedChallenge }
        let canonicalBytes = try CanonicalJSON.encode(value)
        return ExactOpenAIPauseAction(
            binding: binding,
            workspaceBinding: workspaceBinding,
            plan: plan,
            canonicalBytes: canonicalBytes,
            digest: CanonicalJSON.sha256Hex(canonicalBytes)
        )
    }

    private static func expectedPlanValue(
        binding: ExactOpenAIPauseBinding,
        workspaceBinding: ExactOpenAIPauseWorkspaceBinding?,
        expiresAtText: String
    ) throws -> CanonicalJSONValue {
        let before = try expectedStateReference(binding: binding, paused: false)
        let after = try expectedStateReference(binding: binding, paused: true)
        let actionRef = CanonicalJSON.sha256Hex(try CanonicalJSON.encode(.object([
            "account": .string(binding.accountRef),
            "campaign": .string(binding.campaign.campaignClientRef),
        ]))).prefix(32)
        let planID = "openai-pause-\(actionRef)"
        let subplan = CanonicalJSONValue.object([
            "account": .object([
                "account_ref": .string(binding.accountRef),
                "auth_profile_ref": .string(binding.profileRef),
                "credential_binding_ref": .string(binding.credentialBindingRef),
                "identity_kind": .string("OPENAI_ADS_ACCOUNT_KEY"),
                "provider_scope_status": .string("NOT_DOCUMENTED"),
                "stable_account_id": .string(binding.accountClientRef),
            ]),
            "after": after,
            "before": before,
            "capability_version": .string("openai-ads-pause-development-contract-2026-09-05"),
            "egress": .array([
                .object([
                    "host": .string("api.ads.openai.com"),
                    "purpose": .string("fixed OpenAI campaign pause"),
                ]),
            ]),
            "expires_at": .string(expiresAtText),
            "field_changes": .array([
                .object(["after": before, "before": before, "path": .string("campaign/identity")]),
                .object(["after": after, "before": before, "path": .string("campaign/status")]),
            ]),
            "max_spend": .object([
                "currency": .string(binding.accountCurrency),
                "minor_units": .integer(0),
            ]),
            "operation": .string(ApprovalOperation.pause.rawValue),
            "provider": .string("openai_ads"),
            "provider_api_version": .string("v1"),
            "recovery_actions": .array([.string(ApprovalOperation.pause.rawValue)]),
            "subplan_id": .string(planID),
        ])
        return .object([
            "expires_at": .string(expiresAtText),
            "plan_id": .string(planID),
            "profile_ref": .string(workspaceBinding?.workspaceProfileRef ?? binding.profileRef),
            "schema_version": .integer(1),
            "stop_on_non_verified": .bool(true),
            "subplans": .array([subplan]),
        ])
    }

    private static func expectedStateReference(
        binding: ExactOpenAIPauseBinding,
        paused: Bool
    ) throws -> CanonicalJSONValue {
        let campaign = binding.campaign
        let state = CanonicalJSONValue.object([
            "account_client_ref": .string(binding.accountClientRef),
            "account_currency": .string(binding.accountCurrency),
            "account_ref": .string(binding.accountRef),
            "account_status": .string(binding.accountStatus),
            "account_timezone": .string(binding.accountTimezone),
            "bidding_type": .string(campaign.biddingType),
            "campaign_client_ref": .string(campaign.campaignClientRef),
            "campaign_ref": .string(campaign.campaignRef),
            "end_at": campaign.endAtText.map { .string(pythonUTCISO8601($0)) } ?? .null,
            "lifetime_spend_limit_micros": .string(campaign.lifetimeSpendLimitMicros),
            "start_at": .string(pythonUTCISO8601(campaign.startAtText)),
            "status": .string(paused ? "paused" : campaign.status),
        ])
        return .object([
            "fingerprint": .string(CanonicalJSON.sha256Hex(try CanonicalJSON.encode(state))),
            "revision": .null,
        ])
    }
}

public struct ExactOpenAIPauseBinding: Equatable, Sendable {
    public let accountRef: String
    public let accountClientRef: String
    public let profileRef: String
    public let credentialBindingRef: String
    public let accountTimezone: String
    public let accountCurrency: String
    public let accountStatus: String
    public let campaign: ExactOpenAIPauseCampaign

    static func decode(_ value: CanonicalJSONValue) throws -> ExactOpenAIPauseBinding {
        let object = try ClosedObject(value, exactly: [
            "account_ref", "account_client_ref", "profile_ref", "credential_binding_ref", "account_timezone",
            "account_currency", "account_status", "campaign",
        ])
        let accountRef = try object.boundedString("account_ref", maximumBytes: 160)
        let accountClientRef = try object.boundedString("account_client_ref", maximumBytes: 120)
        let profileRef = try object.boundedString("profile_ref", maximumBytes: 160)
        guard isOpenAIPauseLocalReference(accountRef),
              isOpenAIPauseNativeReference(accountClientRef),
              isOpenAIPauseLocalReference(profileRef)
        else { throw ApprovalHostError.malformedChallenge }
        let credentialBindingRef = try object.boundedString("credential_binding_ref", maximumBytes: 96)
        guard credentialBindingRef.range(of: "^credential-binding:sha256:[0-9a-f]{64}$", options: .regularExpression) != nil else {
            throw ApprovalHostError.malformedChallenge
        }
        let accountTimezone = try object.boundedString("account_timezone", maximumBytes: 80)
        guard isSupportedOpenAIPauseTimezone(accountTimezone) else {
            throw ApprovalHostError.malformedChallenge
        }
        let accountCurrency = try object.string("account_currency")
        guard accountCurrency.range(of: "^[A-Z]{3}$", options: .regularExpression) != nil,
              try object.string("account_status") == "active"
        else { throw ApprovalHostError.malformedChallenge }
        return ExactOpenAIPauseBinding(
            accountRef: accountRef,
            accountClientRef: accountClientRef,
            profileRef: profileRef,
            credentialBindingRef: credentialBindingRef,
            accountTimezone: accountTimezone,
            accountCurrency: accountCurrency,
            accountStatus: "active",
            campaign: try ExactOpenAIPauseCampaign.decode(try object.value("campaign"))
        )
    }
}

public struct ExactOpenAIPauseCampaign: Equatable, Sendable {
    public let campaignRef: String
    public let campaignClientRef: String
    public let status: String
    public let biddingType: String
    public let lifetimeSpendLimitMicros: String
    public let startAt: Date
    public let startAtText: String
    public let endAt: Date?
    public let endAtText: String?

    static func decode(_ value: CanonicalJSONValue) throws -> ExactOpenAIPauseCampaign {
        let object = try ClosedObject(value, exactly: [
            "campaign_ref", "campaign_client_ref", "status", "bidding_type", "lifetime_spend_limit_micros", "start_at", "end_at",
        ])
        let startAtText = try object.string("start_at")
        let startAt = try ClosedTimestamp.parse(startAtText)
        let endAt: Date?
        let endAtText: String?
        switch try object.value("end_at") {
        case .null:
            endAt = nil
            endAtText = nil
        case let .string(raw):
            endAt = try ClosedTimestamp.parse(raw)
            endAtText = raw
        default:
            throw ApprovalHostError.malformedChallenge
        }
        guard endAt.map({ $0 >= startAt }) ?? true else { throw ApprovalHostError.malformedChallenge }
        let lifetimeSpendLimitMicros = try object.string("lifetime_spend_limit_micros")
        guard lifetimeSpendLimitMicros.range(of: "^[1-9][0-9]{0,18}$", options: .regularExpression) != nil,
              try object.string("status") == "active"
        else { throw ApprovalHostError.malformedChallenge }
        let campaignRef = try object.boundedString("campaign_ref", maximumBytes: 160)
        let campaignClientRef = try object.boundedString("campaign_client_ref", maximumBytes: 120)
        let biddingType = try object.boundedString("bidding_type", maximumBytes: 80)
        guard isOpenAIPauseLocalReference(campaignRef),
              isOpenAIPauseNativeReference(campaignClientRef),
              ["impressions", "clicks", "conversions"].contains(biddingType)
        else { throw ApprovalHostError.malformedChallenge }
        return ExactOpenAIPauseCampaign(
            campaignRef: campaignRef,
            campaignClientRef: campaignClientRef,
            status: "active",
            biddingType: biddingType,
            lifetimeSpendLimitMicros: lifetimeSpendLimitMicros,
            startAt: startAt,
            startAtText: startAtText,
            endAt: endAt,
            endAtText: endAtText
        )
    }
}

public struct ExactOpenAIPauseWorkspaceBinding: Equatable, Sendable {
    public let workspaceProfileRef: String

    static func decode(_ value: CanonicalJSONValue) throws -> ExactOpenAIPauseWorkspaceBinding {
        let object = try ClosedObject(value, exactly: ["schema_version", "binding_kind", "workspace_profile_ref"])
        let workspaceProfileRef = try object.boundedString("workspace_profile_ref", maximumBytes: 160)
        guard try object.integer("schema_version") == 1,
              try object.string("binding_kind") == "LOCAL_LEDGER_WORKSPACE",
              !workspaceProfileRef.contains("/"), !workspaceProfileRef.contains("\\")
        else { throw ApprovalHostError.malformedChallenge }
        return ExactOpenAIPauseWorkspaceBinding(workspaceProfileRef: workspaceProfileRef)
    }
}

private func pythonUTCISO8601(_ canonicalUTC: String) -> String {
    String(canonicalUTC.dropLast()) + "+00:00"
}

/// These match the installed OpenAI pause-review source boundary.  They run
/// while the closed action is decoded, before any deterministic UI is built.
private func isOpenAIPauseLocalReference(_ value: String) -> Bool {
    value.range(of: "^[A-Za-z0-9_.:/-]{1,160}$", options: .regularExpression) != nil
}

private func isOpenAIPauseNativeReference(_ value: String) -> Bool {
    value.range(of: "^[A-Za-z0-9_-]{1,120}$", options: .regularExpression) != nil
}

/// Foundation accepts fixed-offset aliases such as `GMT-05:00` that the
/// Python `ZoneInfo` review boundary does not. This foundation deliberately
/// supports only the checked cross-runtime subset needed by its vectors until
/// an installed product supplies an independently maintained IANA allowlist.
private func isSupportedOpenAIPauseTimezone(_ value: String) -> Bool {
    ["UTC", "America/Chicago"].contains(value)
}

public struct ExactSubplan: Equatable, Sendable {
    public let subplanID: String
    public let provider: String
    public let operation: ApprovalOperation
    public let account: ExactAccountIdentity
    public let providerAPIVersion: String
    public let capabilityVersion: String
    public let before: ExactStateReference
    public let after: ExactStateReference
    public let fieldChanges: [ExactFieldChange]
    public let maximumSpend: ExactMoney
    public let egress: [ExactEgressTarget]
    public let recoveryActions: [ApprovalOperation]
    public let expiresAt: Date
    public let expiresAtText: String
    public let digest: String

    static func decode(_ value: CanonicalJSONValue) throws -> ExactSubplan {
        let object = try ClosedObject(value, exactly: [
            "subplan_id", "provider", "operation", "account", "provider_api_version", "capability_version",
            "before", "after", "field_changes", "max_spend", "egress", "recovery_actions", "expires_at",
        ])
        let subplanID = try object.boundedString("subplan_id", maximumBytes: 160)
        let provider = try object.boundedString("provider", maximumBytes: 80)
        guard let operation = ApprovalOperation(rawValue: try object.string("operation")) else {
            throw ApprovalHostError.malformedChallenge
        }
        let account = try ExactAccountIdentity.decode(try object.value("account"), provider: provider)
        let providerAPIVersion = try object.boundedString("provider_api_version", maximumBytes: 80)
        let capabilityVersion = try object.boundedString("capability_version", maximumBytes: 80)
        let before = try ExactStateReference.decode(try object.value("before"))
        let after = try ExactStateReference.decode(try object.value("after"))
        // The Python model permits a longer sequence, but this host must render
        // every change deterministically.  Larger plans are unsupported before
        // any review or authentication rather than being silently truncated.
        let fieldChanges = try object.array("field_changes", minimum: 0, maximum: 32)
        let parsedFieldChanges = try fieldChanges.map(ExactFieldChange.decode)
        let maximumSpend = try ExactMoney.decode(try object.value("max_spend"))
        let egress = try object.array("egress", minimum: 1, maximum: 32).map(ExactEgressTarget.decode)
        let recovery = try object.array("recovery_actions", minimum: 1, maximum: 11)
        let recoveryActions = try recovery.map { item -> ApprovalOperation in
            guard case let .string(raw) = item, let parsed = ApprovalOperation(rawValue: raw) else {
                throw ApprovalHostError.malformedChallenge
            }
            return parsed
        }
        let expiresAt = try ClosedTimestamp.parse(try object.string("expires_at"))
        let canonicalBytes = try CanonicalJSON.encode(value)
        return ExactSubplan(
            subplanID: subplanID,
            provider: provider,
            operation: operation,
            account: account,
            providerAPIVersion: providerAPIVersion,
            capabilityVersion: capabilityVersion,
            before: before,
            after: after,
            fieldChanges: parsedFieldChanges,
            maximumSpend: maximumSpend,
            egress: egress,
            recoveryActions: recoveryActions,
            expiresAt: expiresAt,
            expiresAtText: try object.string("expires_at"),
            digest: CanonicalJSON.sha256Hex(canonicalBytes)
        )
    }
}

public enum ExactAccountIdentity: Equatable, Sendable {
    case scoped(
        accountRef: String,
        stableAccountID: String,
        authProfileRef: String,
        principalRef: String,
        clientApplicationRef: String,
        scopes: [String]
    )
    case openAIAccountKey(
        accountRef: String,
        stableAccountID: String,
        authProfileRef: String,
        credentialBindingRef: String
    )

    public var stableAccountID: String {
        switch self {
        case let .scoped(_, stableAccountID, _, _, _, _), let .openAIAccountKey(_, stableAccountID, _, _):
            stableAccountID
        }
    }

    public var accountRef: String {
        switch self {
        case let .scoped(accountRef, _, _, _, _, _), let .openAIAccountKey(accountRef, _, _, _):
            accountRef
        }
    }

    public var authProfileRef: String {
        switch self {
        case let .scoped(_, _, authProfileRef, _, _, _), let .openAIAccountKey(_, _, authProfileRef, _):
            authProfileRef
        }
    }

    static func decode(_ value: CanonicalJSONValue, provider: String) throws -> ExactAccountIdentity {
        let object = try ClosedObject(value, fields: [
            "account_ref", "stable_account_id", "auth_profile_ref", "principal_ref", "client_application_ref",
            "scopes", "identity_kind", "provider_scope_status", "credential_binding_ref",
        ], allowingOneOf: [
            ["account_ref", "stable_account_id", "auth_profile_ref", "principal_ref", "client_application_ref", "scopes"],
            ["identity_kind", "provider_scope_status", "account_ref", "stable_account_id", "auth_profile_ref", "credential_binding_ref"],
        ])
        if object.hasExactly(["account_ref", "stable_account_id", "auth_profile_ref", "principal_ref", "client_application_ref", "scopes"]) {
            let accountRef = try object.boundedString("account_ref", maximumBytes: 160)
            let stableAccountID = try object.boundedString("stable_account_id", maximumBytes: 256)
            let authProfileRef = try object.boundedString("auth_profile_ref", maximumBytes: 160)
            let principalRef = try object.boundedString("principal_ref", maximumBytes: 256)
            let clientApplicationRef = try object.boundedString("client_application_ref", maximumBytes: 256)
            let scopes = try object.array("scopes", minimum: 1, maximum: 64).map {
                guard case let .string(scope) = $0, !scope.isEmpty else { throw ApprovalHostError.malformedChallenge }
                return scope
            }
            guard scopes == Array(Set(scopes)).sorted() else { throw ApprovalHostError.malformedChallenge }
            return .scoped(
                accountRef: accountRef,
                stableAccountID: stableAccountID,
                authProfileRef: authProfileRef,
                principalRef: principalRef,
                clientApplicationRef: clientApplicationRef,
                scopes: scopes
            )
        }
        guard object.hasExactly(["identity_kind", "provider_scope_status", "account_ref", "stable_account_id", "auth_profile_ref", "credential_binding_ref"]),
              provider == "openai_ads",
              try object.string("identity_kind") == "OPENAI_ADS_ACCOUNT_KEY",
              try object.string("provider_scope_status") == "NOT_DOCUMENTED"
        else { throw ApprovalHostError.malformedChallenge }
        let credentialBindingRef = try object.boundedString("credential_binding_ref", maximumBytes: 96)
        guard credentialBindingRef.range(of: "^credential-binding:sha256:[0-9a-f]{64}$", options: .regularExpression) != nil else {
            throw ApprovalHostError.malformedChallenge
        }
        return .openAIAccountKey(
            accountRef: try object.boundedString("account_ref", maximumBytes: 160),
            stableAccountID: try object.boundedString("stable_account_id", maximumBytes: 256),
            authProfileRef: try object.boundedString("auth_profile_ref", maximumBytes: 160),
            credentialBindingRef: credentialBindingRef
        )
    }
}

public struct ExactMoney: Equatable, Sendable {
    public let minorUnits: Int64
    public let currency: String

    static func decode(_ value: CanonicalJSONValue) throws -> ExactMoney {
        let object = try ClosedObject(value, exactly: ["minor_units", "currency"])
        let minorUnits = try object.integer("minor_units")
        let currency = try object.string("currency")
        guard minorUnits >= 0,
              currency.range(of: "^[A-Z]{3}$", options: .regularExpression) != nil
        else { throw ApprovalHostError.malformedChallenge }
        return ExactMoney(minorUnits: minorUnits, currency: currency)
    }
}

public struct ExactEgressTarget: Equatable, Sendable {
    public let host: String
    public let purpose: String

    static func decode(_ value: CanonicalJSONValue) throws -> ExactEgressTarget {
        let object = try ClosedObject(value, exactly: ["host", "purpose"])
        let host = try object.boundedString("host", maximumBytes: 253)
        let purpose = try object.boundedString("purpose", maximumBytes: 120)
        guard host.range(of: "^[A-Za-z0-9.-]+$", options: .regularExpression) != nil else {
            throw ApprovalHostError.malformedChallenge
        }
        return ExactEgressTarget(host: host, purpose: purpose)
    }
}

public struct ExactStateReference: Equatable, Sendable {
    public let fingerprint: String
    public let revision: String?
    public let digest: String

    static func decode(_ value: CanonicalJSONValue) throws -> ExactStateReference {
        let object = try ClosedObject(value, exactly: ["fingerprint", "revision"])
        let fingerprint = try object.boundedString("fingerprint", minimumBytes: 16, maximumBytes: 256)
        let revision: String?
        if case .null = try object.value("revision") {
            revision = nil
        } else {
            revision = try object.boundedString("revision", maximumBytes: 256)
        }
        let canonicalBytes = try CanonicalJSON.encode(value)
        return ExactStateReference(
            fingerprint: fingerprint,
            revision: revision,
            digest: CanonicalJSON.sha256Hex(canonicalBytes)
        )
    }
}

public struct ExactFieldChange: Equatable, Sendable {
    public let path: String
    public let before: ExactStateReference
    public let after: ExactStateReference

    static func decode(_ value: CanonicalJSONValue) throws -> ExactFieldChange {
        let object = try ClosedObject(value, exactly: ["path", "before", "after"])
        return ExactFieldChange(
            path: try object.boundedString("path", maximumBytes: 256),
            before: try ExactStateReference.decode(try object.value("before")),
            after: try ExactStateReference.decode(try object.value("after"))
        )
    }
}

public struct ExactDelegationPolicy: Equatable, Sendable {
    public let policyID: String
    public let policyVersion: Int64
    public let grantorRef: String
    public let configurationHostRef: String
    public let sourceAuthorizationRef: String
    public let configurationInteractionRef: String
    public let authorizedAgentOrHostRef: String
    public let accountStableIDs: [String]
    public let allowedOperations: [ApprovalOperation]
    public let maximumChangePerOperation: [ExactMoneyLimit]
    public let cumulativeChangeBounds: [ExactMoneyLimit]
    public let effectiveFrom: Date
    public let effectiveFromText: String
    public let expiresAt: Date
    public let expiresAtText: String
    public let allowedEgressHosts: [String]
    public let revokedAt: Date?
    public let canonicalBytes: Data
    public let digest: String

    static func decode(_ value: CanonicalJSONValue, maximumItems: Int) throws -> ExactDelegationPolicy {
        let object = try ClosedObject(value, exactly: [
            "schema_version", "policy_id", "policy_version", "source", "grantor_ref", "source_authorization_ref",
            "configuration_host_ref", "configuration_interaction_ref", "authorized_agent_or_host_ref",
            "account_stable_ids", "allowed_operations", "maximum_change_per_operation", "cumulative_change_bounds",
            "effective_from", "expires_at", "allowed_egress_hosts", "revoked_at",
        ])
        guard try object.integer("schema_version") == 1,
              try object.string("source") == "USER_CONTROLLED_CONFIGURATION"
        else { throw ApprovalHostError.malformedChallenge }
        let policyID = try object.boundedString("policy_id", maximumBytes: 160)
        let policyVersion = try object.integer("policy_version")
        guard policyVersion >= 1 else { throw ApprovalHostError.malformedChallenge }
        let grantorRef = try object.boundedString("grantor_ref", maximumBytes: 256)
        let sourceAuthorizationRef = try object.boundedString("source_authorization_ref", maximumBytes: 256)
        let configurationHostRef = try object.boundedString("configuration_host_ref", maximumBytes: 256)
        let configurationInteractionRef = try object.boundedString("configuration_interaction_ref", maximumBytes: 256)
        let authorizedAgentOrHostRef = try object.boundedString("authorized_agent_or_host_ref", maximumBytes: 256)
        let accountIDs = try object.array("account_stable_ids", minimum: 1, maximum: maximumItems).map {
            guard case let .string(accountID) = $0,
                  isSafeDisplayValue(accountID, maximumBytes: 256)
            else {
                throw ApprovalHostError.malformedChallenge
            }
            return accountID
        }
        guard Set(accountIDs).count == accountIDs.count else { throw ApprovalHostError.malformedChallenge }
        let operationValues = try object.array("allowed_operations", minimum: 1, maximum: ApprovalOperation.allCases.count)
        let operations = try operationValues.map { operation -> ApprovalOperation in
            guard case let .string(raw) = operation, let parsed = ApprovalOperation(rawValue: raw) else {
                throw ApprovalHostError.malformedChallenge
            }
            return parsed
        }
        guard Set(operations).count == operations.count else { throw ApprovalHostError.malformedChallenge }
        let maximumChangePerOperation = try validateMoneyLimits(
            try object.array("maximum_change_per_operation", minimum: 1, maximum: maximumItems)
        )
        let cumulativeChangeBounds = try validateMoneyLimits(
            try object.array("cumulative_change_bounds", minimum: 1, maximum: maximumItems)
        )
        let effectiveFrom = try ClosedTimestamp.parse(try object.string("effective_from"))
        let expiresAt = try ClosedTimestamp.parse(try object.string("expires_at"))
        guard effectiveFrom < expiresAt else { throw ApprovalHostError.malformedChallenge }
        let egressHosts = try object.array("allowed_egress_hosts", minimum: 1, maximum: maximumItems).map {
            guard case let .string(host) = $0,
                  isSafeDisplayValue(host, maximumBytes: 253),
                  host.range(of: "^[A-Za-z0-9.-]+$", options: .regularExpression) != nil
            else {
                throw ApprovalHostError.malformedChallenge
            }
            return host
        }
        guard Set(egressHosts).count == egressHosts.count else { throw ApprovalHostError.malformedChallenge }
        let revokedAt: Date?
        switch try object.value("revoked_at") {
        case .null: revokedAt = nil
        case let .string(timestamp): revokedAt = try ClosedTimestamp.parse(timestamp)
        default: throw ApprovalHostError.malformedChallenge
        }
        let canonicalBytes = try CanonicalJSON.encode(value)
        return ExactDelegationPolicy(
            policyID: policyID,
            policyVersion: policyVersion,
            grantorRef: grantorRef,
            configurationHostRef: configurationHostRef,
            sourceAuthorizationRef: sourceAuthorizationRef,
            configurationInteractionRef: configurationInteractionRef,
            authorizedAgentOrHostRef: authorizedAgentOrHostRef,
            accountStableIDs: accountIDs,
            allowedOperations: operations,
            maximumChangePerOperation: maximumChangePerOperation,
            cumulativeChangeBounds: cumulativeChangeBounds,
            effectiveFrom: effectiveFrom,
            effectiveFromText: try object.string("effective_from"),
            expiresAt: expiresAt,
            expiresAtText: try object.string("expires_at"),
            allowedEgressHosts: egressHosts,
            revokedAt: revokedAt,
            canonicalBytes: canonicalBytes,
            digest: CanonicalJSON.sha256Hex(canonicalBytes)
        )
    }

    private static func validateMoneyLimits(_ values: [CanonicalJSONValue]) throws -> [ExactMoneyLimit] {
        let limits = try values.map(ExactMoneyLimit.decode)
        let currencies = limits.map(\.currency)
        guard Set(currencies).count == currencies.count else { throw ApprovalHostError.malformedChallenge }
        return limits
    }
}

public struct ExactMoneyLimit: Equatable, Sendable {
    public let maximumMinorUnits: Int64
    public let currency: String

    static func decode(_ value: CanonicalJSONValue) throws -> ExactMoneyLimit {
        let object = try ClosedObject(value, exactly: ["maximum_minor_units", "currency"])
        let maximumMinorUnits = try object.integer("maximum_minor_units")
        let currency = try object.string("currency")
        guard maximumMinorUnits >= 0,
              currency.range(of: "^[A-Z]{3}$", options: .regularExpression) != nil
        else { throw ApprovalHostError.malformedChallenge }
        return ExactMoneyLimit(maximumMinorUnits: maximumMinorUnits, currency: currency)
    }
}

struct ClosedObject {
    private let object: [String: CanonicalJSONValue]

    init(_ value: CanonicalJSONValue, exactly expected: [String]) throws {
        guard case let .object(object) = value, Set(object.keys) == Set(expected), object.count == expected.count else {
            throw ApprovalHostError.malformedChallenge
        }
        self.object = object
    }

    init(_ value: CanonicalJSONValue, fields: [String], allowingOneOf _: [[String]]) throws {
        guard case let .object(object) = value,
              Set(object.keys).isSubset(of: Set(fields))
        else { throw ApprovalHostError.malformedChallenge }
        self.object = object
    }

    func hasExactly(_ expected: [String]) -> Bool {
        object.count == expected.count && Set(object.keys) == Set(expected)
    }

    func value(_ key: String) throws -> CanonicalJSONValue {
        guard let value = object[key] else { throw ApprovalHostError.malformedChallenge }
        return value
    }

    func string(_ key: String) throws -> String {
        guard case let .string(value) = try value(key) else { throw ApprovalHostError.malformedChallenge }
        return value
    }

    func boundedString(_ key: String, minimumBytes: Int = 1, maximumBytes: Int) throws -> String {
        let string = try string(key)
        let byteCount = string.lengthOfBytes(using: .utf8)
        guard byteCount >= minimumBytes, byteCount <= maximumBytes,
              string.unicodeScalars.allSatisfy({ $0.value >= 0x20 && $0.value != 0x7F })
        else { throw ApprovalHostError.malformedChallenge }
        return string
    }

    func integer(_ key: String) throws -> Int64 {
        guard case let .integer(value) = try value(key) else { throw ApprovalHostError.malformedChallenge }
        return value
    }

    func bool(_ key: String) throws -> Bool {
        guard case let .bool(value) = try value(key) else { throw ApprovalHostError.malformedChallenge }
        return value
    }

    func array(_ key: String, minimum: Int, maximum: Int) throws -> [CanonicalJSONValue] {
        guard case let .array(value) = try value(key), value.count >= minimum, value.count <= maximum else {
            throw ApprovalHostError.malformedChallenge
        }
        return value
    }
}

private func isSafeDisplayValue(_ value: String, maximumBytes: Int) -> Bool {
    !value.isEmpty && value.lengthOfBytes(using: .utf8) <= maximumBytes && value.unicodeScalars.allSatisfy {
        $0.value >= 0x20 && $0.value != 0x7F
    }
}

enum ClosedTimestamp {
    private static let pattern = try! NSRegularExpression(
        // Pydantic's JSON serializer emits either whole UTC seconds or exactly
        // six fractional digits.  Accepting `.1Z` would preserve a different
        // lexical value from Python's `.100000Z`, breaking independent state
        // reconstruction even when the instant is equal.
        pattern: "^([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\\.([0-9]{6}))?Z$"
    )

    static func parse(_ raw: String) throws -> Date {
        let range = NSRange(raw.startIndex..., in: raw)
        // ICU's end anchor can match before a final line terminator. Every
        // retained timestamp must consume the complete canonical string.
        guard let match = pattern.firstMatch(in: raw, range: range),
              match.range == range, match.numberOfRanges == 8 else {
            throw ApprovalHostError.malformedChallenge
        }
        func component(_ position: Int) throws -> Int {
            let componentRange = match.range(at: position)
            guard componentRange.location != NSNotFound,
                  let range = Range(componentRange, in: raw),
                  let value = Int(raw[range])
            else { throw ApprovalHostError.malformedChallenge }
            return value
        }
        let year = try component(1)
        let month = try component(2)
        let day = try component(3)
        let hour = try component(4)
        let minute = try component(5)
        let second = try component(6)
        let fractionRange = match.range(at: 7)
        let nanosecond: Int
        if fractionRange.location == NSNotFound {
            nanosecond = 0
        } else if let range = Range(fractionRange, in: raw) {
            let fraction = String(raw[range])
            nanosecond = Int(fraction.padding(toLength: 9, withPad: "0", startingAt: 0)) ?? -1
        } else {
            throw ApprovalHostError.malformedChallenge
        }
        guard (1...9999).contains(year), (1...12).contains(month), (0...23).contains(hour),
              (0...59).contains(minute), (0...59).contains(second), (0..<1_000_000_000).contains(nanosecond)
        else { throw ApprovalHostError.malformedChallenge }
        var calendar = Calendar(identifier: .gregorian)
        calendar.locale = Locale(identifier: "en_US_POSIX")
        calendar.timeZone = TimeZone(secondsFromGMT: 0)!
        var components = DateComponents()
        components.calendar = calendar
        components.timeZone = calendar.timeZone
        components.year = year
        components.month = month
        components.day = day
        components.hour = hour
        components.minute = minute
        components.second = second
        components.nanosecond = nanosecond
        guard let date = calendar.date(from: components) else { throw ApprovalHostError.malformedChallenge }
        let roundTrip = calendar.dateComponents(
            [.year, .month, .day, .hour, .minute, .second, .nanosecond], from: date
        )
        guard roundTrip.year == year, roundTrip.month == month, roundTrip.day == day,
              roundTrip.hour == hour, roundTrip.minute == minute, roundTrip.second == second
        else { throw ApprovalHostError.malformedChallenge }
        // Foundation stores Date as a binary floating-point interval and may
        // return a neighboring nanosecond on round-trip.  The closed parser
        // retains the original validated whole-second/six-digit UTC text for every hash,
        // plan reconstruction, review, and receipt binding; Date is used only
        // for calendar validation and ordering.
        return date
    }
}

private func decodeBase64URL(_ raw: String, exactByteCount: Int) throws -> Data {
    guard raw.range(of: "^[A-Za-z0-9_-]+$", options: .regularExpression) != nil else {
        throw ApprovalHostError.malformedChallenge
    }
    let base64 = raw.replacingOccurrences(of: "-", with: "+").replacingOccurrences(of: "_", with: "/")
    let padding = String(repeating: "=", count: (4 - base64.count % 4) % 4)
    guard let data = Data(base64Encoded: base64 + padding), data.count == exactByteCount,
          base64URLEncoded(data) == raw
    else {
        throw ApprovalHostError.malformedChallenge
    }
    return data
}

func base64URLEncoded(_ data: Data) -> String {
    data.base64EncodedString()
        .replacingOccurrences(of: "+", with: "-")
        .replacingOccurrences(of: "/", with: "_")
        .replacingOccurrences(of: "=", with: "")
}
