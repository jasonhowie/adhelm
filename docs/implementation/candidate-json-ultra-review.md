# Candidate history strict JSON review

Review reproduced a numeric-type laundering defect at the nested retained
candidate boundary: RFC 8785 canonicalization converted an integral floating
budget to an integer before strict validation, leaving the same candidate
digest. The record now checks representability while preserving original JSON
number types for the provider-specific validator. Supplied literal flags and
versions must also preserve exact JSON scalar types; serialization warnings
from malformed constructed models cannot print their representations.

The independent regressions cover all three retained compiler variants,
floating budgets and versions, integer substitutes for boolean safety flags,
no partial history, constructed-model warnings, and unchanged valid review
serialization. Combined candidate-history validation passed 66 tests. This is
local fixture evidence; candidate records still grant no execution authority.
