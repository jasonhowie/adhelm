"""Independent input and all-page failure checks for fixed GA4 identity projections."""

import httpx
import pytest
from test_ga4_identity_projection import _client, _page, _property, _stream

from adhelm.errors import AdhelmError


@pytest.mark.parametrize(
    ("invalid", "target"),
    [
        (value, target)
        for value in ("١٢٣", "１２３", "123\n", 123, None, True)
        for target in ("property", "stream")
        if not (value is None and target == "stream")
    ],
)
def test_native_ids_reject_before_credential_or_http(invalid: object, target: str) -> None:
    client = _client(
        lambda _request: pytest.fail("Invalid target reached HTTP"),
        token_getter=lambda: pytest.fail("Invalid target reached credentials"),
    )
    with pytest.raises(AdhelmError) as error:
        client.discover_identity(
            invalid if target == "property" else "123",
            expected_web_stream_id=invalid if target == "stream" else "456",
        )
    assert error.value.code == "INVALID_GA4_PROPERTY"


def test_later_wrong_parent_in_unselected_app_stream_invalidates_selected_web_stream() -> None:
    pages: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/123"):
            return httpx.Response(200, json=_property())
        pages.append(request.url.params.get("pageToken"))
        if len(pages) == 1:
            return httpx.Response(200, json=_page([_stream("456")], next_token="later"))
        return httpx.Response(
            200,
            json=_page(
                [_stream("900", "ANDROID_APP_DATA_STREAM", name="properties/999/dataStreams/900")]
            ),
        )

    with pytest.raises(AdhelmError) as error:
        _client(handler).discover_identity("123", expected_web_stream_id="456")
    assert error.value.code == "PROVIDER_RESPONSE_INVALID"
    assert pages == [None, "later"]
