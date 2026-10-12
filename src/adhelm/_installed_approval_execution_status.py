"""Fixed availability facts for the execution commands."""

from typing import Final

from adhelm.release_policy import UNRELEASED_STATUS

# The commands are registered, but the 0.1.0 release policy refuses every one of
# them (`adhelm.release_policy`), and the ordinary source/wheel binding remains
# deliberately unconfigured besides. This is not a claim that native
# installation, signing, enrollment, or live provider execution has been
# validated.
INSTALLED_APPROVAL_EXECUTION_COMMAND_STATUS: Final = UNRELEASED_STATUS
INSTALLED_APPROVAL_EXECUTION_CAPABILITY_STATUS: Final = "DEVELOPMENT_ONLY_NOT_LIVE_VALIDATED"

# Google Ads campaign PAUSE executes through the provider-neutral `execute plan`
# leaf: P03 registered its planner and guarded adapter for ("google_ads", PAUSE),
# so the pair is no longer unimplemented. It is still not usable in an ordinary
# build, because the installed approval bridge refuses COMPOSITE_EXECUTION_PLAN,
# so it shares the generic leaves' status string rather than inventing another
# one. Published artifacts and `discover` read it from this one place so a
# future change cannot leave either stale.
GOOGLE_ADS_PAUSE_EXECUTION_COMMAND_STATUS: Final = INSTALLED_APPROVAL_EXECUTION_COMMAND_STATUS

# The generic provider-neutral leaves. `execute plan` and `recover attempt` are
# registered and refused by the same release-policy gate as the three fixed
# leaves, so they share its status string rather than inventing a second one.
# They additionally require a registered execution adapter, which `discover`
# renders from `CONFIGURED_EXECUTION_ADAPTERS` itself rather than from a
# constant here.
GENERIC_EXECUTION_COMMAND_STATUS: Final = INSTALLED_APPROVAL_EXECUTION_COMMAND_STATUS
ATTEMPT_RECOVERY_COMMAND_STATUS: Final = INSTALLED_APPROVAL_EXECUTION_COMMAND_STATUS


# Google Ads paused Search hierarchy CREATE executes through the same
# provider-neutral `execute plan` leaf: P04 registered its planner and guarded
# adapter for ("google_ads", CREATE), so the pair is no longer unimplemented. It
# is still not usable in an ordinary build, because the installed approval
# bridge refuses COMPOSITE_EXECUTION_PLAN, so it shares the generic leaves'
# status string rather than inventing another one.  (The ledger now registers
# the two additive Google Ads CREATE record kinds, so that is no longer one of
# the reasons it refuses.)
GOOGLE_ADS_CREATE_EXECUTION_COMMAND_STATUS: Final = INSTALLED_APPROVAL_EXECUTION_COMMAND_STATUS

# OpenAI Ads paused chat-card hierarchy CREATE executes through the same
# provider-neutral `execute plan` leaf: P04-OA registered its planner and
# journaled child-egress adapter for ("openai_ads", CREATE), the ledger
# registers its two additive record kinds, and the transport carries the one
# bounded multipart image upload it needs.  The pair is therefore no longer
# unimplemented, and it is still not usable in an ordinary build, because the
# installed approval bridge refuses COMPOSITE_EXECUTION_PLAN.  It shares the
# generic leaves' status string rather than inventing another one, and the two
# build checks stay in the planner and the adapter factory so a build without
# either dependency refuses before any credential.
OPENAI_ADS_CREATE_EXECUTION_COMMAND_STATUS: Final = INSTALLED_APPROVAL_EXECUTION_COMMAND_STATUS

# Google Ads Search hierarchy ACTIVATE and the guarded maintenance operations
# execute through the same provider-neutral `execute plan` leaf: P05 registered
# their planners and guarded adapters for ("google_ads", ACTIVATE) and
# ("google_ads", UPDATE), so those pairs are no longer unimplemented. The
# delete-class ("google_ads", ARCHIVE) pair is deliberately not registered until
# a cited reference read can supply AGENTS rule 11's dependency proof. The
# registered pairs are still not usable in an ordinary build, and the only
# reason is that the installed approval bridge refuses COMPOSITE_EXECUTION_PLAN;
# a ledger that did not register the four additive ACTIVATE and MAINTAIN record
# kinds would be a further, conditional refusal, but this build's ledger
# registers all four. They share the generic leaves' status string rather than
# inventing another one. Activation additionally remains a separate,
# spend-explicit, digest-bound authorization: nothing here makes it automatic.
GOOGLE_ADS_ACTIVATE_EXECUTION_COMMAND_STATUS: Final = INSTALLED_APPROVAL_EXECUTION_COMMAND_STATUS
GOOGLE_ADS_MAINTAIN_EXECUTION_COMMAND_STATUS: Final = INSTALLED_APPROVAL_EXECUTION_COMMAND_STATUS
