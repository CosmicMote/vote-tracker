"""Census geocoder lookups from recorded responses: the vintage carrying the configured Congress's
district map is found, at-large and delegate seats map to district 0, and points outside the US
find nothing."""

import httpx
import pytest
import respx

from conftest import fixture_text
from tracker.sources import census
from tracker.sources.http import HttpClient


@pytest.mark.parametrize("n,expected", [(119, "119th"), (120, "120th"), (121, "121st"), (122, "122nd"),
                                        (123, "123rd"), (111, "111th"), (112, "112th")])
def test_ordinal(n, expected):
    assert census.ordinal(n) == expected


@pytest.mark.parametrize("geoid,expected", [("0801", "1"), ("4837", "37"), ("5600", "0"), ("1198", "0")])
def test_district_code(geoid, expected):
    assert census.district_code(geoid) == expected


def respond(request: httpx.Request) -> httpx.Response:
    name = {"Current_Current": "austin_current.json", "ACS2025_Current": "austin_acs2025.json"}
    return httpx.Response(200, text=fixture_text("census", name[request.url.params["vintage"]]))


@respx.mock
def test_falls_back_to_the_vintage_with_the_configured_congress():
    # Austin is TX-37 in the 119th Congress and TX-10 in the 120th; "Current" already has the 120th.
    route = respx.get(census.URL).mock(side_effect=respond)
    with HttpClient("test", min_interval_seconds=0) as http:
        loc = census.locate(http, 30.26721, -97.74312, 119)
    assert loc == census.Location(state="TX", state_name="Texas", district="37")
    assert [c.request.url.params["vintage"] for c in route.calls] == ["Current_Current", "ACS2025_Current"]
    params = route.calls[0].request.url.params
    assert params["layers"] == "States,119th Congressional Districts"
    assert (params["x"], params["y"]) == ("-97.7431", "30.2672")  # rounded to about 10 m


@respx.mock
def test_outside_the_us_finds_nothing():
    route = respx.get(census.URL).mock(return_value=httpx.Response(200, text=fixture_text("census", "ocean.json")))
    with HttpClient("test", min_interval_seconds=0) as http:
        assert census.locate(http, 30, -40, 119) is None
    assert route.call_count == 1


@respx.mock
def test_state_without_a_district_when_no_vintage_has_the_map():
    respx.get(census.URL).mock(return_value=httpx.Response(200, text=fixture_text("census", "austin_acs2025.json")))
    with HttpClient("test", min_interval_seconds=0) as http:
        loc = census.locate(http, 30.2672, -97.7431, 125)
    assert loc == census.Location(state="TX", state_name="Texas", district=None)
