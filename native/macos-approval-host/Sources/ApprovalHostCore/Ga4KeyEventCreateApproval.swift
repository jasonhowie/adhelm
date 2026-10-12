import Foundation

/// Closed native representation of the single GA4 key-event CREATE action
/// family that this host can review.  This is intentionally not a generic
/// `CompositePlan` carrier: every source binding that contributes to the
/// Python plan identifier is retained and the complete plan is reconstructed
/// before review, authentication, or signing.
///
/// The Python lifecycle owns the current fingerprint key.  The host binds the
/// opaque keyed semantic state and all of its fixed controls, but does not
/// claim to recompute or authenticate those HMAC values itself.
public struct ExactGa4KeyEventCreateAction: Equatable, Sendable {
    public static let fixtureOnlyStatus = "DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED"
    public static let editScope = "https://www.googleapis.com/auth/analytics.edit"
    public static let host = "analyticsadmin.googleapis.com"
    public static let egressPurpose = "fixed GA4 Admin key-event create"
    public static let recoveryDisposition = "RECONCILE_EXPORT_STOP_NO_REMOTE_COMPENSATION"

    public let workspace: ExactGa4WorkspaceBinding
    public let candidate: ExactGa4Candidate
    public let selection: ExactGa4SelectionBinding
    public let observation: ExactGa4ObservationBinding
    public let execution: ExactGa4ExecutionBinding
    public let nativeRequest: ExactGa4NativeRequestBinding
    public let semanticState: ExactGa4SemanticState
    public let plan: ExactGa4Plan
    public let canonicalBytes: Data
    public let digest: String

    static func decode(_ value: CanonicalJSONValue, maximumSubplans: Int) throws -> ExactGa4KeyEventCreateAction {
        guard maximumSubplans >= 1 else { throw ApprovalHostError.malformedChallenge }
        let object = try ClosedObject(value, exactly: [
            "schema_version", "status", "workspace_binding", "candidate", "selection_binding",
            "observation_binding", "execution_binding", "native_request_binding", "semantic_state",
            "plan", "declared_destination_relationship", "destination_to_execution_profile_mapping",
            "current_state_status", "provider_create_permission", "semantic_source_mapping",
            "configuration_readback", "browser_network_verification", "realtime_verification",
            "processed_report_verification", "native_conversion_mapping", "execution_authority",
        ])
        guard try object.integer("schema_version") == 1,
              try object.string("status") == fixtureOnlyStatus,
              try object.string("declared_destination_relationship") == "DECLARED_DESTINATION_SELECTION_ONLY",
              try object.string("destination_to_execution_profile_mapping") == "NOT_EVIDENCED",
              try object.string("current_state_status") == "RECHECK_REQUIRED_BEFORE_EGRESS",
              try object.string("provider_create_permission") == "NOT_PREPROVABLE",
              try object.string("semantic_source_mapping") == "NOT_VERIFIED",
              try object.string("configuration_readback") == "REQUIRED_AFTER_CREATE",
              try object.string("browser_network_verification") == "NOT_EVALUATED",
              try object.string("realtime_verification") == "NOT_EVALUATED",
              try object.string("processed_report_verification") == "NOT_EVALUATED",
              try object.string("native_conversion_mapping") == "NONE_CREATED",
              try object.bool("execution_authority") == false
        else { throw ApprovalHostError.malformedChallenge }

        let workspaceValue = try object.value("workspace_binding")
        let candidateValue = try object.value("candidate")
        let selectionValue = try object.value("selection_binding")
        let observationValue = try object.value("observation_binding")
        let executionValue = try object.value("execution_binding")
        let nativeRequestValue = try object.value("native_request_binding")
        let semanticStateValue = try object.value("semantic_state")
        let planValue = try object.value("plan")

        let workspace = try ExactGa4WorkspaceBinding.decode(workspaceValue)
        let candidate = try ExactGa4Candidate.decode(candidateValue)
        let selection = try ExactGa4SelectionBinding.decode(selectionValue)
        let observation = try ExactGa4ObservationBinding.decode(observationValue)
        let execution = try ExactGa4ExecutionBinding.decode(executionValue)
        let nativeRequest = try ExactGa4NativeRequestBinding.decode(nativeRequestValue)
        let semanticState = try ExactGa4SemanticState.decode(semanticStateValue)
        let plan = try ExactGa4Plan.decode(planValue)

        guard selection.checkpointDigest == observation.checkpointDigest,
              observation.profileRef == execution.authProfileRef,
              observation.configurationDigest == execution.configurationDigest,
              observation.profileConfigurationDigest == execution.profileConfigurationDigest,
              observation.inventoryDigest == semanticState.inventoryDigest,
              semanticState.candidateDigest == candidate.compilerCandidateDigest,
              semanticState.preconditionsCandidateDigest == candidate.compilerCandidateDigest,
              semanticState.preconditionsInventoryDigest == observation.inventoryDigest,
              candidate.identity == execution.target.identity,
              candidate.targetDigest == nativeRequest.targetDigest,
              candidate.requestDigest == nativeRequest.bodyDigest,
              selection.evaluatedAt <= plan.expiresAt,
              observation.readFinishedAt <= selection.evaluatedAt,
              observation.readStartedAt <= observation.readFinishedAt
        else { throw ApprovalHostError.malformedChallenge }

        let expectedPlan = try expectedPlanValue(
            workspaceValue: workspaceValue,
            candidateValue: candidateValue,
            selectionValue: selectionValue,
            observationValue: observationValue,
            executionValue: executionValue,
            nativeRequestValue: nativeRequestValue,
            semanticStateValue: semanticStateValue,
            workspace: workspace,
            candidate: candidate,
            execution: execution,
            semanticState: semanticState,
            expiresAtText: plan.expiresAtText
        )
        guard planValue == expectedPlan else { throw ApprovalHostError.malformedChallenge }

        let canonicalBytes = try CanonicalJSON.encode(value)
        return ExactGa4KeyEventCreateAction(
            workspace: workspace,
            candidate: candidate,
            selection: selection,
            observation: observation,
            execution: execution,
            nativeRequest: nativeRequest,
            semanticState: semanticState,
            plan: plan,
            canonicalBytes: canonicalBytes,
            digest: CanonicalJSON.sha256Hex(canonicalBytes)
        )
    }

