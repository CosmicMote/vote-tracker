"""Congress.gov API v3: auth, JSON format, offset paging. Returns payloads as received."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterator

from .http import HttpClient, redact

BASE_URL = "https://api.congress.gov/v3"
PAGE_LIMIT = 250


@dataclass
class Fetched:
    """A response body as received, with its key-stripped URL."""

    url: str
    text: str

    def json(self) -> Any:
        return json.loads(self.text)


class CongressGovClient:
    def __init__(self, http: HttpClient, api_key: str | None, base_url: str = BASE_URL):
        if not api_key:
            raise ValueError("CONGRESS_GOV_API_KEY is not set")
        self.http = http
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    def get(self, path: str, **params: Any) -> Fetched:
        url = f"{self.base_url}/{path.lstrip('/')}"
        resp = self.http.get(url, params={"format": "json", **params, "api_key": self.api_key})
        return Fetched(url=redact(str(resp.request.url)), text=resp.text)

    def pages(self, path: str, **params: Any) -> Iterator[tuple[int, Fetched]]:
        """Yield (offset, page) until the API stops returning a `next` link."""
        offset = 0
        while True:
            page = self.get(path, offset=offset, limit=PAGE_LIMIT, **params)
            yield offset, page
            if not page.json().get("pagination", {}).get("next"):
                return
            offset += PAGE_LIMIT

    # Endpoints ---------------------------------------------------------------

    def member(self, bioguide: str) -> Fetched:
        return self.get(f"member/{bioguide}")

    def sponsored(self, bioguide: str) -> Iterator[tuple[int, Fetched]]:
        return self.pages(f"member/{bioguide}/sponsored-legislation")

    def cosponsored(self, bioguide: str) -> Iterator[tuple[int, Fetched]]:
        return self.pages(f"member/{bioguide}/cosponsored-legislation")

    def house_votes(self, congress: int, session: int) -> Iterator[tuple[int, Fetched]]:
        return self.pages(f"house-vote/{congress}/{session}")

    def house_vote(self, congress: int, session: int, roll: int) -> Fetched:
        return self.get(f"house-vote/{congress}/{session}/{roll}")

    def house_vote_members(self, congress: int, session: int, roll: int) -> Fetched:
        return self.get(f"house-vote/{congress}/{session}/{roll}/members", limit=PAGE_LIMIT * 2)

    def bill(self, congress: int | str, bill_type: str, number: str) -> Fetched:
        return self.get(f"bill/{congress}/{bill_type.lower()}/{number}")

    def bill_summaries(self, congress: int | str, bill_type: str, number: str) -> Fetched:
        return self.get(f"bill/{congress}/{bill_type.lower()}/{number}/summaries")

    def bill_text(self, congress: int | str, bill_type: str, number: str) -> Fetched:
        return self.get(f"bill/{congress}/{bill_type.lower()}/{number}/text")

    def bill_cosponsors(self, congress: int | str, bill_type: str, number: str) -> Iterator[tuple[int, Fetched]]:
        return self.pages(f"bill/{congress}/{bill_type.lower()}/{number}/cosponsors")

    def amendment(self, congress: int | str, amdt_type: str, number: str) -> Fetched:
        return self.get(f"amendment/{congress}/{amdt_type.lower()}/{number}")

    def fetch_url(self, url: str) -> Fetched:
        """Fetch a document linked from an API response (e.g. formatted bill text)."""
        resp = self.http.get(url)
        return Fetched(url=redact(str(resp.request.url)), text=resp.text)
