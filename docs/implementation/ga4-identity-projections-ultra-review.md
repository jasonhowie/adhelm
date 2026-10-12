# GA4 identity projection independent review

The legacy `Ga4DataClient.discover_identity` repair requests only the fixed
property identity fields and paginated stream name/type fields. It retains the
existing `Ga4Identity` wire shape and rejects unrequested fields instead of
relaxing the shared privacy filter. The ordinary `webStreamData.defaultUri`
surface is no longer requested.

Independent review found an additional input defect: the existing native-ID
helper used `str.isdecimal()`, which accepts non-ASCII digit characters. Those
inputs could reach credential lookup and provider egress before later identity
validation. `_property_id` now requires an actual string of 1–20 ASCII digits;
malformed, nonstring, newline and non-ASCII property/explicit-stream inputs
fail before token access. An omitted expected stream remains the supported
automatic unique-web-stream selection case.

The independent tests also confirm that finding the selected web stream on an
early page does not terminate inventory validation: a later unselected app
stream with a foreign parent invalidates the observation. Complete bounded
pagination, duplicate IDs and token cycles are still checked before any identity
result. A current fixed read is sequential and does not establish mutation
authority, browser firing or processed receipt.

Validation covered 53 focused GA4 identity and measurement tests, including
12 new independent cases. Tests use synthetic HTTP and token seams; no account,
OS credential store or network was accessed. Live partial-response conformance
remains required. The provider method documents omission of `nextPageToken` as
the terminal condition; no empty-string fallback was introduced.
[Official stream list contract](https://developers.google.com/analytics/devguides/config/admin/v1/rest/v1beta/properties.dataStreams/list).
