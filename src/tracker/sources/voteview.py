"""Voteview (UCLA) per-Congress Senate roll-call files: an alternative to senate.gov's XML.

Three CSVs per Congress, each re-downloaded whole: roll calls (question, result, counts,
majority requirement, bill), each member's position per roll call, and members (ICPSR ID
to Bioguide ID and party). https://voteview.com/articles/data_help_rollcalls
"""

from __future__ import annotations

from .congress_gov import Fetched
from .http import HttpClient

FILES = {
    "voteview_rollcalls": "rollcalls/S{congress}_rollcalls.csv",
    "voteview_votes": "votes/S{congress}_votes.csv",
    "voteview_members": "members/S{congress}_members.csv",
}


class VoteviewClient:
    def __init__(self, http: HttpClient, base_url: str):
        self.http = http
        self.base_url = base_url.rstrip("/")

    def fetch(self, kind: str, congress: int) -> Fetched:
        url = f"{self.base_url}/{FILES[kind].format(congress=congress)}"
        return Fetched(url=url, text=self.http.get(url).text)
