"""Open-Meteo weather tools.

Port of infrastructure/gateway/open_meteo_lambda.py: same two tools the
AgentCore Gateway exposed (searchLocation, getForecast), as local Strands
tools. Phase 3 of docs/opensource-migration-plan.md moves these behind a
self-hosted MCP server; the HTTP logic stays identical.
"""

import json
import urllib.parse
import urllib.request

from strands import tool

# Fields that bloat geocoding results without helping the model pick coordinates.
# postcodes alone can be 200+ entries for a single city.
_LOCATION_DROP = {"postcodes", "admin1_id", "admin2_id", "admin3_id", "admin4_id", "country_id"}


def _fetch(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=20) as response:
        return json.loads(response.read().decode())


def _clean_location(result: dict) -> dict:
    return {k: v for k, v in result.items() if k not in _LOCATION_DROP}


@tool
def searchLocation(name: str, count: int = 3, language: str = "en") -> str:
    """Resolve a city name to latitude/longitude candidates.

    Args:
        name: City name in English or the local language (not French exonyms).
        count: Maximum number of candidate locations to return.
        language: Language for the result labels.
    """
    params = {"name": name, "count": count, "language": language}
    url = "https://geocoding-api.open-meteo.com/v1/search?" + urllib.parse.urlencode(params)
    data = _fetch(url)
    if "results" in data:
        data = {"results": [_clean_location(r) for r in data["results"]]}
    return json.dumps(data)


@tool
def getForecast(
    latitude: float,
    longitude: float,
    current_weather: bool = True,
    hourly: str = "",
    daily: str = "",
    forecast_days: int = 1,
    timezone: str = "auto",
) -> str:
    """Current weather and forecast for coordinates.

    Args:
        latitude: Latitude from searchLocation.
        longitude: Longitude from searchLocation.
        current_weather: Include current temperature, wind, and conditions.
        hourly: Comma-separated hourly variables, e.g.
            "relativehumidity_2m,precipitation_probability".
        daily: Comma-separated daily variables for multi-day questions, e.g.
            "temperature_2m_max,temperature_2m_min,precipitation_sum,weathercode".
        forecast_days: Number of forecast days (1 = today only).
        timezone: IANA timezone or "auto" for local times.
    """
    params: dict = {
        "latitude": latitude,
        "longitude": longitude,
        "current_weather": current_weather,
        "forecast_days": forecast_days,
        "timezone": timezone,
    }
    if hourly:
        params["hourly"] = hourly
    if daily:
        params["daily"] = daily
    url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(params, doseq=True)
    return json.dumps(_fetch(url))
