"""US Census Bureau geocoder: the state and congressional district at a point.

https://geocoding.geo.census.gov/geocoder/ needs no API key. Each vintage carries the district
map of one Congress ("119th Congressional Districts"), and "Current" moves to the next Congress's
map before that Congress is seated, so vintages are tried until one has the configured Congress.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .http import HttpClient

log = logging.getLogger(__name__)

URL = "https://geocoding.geo.census.gov/geocoder/geographies/coordinates"
VINTAGES = ("Current_Current", "ACS2025_Current", "ACS2024_Current")
# District codes for a single seat: 00 at large, 98 for DC's and the territories' delegates.
AT_LARGE = {"00", "98"}


@dataclass(frozen=True)
class Location:
    state: str  # postal code, e.g. "CO"
    state_name: str
    district: str | None  # as the crosswalk has it: "1", "0" for at large; None if not found


def ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def district_code(geoid: str) -> str:
    """'0801' -> '1', '5600' -> '0' (at large), '1198' -> '0' (DC delegate)."""
    code = geoid[2:]
    return "0" if code in AT_LARGE else str(int(code))


def parse(payload: dict[str, Any], layer: str) -> Location | None:
    geos = (payload.get("result") or {}).get("geographies") or {}
    states = geos.get("States") or []
    if not states:
        return None
    districts = geos.get(layer) or []
    return Location(
        state=states[0]["STUSAB"],
        state_name=states[0]["NAME"],
        district=district_code(districts[0]["GEOID"]) if districts else None,
    )


def locate(http: HttpClient, lat: float, lon: float, congress: int) -> Location | None:
    """The state and district at a point, or None outside the US. The district is None when no
    vintage has the map for `congress`."""
    layer = f"{ordinal(congress)} Congressional Districts"
    found: Location | None = None
    for vintage in VINTAGES:
        params = {
            "x": f"{lon:.4f}",
            "y": f"{lat:.4f}",
            "benchmark": "Public_AR_Current",
            "vintage": vintage,
            "layers": f"States,{layer}",
            "format": "json",
        }
        loc = parse(http.get(URL, params).json(), layer)
        if loc is None:
            return None
        if loc.district is not None:
            return loc
        found = found or loc
        log.info("census vintage %s has no %s; trying an older one", vintage, layer)
    return found