    private static func expectedPlanValue(
        workspaceValue: CanonicalJSONValue,
        candidateValue: CanonicalJSONValue,
        selectionValue: CanonicalJSONValue,
        observationValue: CanonicalJSONValue,
        executionValue: CanonicalJSONValue,
        nativeRequestValue: CanonicalJSONValue,
        semanticStateValue: CanonicalJSONValue,
        workspace: ExactGa4WorkspaceBinding,
        candidate: ExactGa4Candidate,
        execution: ExactGa4ExecutionBinding,
        semanticState: ExactGa4SemanticState,
        expiresAtText: String
    ) throws -> CanonicalJSONValue {
        let actionReferenceInput = CanonicalJSONValue.object([
            "kind": .string("ga4-key-event-create-action-v1"),
            "workspace": workspaceValue,
            "candidate": candidateValue,
            "selection": selectionValue,
            "observation": observationValue,
            "execution": executionValue,
            "native_request": nativeRequestValue,
            "semantic_state": semanticStateValue,
            "expires_at": .string(ga4PythonUTCISO8601(expiresAtText)),
        ])
        let actionReference = CanonicalJSON.sha256Hex(try CanonicalJSON.encode(actionReferenceInput))
        let planID = "ga4-key-event-create-\(actionReference)"
        let account: CanonicalJSONValue = .object([
            "account_ref": .string(candidate.identity.propertyRef),
            "stable_account_id": .string(candidate.identity.propertyClientRef),
            "auth_profile_ref": .string(execution.authProfileRef),
            "principal_ref": .string(execution.principalRef),
            "client_application_ref": .string(execution.clientApplicationRef),
            "scopes": .array([.string(Self.editScope)]),
        ])
        let subplan: CanonicalJSONValue = .object([
            "subplan_id": .string(planID),
            "provider": .string("ga4_admin"),
            "operation": .string(ApprovalOperation.create.rawValue),
            "account": account,
            "provider_api_version": .string("v1alpha"),
            "capability_version": .string(candidate.capabilityVersion),
            "before": semanticState.beforeValue,
            "after": semanticState.afterValue,
            "field_changes": .array([
                .object([
                    "path": .string("key-event/semantic-collection"),
                    "before": semanticState.beforeValue,
                    "after": semanticState.afterValue,
                ]),
            ]),
            "max_spend": .object([
                "minor_units": .integer(0),
                "currency": .string(execution.target.expectedCurrency),
            ]),
            "egress": .array([
                .object([
                    "host": .string(Self.host),
                    "purpose": .string(Self.egressPurpose),
                ]),
            ]),
            "recovery_actions": .array([]),
            "recovery_disposition": .string(Self.recoveryDisposition),
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

public struct ExactGa4WorkspaceBinding: Equatable, Sendable {
    public let workspaceProfileRef: String

    static func decode(_ value: CanonicalJSONValue) throws -> ExactGa4WorkspaceBinding {
        let object = try ClosedObject(value, exactly: ["schema_version", "binding_kind", "workspace_profile_ref"])
        let reference = try object.boundedString("workspace_profile_ref", maximumBytes: 160)
        guard try object.integer("schema_version") == 1,
              try object.string("binding_kind") == "LOCAL_LEDGER_WORKSPACE",
              ga4SafeReference(reference)
        else { throw ApprovalHostError.malformedChallenge }
        return ExactGa4WorkspaceBinding(workspaceProfileRef: reference)
    }
}

public struct ExactGa4Identity: Equatable, Sendable {
    public let propertyRef: String
    public let propertyClientRef: String
    public let webStreamRef: String
    public let webStreamClientRef: String

    static func decode(_ value: CanonicalJSONValue) throws -> ExactGa4Identity {
        let object = try ClosedObject(value, exactly: [
            "property_ref", "property_client_ref", "web_stream_ref", "web_stream_client_ref", "api_version", "required_scope",
        ])
        let propertyClientRef = try object.boundedString("property_client_ref", maximumBytes: 20)
        let webStreamClientRef = try object.boundedString("web_stream_client_ref", maximumBytes: 20)
        let propertyRef = try object.boundedString("property_ref", maximumBytes: 160)
        let webStreamRef = try object.boundedString("web_stream_ref", maximumBytes: 160)
        guard ga4NativeID(propertyClientRef), ga4NativeID(webStreamClientRef),
              propertyRef == "ga4_admin.property.\(propertyClientRef)",
              webStreamRef == "ga4_admin.web_stream.\(webStreamClientRef)",
              try object.string("api_version") == "v1alpha",
              try object.string("required_scope") == ExactGa4KeyEventCreateAction.editScope
        else { throw ApprovalHostError.malformedChallenge }
        return ExactGa4Identity(
            propertyRef: propertyRef,
            propertyClientRef: propertyClientRef,
            webStreamRef: webStreamRef,
            webStreamClientRef: webStreamClientRef
        )
    }
}

public struct ExactGa4Candidate: Equatable, Sendable {
    public let identity: ExactGa4Identity
    public let capabilityVersion: String
    public let compilerCandidateDigest: String
    public let targetDigest: String
    public let requestDigest: String
    public let eventName: String

    static func decode(_ value: CanonicalJSONValue) throws -> ExactGa4Candidate {
        let record = try ClosedObject(value, exactly: ["schema_version", "event_type", "compilation", "candidate_digest"])
        guard try record.integer("schema_version") == 1,
              try record.string("event_type") == "GA4_KEY_EVENT_CANDIDATE_COMPILED",
              ga4Digest(try record.string("candidate_digest"))
        else { throw ApprovalHostError.malformedChallenge }
        let compilationValue = try record.value("compilation")
        let compilation = try ClosedObject(compilationValue, exactly: [
            "schema_version", "provider", "compiler_candidate", "compiler_candidate_digest", "conversion_binding",
            "compiler_evidence", "execution_status", "semantic_accuracy", "source_event_mapping",
            "effective_time_status", "requires_new_compilation_for_planning", "requires_exact_authorization",
            "remote_side_effects", "execution_authority",
        ])
        let candidateDigest = try record.string("candidate_digest")
        let compilationDigest = try CanonicalJSON.sha256Hex(CanonicalJSON.encode(compilationValue))
        guard try compilation.integer("schema_version") == 1,
              try compilation.string("provider") == "ga4_admin",
              try compilation.string("compiler_evidence") == "FIXTURE_VERIFIED_LIVE_UNVERIFIED",
              try compilation.string("execution_status") == "OFFLINE_CANDIDATE_NOT_EXECUTABLE",
              try compilation.string("semantic_accuracy") == "NOT_VERIFIED",
              try compilation.string("source_event_mapping") == "NOT_PROVEN",
              try compilation.string("effective_time_status") == "DECLARED_NOT_EVALUATED",
              try compilation.bool("requires_new_compilation_for_planning"),
              try compilation.bool("requires_exact_authorization"),
              try compilation.bool("remote_side_effects") == false,
              try compilation.bool("execution_authority") == false,
              ga4Digest(try compilation.string("compiler_candidate_digest")),
              compilationDigest == candidateDigest
        else { throw ApprovalHostError.malformedChallenge }

        let compilerCandidateValue = try compilation.value("compiler_candidate")
        let compilerCandidate = try ClosedObject(compilerCandidateValue, exactly: [
            "schema_version", "provider", "capability_version", "event_contract_ref", "semantics_ref", "identity",
            "target", "request", "declared_field_names", "input_digest", "prospective_effects", "planning_boundary",
        ])
        let compilerCandidateDigest = try compilation.string("compiler_candidate_digest")
        let expectedCompilerCandidateDigest = try CanonicalJSON.sha256Hex(CanonicalJSON.encode(compilerCandidateValue))
        guard try compilerCandidate.integer("schema_version") == 1,
              try compilerCandidate.string("provider") == "ga4_admin",
              try compilerCandidate.string("capability_version") == "ga4-admin-key-event-create-contract-2026-09-04",
              ga4SafeReference(try compilerCandidate.string("event_contract_ref")),
              ga4SafeReference(try compilerCandidate.string("semantics_ref")),
              ga4Digest(try compilerCandidate.string("input_digest")),
              expectedCompilerCandidateDigest == compilerCandidateDigest
        else { throw ApprovalHostError.malformedChallenge }
        let identity = try ExactGa4Identity.decode(try compilerCandidate.value("identity"))
        try validateGa4CandidateTarget(try compilerCandidate.value("target"), identity: identity)
        let eventName = try validateGa4CandidateRequest(try compilerCandidate.value("request"))
        let declaredFieldNames = try validateGa4DeclaredFields(
            try compilerCandidate.value("declared_field_names")
        )
        try validateGa4ProspectiveEffects(try compilerCandidate.value("prospective_effects"))
        try validateGa4PlanningBoundary(try compilerCandidate.value("planning_boundary"))
        let boundFieldNames = try validateGa4ConversionBinding(
            try compilation.value("conversion_binding")
        )
        guard declaredFieldNames == boundFieldNames else {
            throw ApprovalHostError.malformedChallenge
        }
        return ExactGa4Candidate(
            identity: identity,
            capabilityVersion: try compilerCandidate.string("capability_version"),
            compilerCandidateDigest: try compilation.string("compiler_candidate_digest"),
            targetDigest: CanonicalJSON.sha256Hex(try CanonicalJSON.encode(try compilerCandidate.value("target"))),
            requestDigest: CanonicalJSON.sha256Hex(try CanonicalJSON.encode(try compilerCandidate.value("request"))),
            eventName: eventName
        )
    }
}

public struct ExactGa4SelectionBinding: Equatable, Sendable {
    public let checkpointDigest: String
    public let evaluatedAt: Date
    public let evaluatedAtText: String
    public let candidateRecordDigest: String
    public let contractRecordDigest: String

    static func decode(_ value: CanonicalJSONValue) throws -> ExactGa4SelectionBinding {
        let object = try ClosedObject(value, exactly: [
            "schema_version", "binding_kind", "candidate_record_digest", "contract_record_digest", "checkpoint_digest", "evaluated_at",
        ])
        let evaluatedAtText = try object.string("evaluated_at")
        guard try object.integer("schema_version") == 1,
              try object.string("binding_kind") == "VERIFIED_LOCAL_CANDIDATE_CONTRACT_SELECTION",
              ga4Digest(try object.string("candidate_record_digest")),
              ga4Digest(try object.string("contract_record_digest")),
              ga4Digest(try object.string("checkpoint_digest"))
        else { throw ApprovalHostError.malformedChallenge }
        return ExactGa4SelectionBinding(
            checkpointDigest: try object.string("checkpoint_digest"),
            evaluatedAt: try ClosedTimestamp.parse(evaluatedAtText),
            evaluatedAtText: evaluatedAtText,
            candidateRecordDigest: try object.string("candidate_record_digest"),
            contractRecordDigest: try object.string("contract_record_digest")
        )
    }
}

public struct ExactGa4ObservationBinding: Equatable, Sendable {
    public let checkpointDigest: String
    public let profileRef: String
    public let configurationDigest: String
    public let profileConfigurationDigest: String
    public let inventoryDigest: String
    public let readStartedAt: Date
    public let readStartedAtText: String
    public let readFinishedAt: Date
    public let readFinishedAtText: String
    public let observationRecordDigest: String

    static func decode(_ value: CanonicalJSONValue) throws -> ExactGa4ObservationBinding {
        let object = try ClosedObject(value, exactly: [
            "schema_version", "binding_kind", "observation_record_digest", "checkpoint_digest", "profile_ref",
            "configuration_digest", "profile_configuration_digest", "inventory_digest", "read_started_at",
            "read_finished_at", "evidence_kind", "consistency", "granted_scopes", "provider_create_permission",
        ])
        let startedText = try object.string("read_started_at")
        let finishedText = try object.string("read_finished_at")
        guard try object.integer("schema_version") == 1,
              try object.string("binding_kind") == "GA4_ADMIN_EDIT_INVENTORY_OBSERVATION",
              ga4Digest(try object.string("observation_record_digest")),
              ga4Digest(try object.string("checkpoint_digest")),
              ga4Digest(try object.string("configuration_digest")),
              ga4Digest(try object.string("profile_configuration_digest")),
              ga4Digest(try object.string("inventory_digest")),
              ga4SafeReference(try object.string("profile_ref")),
              try object.string("evidence_kind") == "EDIT_PROFILE_CONFIGURATION_READ",
              try object.string("consistency") == "SEQUENTIAL_NONATOMIC",
              try object.string("granted_scopes") == "NOT_EVALUATED",
              try object.string("provider_create_permission") == "NOT_PREPROVABLE"
        else { throw ApprovalHostError.malformedChallenge }
        let started = try ClosedTimestamp.parse(startedText)
        let finished = try ClosedTimestamp.parse(finishedText)
        guard started <= finished else { throw ApprovalHostError.malformedChallenge }
        return ExactGa4ObservationBinding(
            checkpointDigest: try object.string("checkpoint_digest"),
            profileRef: try object.string("profile_ref"),
            configurationDigest: try object.string("configuration_digest"),
            profileConfigurationDigest: try object.string("profile_configuration_digest"),
            inventoryDigest: try object.string("inventory_digest"),
            readStartedAt: started,
            readStartedAtText: startedText,
            readFinishedAt: finished,
            readFinishedAtText: finishedText,
            observationRecordDigest: try object.string("observation_record_digest")
        )
    }
}

public struct ExactGa4ExecutionTarget: Equatable, Sendable {
    public let identity: ExactGa4Identity
    public let expectedTimezone: String
    public let expectedCurrency: String
    public let digest: String

    static func decode(_ value: CanonicalJSONValue) throws -> ExactGa4ExecutionTarget {
        let object = try ClosedObject(value, exactly: [
            "schema_version", "profile_purpose", "property_ref", "property_client_ref", "web_stream_ref",
            "web_stream_client_ref", "expected_timezone", "expected_currency", "admin_api_version", "required_scope",
        ])
        let propertyRef = try object.value("property_ref")
        let propertyClientRef = try object.value("property_client_ref")
        let webStreamRef = try object.value("web_stream_ref")
        let webStreamClientRef = try object.value("web_stream_client_ref")
        let apiVersion = try object.value("admin_api_version")
        let requiredScope = try object.value("required_scope")
        let identity = try ExactGa4Identity.decode(.object([
            "property_ref": propertyRef,
            "property_client_ref": propertyClientRef,
            "web_stream_ref": webStreamRef,
            "web_stream_client_ref": webStreamClientRef,
            "api_version": apiVersion,
            "required_scope": requiredScope,
        ]))
        let timezone = try object.boundedString("expected_timezone", maximumBytes: 80)
        let currency = try object.boundedString("expected_currency", maximumBytes: 3)
        guard try object.integer("schema_version") == 1,
              try object.string("profile_purpose") == "EDIT",
              try object.string("admin_api_version") == "v1alpha",
              try object.string("required_scope") == ExactGa4KeyEventCreateAction.editScope,
              ga4Currency(currency),
              ga4SupportedTimezone(timezone)
        else { throw ApprovalHostError.malformedChallenge }
        return ExactGa4ExecutionTarget(
            identity: identity,
            expectedTimezone: timezone,
            expectedCurrency: currency,
            digest: CanonicalJSON.sha256Hex(try CanonicalJSON.encode(value))
        )
    }
}

public struct ExactGa4ExecutionBinding: Equatable, Sendable {
    public let authProfileRef: String
    public let principalRef: String
    public let clientApplicationRef: String
    public let configurationDigest: String
    public let profileConfigurationDigest: String
    public let target: ExactGa4ExecutionTarget

    static func decode(_ value: CanonicalJSONValue) throws -> ExactGa4ExecutionBinding {
        let object = try ClosedObject(value, exactly: [
            "schema_version", "binding_kind", "auth_profile_ref", "profile_configuration_digest",
            "configuration_digest", "provider", "purpose", "credential_source", "principal_ref",
            "client_application_ref", "scopes", "target", "local_association_status",
            "provider_scope_status", "provider_create_permission",
        ])
        let scopes = try object.array("scopes", minimum: 1, maximum: 1)
        guard try object.integer("schema_version") == 1,
              try object.string("binding_kind") == "GA4_ADMIN_EDIT_LOCAL_OAUTH_ASSOCIATION",
              ga4SafeReference(try object.string("auth_profile_ref")),
              ga4SafeReference(try object.string("principal_ref")),
              ga4SafeReference(try object.string("client_application_ref")),
              ga4Digest(try object.string("profile_configuration_digest")),
              ga4Digest(try object.string("configuration_digest")),
              try object.string("provider") == "ga4_admin",
              try object.string("purpose") == "EDIT",
              try object.string("credential_source") == "OS_OAUTH_GOOGLE",
              scopes == [.string(ExactGa4KeyEventCreateAction.editScope)],
              try object.string("local_association_status") == "LOCAL_SEALED_OAUTH_ASSOCIATION_ONLY",
              try object.string("provider_scope_status") == "NOT_PROVEN_BY_RESOURCE_READ",
              try object.string("provider_create_permission") == "NOT_PREPROVABLE"
        else { throw ApprovalHostError.malformedChallenge }
        return ExactGa4ExecutionBinding(
            authProfileRef: try object.string("auth_profile_ref"),
            principalRef: try object.string("principal_ref"),
            clientApplicationRef: try object.string("client_application_ref"),
            configurationDigest: try object.string("configuration_digest"),
            profileConfigurationDigest: try object.string("profile_configuration_digest"),
            target: try ExactGa4ExecutionTarget.decode(try object.value("target"))
        )
    }
}

public struct ExactGa4NativeRequestBinding: Equatable, Sendable {
    public let targetDigest: String
    public let bodyDigest: String

    static func decode(_ value: CanonicalJSONValue) throws -> ExactGa4NativeRequestBinding {
        let object = try ClosedObject(value, exactly: ["schema_version", "host", "method", "api_version", "target_digest", "body_digest"])
        guard try object.integer("schema_version") == 1,
              try object.string("host") == ExactGa4KeyEventCreateAction.host,
              try object.string("method") == "POST",
              try object.string("api_version") == "v1alpha",
              ga4Digest(try object.string("target_digest")),
              ga4Digest(try object.string("body_digest"))
        else { throw ApprovalHostError.malformedChallenge }
        return ExactGa4NativeRequestBinding(
            targetDigest: try object.string("target_digest"), bodyDigest: try object.string("body_digest")
        )
    }
}

public struct ExactGa4SemanticState: Equatable, Sendable {
    public let candidateDigest: String
    public let inventoryDigest: String
    public let beforeValue: CanonicalJSONValue
    public let afterValue: CanonicalJSONValue
    public let beforeFingerprint: String
    public let afterFingerprint: String
    public let beforeMemberCount: Int64
    public let afterMemberCount: Int64
    public let documentedKeyEventCap: Int64
    public let remainingDocumentedSlots: Int64
    public let inventoryScope: String
    public let keyedContentIntegrity: String
    public let duplicateStatus: String
    public let capacityStatus: String
    public let currentProviderState: String
    public let reservationStatus: String
    public let permissionStatus: String
    public let preconditionsCandidateDigest: String
    public let preconditionsInventoryDigest: String

    static func decode(_ value: CanonicalJSONValue) throws -> ExactGa4SemanticState {
        let object = try ClosedObject(value, exactly: [
            "schema_version", "state_kind", "semantic_projection", "target_binding_fingerprint", "candidate_digest",
            "inventory_digest", "before", "after", "before_member_count", "after_member_count", "preconditions",
        ])
        let beforeValue = try object.value("before")
        let afterValue = try object.value("after")
        let beforeFingerprint = try ga4StateFingerprint(beforeValue)
        let afterFingerprint = try ga4StateFingerprint(afterValue)
        let beforeCount = try object.integer("before_member_count")
        let afterCount = try object.integer("after_member_count")
        let preconditions = try ClosedObject(try object.value("preconditions"), exactly: [
            "schema_version", "candidate_digest", "inventory_digest", "inventory_scope", "fingerprint_key_continuity",
            "inventory_content_integrity", "observed_service_level", "complete_observed_key_event_total",
            "documented_key_event_cap", "remaining_documented_slots", "capacity_status", "duplicate_status",
            "current_provider_state", "reservation_status", "permission_status", "remote_side_effects", "execution_authority",
        ])
        let total = try preconditions.integer("complete_observed_key_event_total")
        let cap = try preconditions.integer("documented_key_event_cap")
        let remaining = try preconditions.integer("remaining_documented_slots")
        guard try object.integer("schema_version") == 1,
              try object.string("state_kind") == "GA4_KEY_EVENT_CREATE_SEMANTIC_COLLECTION_V1",
              try object.string("semantic_projection") == "EVENT_NAME_HMAC_COUNTING_DEFAULT_VALUE_EXACT_V1",
              ga4Digest(try object.string("target_binding_fingerprint")),
              ga4Digest(try object.string("candidate_digest")),
              ga4Digest(try object.string("inventory_digest")),
              afterCount == beforeCount + 1,
              beforeCount >= 0, afterCount >= 1,
              beforeFingerprint != afterFingerprint,
              try preconditions.integer("schema_version") == 1,
              ga4Digest(try preconditions.string("candidate_digest")),
              ga4Digest(try preconditions.string("inventory_digest")),
              try preconditions.string("inventory_scope") == "COMPLETE_SUPPLIED_EDIT_INVENTORY",
              try preconditions.string("fingerprint_key_continuity") == "MATCHES_CURRENT_LOCAL_FINGERPRINT_KEY",
              try preconditions.string("inventory_content_integrity") == "MATCHES_CURRENT_LOCAL_FINGERPRINT_KEYED_CONTENT",
              try preconditions.string("observed_service_level") == "GOOGLE_ANALYTICS_STANDARD",
              cap == 30, total == beforeCount, remaining == cap - total, remaining >= 1,
              try preconditions.string("capacity_status") == "BELOW_DOCUMENTED_STANDARD_KEY_EVENT_CAP",
              try preconditions.string("duplicate_status") == "NOT_OBSERVED_IN_COMPLETE_SUPPLIED_INVENTORY",
              try preconditions.string("current_provider_state") == "NOT_ESTABLISHED",
              try preconditions.string("reservation_status") == "NOT_ESTABLISHED",
              try preconditions.string("permission_status") == "NOT_EVALUATED",
              try preconditions.bool("remote_side_effects") == false,
              try preconditions.bool("execution_authority") == false
        else { throw ApprovalHostError.malformedChallenge }
        return ExactGa4SemanticState(
            candidateDigest: try object.string("candidate_digest"),
            inventoryDigest: try object.string("inventory_digest"),
            beforeValue: beforeValue,
            afterValue: afterValue,
            beforeFingerprint: beforeFingerprint,
            afterFingerprint: afterFingerprint,
            beforeMemberCount: beforeCount,
            afterMemberCount: afterCount,
            documentedKeyEventCap: cap,
            remainingDocumentedSlots: remaining,
            inventoryScope: try preconditions.string("inventory_scope"),
            keyedContentIntegrity: try preconditions.string("inventory_content_integrity"),
            duplicateStatus: try preconditions.string("duplicate_status"),
            capacityStatus: try preconditions.string("capacity_status"),
            currentProviderState: try preconditions.string("current_provider_state"),
            reservationStatus: try preconditions.string("reservation_status"),
            permissionStatus: try preconditions.string("permission_status"),
            preconditionsCandidateDigest: try preconditions.string("candidate_digest"),
            preconditionsInventoryDigest: try preconditions.string("inventory_digest")
        )
    }
}

public struct ExactGa4Plan: Equatable, Sendable {
    public let planID: String
    public let profileRef: String
    public let expiresAt: Date
    public let expiresAtText: String
    public let subplanDigest: String
    public let maximumCurrency: String
    public let digest: String

    static func decode(_ value: CanonicalJSONValue) throws -> ExactGa4Plan {
        let object = try ClosedObject(value, exactly: ["schema_version", "plan_id", "profile_ref", "subplans", "expires_at", "stop_on_non_verified"])
        let expiresAtText = try object.string("expires_at")
        let subplans = try object.array("subplans", minimum: 1, maximum: 1)
        let subplanValue = subplans[0]
        let subplan = try ClosedObject(subplanValue, exactly: [
            "subplan_id", "provider", "operation", "account", "provider_api_version", "capability_version",
            "before", "after", "field_changes", "max_spend", "egress", "recovery_actions",
            "recovery_disposition", "expires_at",
        ])
        let money = try ClosedObject(try subplan.value("max_spend"), exactly: ["minor_units", "currency"])
        let egress = try subplan.array("egress", minimum: 1, maximum: 1)
        let egressItem = try ClosedObject(egress[0], exactly: ["host", "purpose"])
        let recovery = try subplan.array("recovery_actions", minimum: 0, maximum: 0)
        let currency = try money.string("currency")
        let subplanExpiresAtText = try subplan.string("expires_at")
        guard recovery.isEmpty,
              try object.integer("schema_version") == 1,
              try object.bool("stop_on_non_verified"),
              ga4SafeReference(try object.string("plan_id")),
              ga4SafeReference(try object.string("profile_ref")),
              try subplan.string("provider") == "ga4_admin",
              try subplan.string("operation") == "CREATE",
              try subplan.string("provider_api_version") == "v1alpha",
              try subplan.string("recovery_disposition") == ExactGa4KeyEventCreateAction.recoveryDisposition,
              try money.integer("minor_units") == 0,
              ga4Currency(currency),
              try egressItem.string("host") == ExactGa4KeyEventCreateAction.host,
              try egressItem.string("purpose") == ExactGa4KeyEventCreateAction.egressPurpose,
              subplanExpiresAtText == expiresAtText
        else { throw ApprovalHostError.malformedChallenge }
        return ExactGa4Plan(
            planID: try object.string("plan_id"),
            profileRef: try object.string("profile_ref"),
            expiresAt: try ClosedTimestamp.parse(expiresAtText),
            expiresAtText: expiresAtText,
            subplanDigest: CanonicalJSON.sha256Hex(try CanonicalJSON.encode(subplanValue)),
            maximumCurrency: currency,
            digest: CanonicalJSON.sha256Hex(try CanonicalJSON.encode(value))
        )
    }
}

private func ga4Digest(_ value: String) -> Bool {
    value.utf8.count == 64 && value.utf8.allSatisfy { byte in
        (byte >= 48 && byte <= 57) || (byte >= 97 && byte <= 102)
    }
}

private func ga4SafeText(_ value: String, maximumBytes: Int) -> Bool {
    !value.isEmpty && value.lengthOfBytes(using: .utf8) <= maximumBytes && value.unicodeScalars.allSatisfy {
        $0.value >= 0x20 && $0.value != 0x7F
    }
}

private func ga4SafeReference(_ value: String) -> Bool {
    !value.isEmpty && value.utf8.count <= 160 && value.utf8.allSatisfy { byte in
        (byte >= 48 && byte <= 57) || (byte >= 65 && byte <= 90)
            || (byte >= 97 && byte <= 122) || [46, 95, 58, 47, 45].contains(byte)
    }
}

private func ga4NativeID(_ value: String) -> Bool {
    !value.isEmpty && value.utf8.count <= 20 && value.utf8.allSatisfy { $0 >= 48 && $0 <= 57 }
}

private func ga4Currency(_ value: String) -> Bool {
    value.utf8.count == 3 && value.utf8.allSatisfy { $0 >= 65 && $0 <= 90 }
}

private func ga4SupportedTimezone(_ value: String) -> Bool {
    value == "UTC" || value == "America/Chicago"
}

private func ga4EventName(_ value: String) -> Bool {
    let bytes = Array(value.utf8)
    guard !bytes.isEmpty, bytes.count <= 40, bytes[0] >= 97, bytes[0] <= 122 else { return false }
    return bytes.dropFirst().allSatisfy { byte in
        (byte >= 97 && byte <= 122) || (byte >= 48 && byte <= 57) || byte == 95
    }
}

private func ga4SafeEventName(_ value: String) -> Bool {
    guard ga4EventName(value) else { return false }
    // This closed subset mirrors the Python compiler's retained name boundary.
    // The host displays the event name during a local review, so it must not
    // independently accept an otherwise syntactically valid sensitive surface.
    let prohibitedTerms = [
        "email", "phone", "user", "client", "patient", "member", "account", "eligibility",
        "underwriting", "health", "financial", "gclid", "fbclid", "ssn", "password", "token",
        "cookie", "secret", "contactid", "leadid", "customerid", "ttclid", "msclkid", "formvalue",
        "formvalues", "domtext", "pagetext", "url",
    ]
    return !prohibitedTerms.contains { value.contains($0) }
}

private func ga4PythonUTCISO8601(_ canonicalUTC: String) -> String {
    String(canonicalUTC.dropLast()) + "+00:00"
}

private func ga4StateFingerprint(_ value: CanonicalJSONValue) throws -> String {
    let object = try ClosedObject(value, exactly: ["fingerprint", "revision"])
    guard ga4Digest(try object.string("fingerprint")),
          try object.string("revision") == "GA4_KEY_EVENT_CREATE_SEMANTIC_COLLECTION_V1"
    else { throw ApprovalHostError.malformedChallenge }
    return try object.string("fingerprint")
}

private func validateGa4CandidateTarget(_ value: CanonicalJSONValue, identity: ExactGa4Identity) throws {
    let object = try ClosedObject(value, exactly: ["host", "api_version", "method", "parent", "path"])
    guard try object.string("host") == ExactGa4KeyEventCreateAction.host,
          try object.string("api_version") == "v1alpha",
          try object.string("method") == "POST",
          try object.string("parent") == "properties/\(identity.propertyClientRef)",
          try object.string("path") == "/v1alpha/properties/\(identity.propertyClientRef)/keyEvents"
    else { throw ApprovalHostError.malformedChallenge }
}

private func validateGa4CandidateRequest(_ value: CanonicalJSONValue) throws -> String {
    let object = try ClosedObject(value, exactly: ["eventName", "countingMethod", "defaultValue"])
    let eventName = try object.boundedString("eventName", maximumBytes: 40)
    guard ga4SafeEventName(eventName),
          try object.string("countingMethod") == "ONCE_PER_EVENT"
    else { throw ApprovalHostError.malformedChallenge }
    switch try object.value("defaultValue") {
    case .null:
        return eventName
    case .object:
        // The retained GA4 CREATE action used by this narrow first subject has
        // no default value. Supporting a decimal API body would require a
        // separate closed number representation; refuse it before review.
        throw ApprovalHostError.malformedChallenge
    default:
        throw ApprovalHostError.malformedChallenge
    }
}

private func validateGa4DeclaredFields(_ value: CanonicalJSONValue) throws -> [String] {
    guard case .array(let fields) = value, fields.count <= 25 else { throw ApprovalHostError.malformedChallenge }
    let names = try fields.map { item -> String in
        guard case .string(let name) = item,
              ga4SafeEventName(name)
        else { throw ApprovalHostError.malformedChallenge }
        return name
    }
    guard Set(names).count == names.count else { throw ApprovalHostError.malformedChallenge }
    return names
}

private func validateGa4ProspectiveEffects(_ value: CanonicalJSONValue) throws {
    let object = try ClosedObject(value, exactly: [
        "configuration_surface", "applies_to", "historical_backfill", "native_conversion_mapping", "browser_or_receipt_proven",
    ])
    guard try object.string("configuration_surface") == "LIVE_CONFIGURATION_WRITE",
          try object.string("applies_to") == "FUTURE_EVENT_OCCURRENCES",
          try object.bool("historical_backfill") == false,
          try object.string("native_conversion_mapping") == "NONE_CREATED",
          try object.bool("browser_or_receipt_proven") == false
    else { throw ApprovalHostError.malformedChallenge }
}

private func validateGa4PlanningBoundary(_ value: CanonicalJSONValue) throws {
    let keys = [
        "requires_verified_live_property_and_stream_identity", "requires_verified_event_collection_and_reserved_name_check",
        "requires_existing_key_event_inventory", "requires_immutable_event_name_and_duplicate_check",
        "requires_pre_apply_drift_recheck", "requires_semantics_privacy_and_consent_validation",
        "requires_exact_authorization", "requires_configuration_readback", "requires_browser_network_verification",
        "requires_realtime_verification", "requires_processed_report_verification",
        "requires_separate_native_conversion_mapping",
    ]
    let object = try ClosedObject(value, exactly: ["status"] + keys)
    guard try object.string("status") == "OFFLINE_CANDIDATE_NOT_EXECUTABLE" else {
        throw ApprovalHostError.malformedChallenge
    }
    for key in keys {
        guard try object.bool(key) else { throw ApprovalHostError.malformedChallenge }
    }
}

private func validateGa4ConversionBinding(_ value: CanonicalJSONValue) throws -> [String] {
    let object = try ClosedObject(value, exactly: [
        "schema_version", "conversion_contract_record_digest", "contract_id", "content_digest", "contract_version",
        "contract_effective_from", "declared_destination_profile_ref", "consent_contract_ref", "bound_boolean_fields",
        "counting_binding", "value_binding", "semantic_accuracy", "source_event_mapping", "effective_time_status",
    ])
    guard try object.integer("schema_version") == 1,
          ga4Digest(try object.string("conversion_contract_record_digest")),
          ga4Digest(try object.string("content_digest")),
          try object.integer("contract_version") >= 1,
          ga4SafeReference(try object.string("contract_id")),
          ga4SafeReference(try object.string("declared_destination_profile_ref")),
          ga4SafeReference(try object.string("consent_contract_ref")),
          try object.string("counting_binding") == "EACH_QUALIFYING_OCCURRENCE_TO_ONCE_PER_EVENT",
          try object.string("value_binding") == "NO_MONETARY_VALUE_TO_NO_DEFAULT_VALUE",
          try object.string("semantic_accuracy") == "NOT_VERIFIED",
          try object.string("source_event_mapping") == "NOT_PROVEN",
          try object.string("effective_time_status") == "DECLARED_NOT_EVALUATED"
    else { throw ApprovalHostError.malformedChallenge }
    _ = try ClosedTimestamp.parse(try object.string("contract_effective_from"))
    guard case .array(let fields) = try object.value("bound_boolean_fields"), fields.count <= 25 else {
        throw ApprovalHostError.malformedChallenge
    }
    var names = Set<String>()
    var orderedNames: [String] = []
    for item in fields {
        let field = try ClosedObject(item, exactly: ["name", "event_value_type", "event_required"])
        let name = try field.boundedString("name", maximumBytes: 40)
        _ = try field.bool("event_required")
        guard ga4SafeEventName(name),
              try field.string("event_value_type") == "BOOLEAN",
              names.insert(name).inserted
        else { throw ApprovalHostError.malformedChallenge }
        orderedNames.append(name)
    }
    return orderedNames
}
