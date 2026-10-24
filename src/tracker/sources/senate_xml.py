"""senate.gov roll-call XML: the per-session vote menu and one file per vote."""

from __future__ import annotations

from .congress_gov import Fetched
from .http import HttpClient

MENU_URL = "https://www.senate.gov/legislative/LIS/roll_call_lists/vote_menu_{congress}_{session}.xml"
VOTE_URL = (
    "https://www.senate.gov/legislative/LIS/roll_call_votes/vote{congress}{session}/"
    "vote_{congress}_{session}_{roll:05d}.xml"
)
VOTE_PAGE_URL = (
    "https://www.senate.gov/legislative/LIS/roll_call_votes/vote{congress}{session}/"
    "vote_{congress}_{session}_{roll:05d}.htm"
)


def vote_url(congress: int, session: int, roll: int) -> str:
    return VOTE_URL.format(congress=congress, session=session, roll=roll)


def vote_page_url(congress: int, session: int, roll: int) -> str:
    return VOTE_PAGE_URL.format(congress=congress, session=session, roll=roll)


class SenateClient:
    def __init__(self, http: HttpClient):
        self.http = http

    def vote_menu(self, congress: int, session: int) -> Fetched:
        url = MENU_URL.format(congress=congress, session=session)
        return Fetched(url=url, text=self.http.get(url).text)

    def vote(self, congress: int, session: int, roll: int) -> Fetched:
        url = vote_url(congress, session, roll)
        return Fetched(url=url, text=self.http.get(url).text)
