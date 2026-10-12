# All-provider candidate-history independent review

This review covers the retained LinkedIn Ads, Reddit Ads, and TikTok Ads
candidate variants added alongside the existing OpenAI Ads, Meta Ads, and
Google Ads paths. It exercised only local compiler, ledger, CLI, checkpoint,
and export boundaries. It made no provider request, credential-store access,
or remote mutation.

The review initially reproduced an ambient-`Decimal` defect: setting process
precision to one caused LinkedIn candidate compilation to fail and TikTok
candidate compilation to raise `InvalidOperation`. The native-money helpers
now render and recover USD cents with integer/digit operations, retaining the
same normal-context wire values. The independent regression uses precision one
with `Inexact` and `Rounded` traps enabled, then retains and reloads each
candidate.

The independent checks also verify that:

- LinkedIn and TikTok retain the exact JCS-safe USD upper bound as
  `90071992547409.91`; Reddit retains the corresponding safe micros boundary.
  Constructed Money values one unit beyond each provider-safe limit fail during
  recompilation.
- Each new `compile --retain` command rejects raw JSON float and boolean money
  values before a sharing-key read or ledger creation.
- Rehashed outer records containing a constructed nested planning boundary with
  a false required check fail before append.
- A small-segment verified ledger rotates and still loads/catalogs all three new
  variants with their digest-bound provenance.

The stored candidate is a non-executable compilation review. It does not prove
current provider identity, eligibility, media, conversion, destination, or
creative history, and it grants no execution or activation authority. Those
conditions remain explicit in each retained planning boundary and require a new
compile, current reads, drift checks, and a separately authorized plan.
