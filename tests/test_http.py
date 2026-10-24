import httpx
import pytest
import respx

from tracker.sources.congress_gov import CongressGovClient
from tracker.sources.http import HttpClient, HttpError, RetryableHttpError, redact


class FakeClock:
    def __init__(self):
        self.t = 0.0
        self.sleeps: list[float] = []

    def sleep(self, s: float) -> None:
        self.sleeps.append(round(s, 3))
        self.t += s

    def __call__(self) -> float:
        return self.t


def client(clock, **kw) -> HttpClient:
    return HttpClient("vote-tracker-test/0.1 (test@example.com)", sleep=clock.sleep, clock=clock, **kw)


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://api.congress.gov/v3/x?format=json&api_key=SECRET&limit=1", "https://api.congress.gov/v3/x?format=json&limit=1"),
        ("https://api.congress.gov/v3/x?api_key=SECRET&format=json", "https://api.congress.gov/v3/x?format=json"),
        ("https://api.congress.gov/v3/x?format=json&api_key=SECRET", "https://api.congress.gov/v3/x?format=json"),
        ("https://api.congress.gov/v3/x?api_key=SECRET", "https://api.congress.gov/v3/x"),
    ],
)
def test_redact(url, expected):
    assert redact(url) == expected


@respx.mock
def test_sends_user_agent_and_retries_429_honoring_retry_after():
    clock = FakeClock()
    route = respx.get("https://example.test/a").mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200, text="ok")]
    )
    with client(clock, min_interval_seconds=0) as c:
        assert c.get("https://example.test/a").text == "ok"
    assert route.call_count == 2
    assert route.calls[0].request.headers["User-Agent"].startswith("vote-tracker-test/0.1")
    assert 7.0 in clock.sleeps


@respx.mock
def test_retries_5xx_with_backoff_then_gives_up():
    clock = FakeClock()
    route = respx.get("https://example.test/b").mock(return_value=httpx.Response(503))
    with client(clock, min_interval_seconds=0, max_attempts=3) as c, pytest.raises(RetryableHttpError):
        c.get("https://example.test/b")
    assert route.call_count == 3
    assert clock.sleeps == [2.0, 4.0]


@respx.mock
def test_does_not_retry_other_4xx():
    clock = FakeClock()
    route = respx.get("https://example.test/c").mock(return_value=httpx.Response(403))
    with client(clock, min_interval_seconds=0) as c, pytest.raises(HttpError) as exc:
        c.get("https://example.test/c")
    assert route.call_count == 1 and exc.value.status == 403


@respx.mock
def test_throttles_per_host():
    clock = FakeClock()
    respx.get(url__regex=r"https://(one|two)\.test/.*").mock(return_value=httpx.Response(200))
    with client(clock, min_interval_seconds=1.0) as c:
        c.get("https://one.test/1")
        c.get("https://two.test/1")  # different host: no wait
        c.get("https://one.test/2")  # same host: waits out the interval
    assert clock.sleeps == [1.0]


@respx.mock
def test_congress_gov_never_exposes_key():
    clock = FakeClock()
    respx.get("https://api.congress.gov/v3/bill/119/hr/1").mock(
        return_value=httpx.Response(500)
    )
    with client(clock, min_interval_seconds=0, max_attempts=1) as c:
        cg = CongressGovClient(c, "SECRET-KEY")
        with pytest.raises(HttpError) as exc:
            cg.bill(119, "hr", "1")
    assert "SECRET-KEY" not in str(exc.value) and "SECRET-KEY" not in (exc.value.url or "")


@respx.mock
def test_congress_gov_pages_until_no_next():
    clock = FakeClock()
    route = respx.get("https://api.congress.gov/v3/member/X/sponsored-legislation").mock(
        side_effect=[
            httpx.Response(200, json={"sponsoredLegislation": [1], "pagination": {"next": "https://..."}}),
            httpx.Response(200, json={"sponsoredLegislation": [2], "pagination": {}}),
        ]
    )
    with client(clock, min_interval_seconds=0) as c:
        pages = list(CongressGovClient(c, "K").sponsored("X"))
    assert [o for o, _ in pages] == [0, 250]
    assert route.calls[1].request.url.params["offset"] == "250"
    assert all("api_key" not in p.url for _, p in pages)
