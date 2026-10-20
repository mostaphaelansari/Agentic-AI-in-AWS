from __future__ import annotations

import sys
from pathlib import Path

import mcp.client.streamable_http
import strands.tools.mcp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "opensource" / "agents" / "custom"))
import service  # noqa: E402


def test_booking_mcp_config_uses_headers_only_when_key_is_present(monkeypatch):
    monkeypatch.setattr(service, "MCP_URL", "http://mcp-tools:8000/mcp")
    monkeypatch.setattr(service, "BOOKING_MCP_URL", "https://mcp.rapidapi.com")
    monkeypatch.setattr(service, "RAPIDAPI_HOST", "booking.example")
    monkeypatch.setattr(service, "RAPIDAPI_KEY", "test-secret")
    monkeypatch.setattr(service, "AERODATABOX_MCP_URL", "")
    monkeypatch.setattr(service, "AIR_SCRAPER_MCP_URL", "")
    monkeypatch.setattr(service, "GEOAPIFY_MCP_URL", "")

    assert service._mcp_server_configs() == [
        ("local", "http://mcp-tools:8000/mcp", None),
        (
            "booking",
            "https://mcp.rapidapi.com",
            {"x-api-host": "booking.example", "x-api-key": "test-secret"},
        ),
    ]


def test_booking_mcp_is_disabled_without_key(monkeypatch):
    monkeypatch.setattr(service, "MCP_URL", "")
    monkeypatch.setattr(service, "BOOKING_MCP_URL", "https://mcp.rapidapi.com")
    monkeypatch.setattr(service, "RAPIDAPI_KEY", "")
    monkeypatch.setattr(service, "AERODATABOX_MCP_URL", "")
    monkeypatch.setattr(service, "AIR_SCRAPER_MCP_URL", "")
    monkeypatch.setattr(service, "GEOAPIFY_MCP_URL", "")

    assert service._mcp_server_configs() == []


def test_aerodatabox_mcp_config_reuses_rapidapi_key(monkeypatch):
    monkeypatch.setattr(service, "MCP_URL", "")
    monkeypatch.setattr(service, "BOOKING_MCP_URL", "")
    monkeypatch.setattr(service, "RAPIDAPI_KEY", "shared-secret")
    monkeypatch.setattr(service, "AERODATABOX_MCP_URL", "https://mcp.rapidapi.com")
    monkeypatch.setattr(service, "AERODATABOX_RAPIDAPI_HOST", "aerodatabox.example")
    monkeypatch.setattr(service, "AERODATABOX_RAPIDAPI_KEY", "shared-secret")
    monkeypatch.setattr(service, "AIR_SCRAPER_MCP_URL", "")
    monkeypatch.setattr(service, "GEOAPIFY_MCP_URL", "")

    assert service._mcp_server_configs() == [
        (
            "aerodatabox",
            "https://mcp.rapidapi.com",
            {"x-api-host": "aerodatabox.example", "x-api-key": "shared-secret"},
        ),
    ]


def test_air_scraper_mcp_config_reuses_rapidapi_key(monkeypatch):
    monkeypatch.setattr(service, "MCP_URL", "")
    monkeypatch.setattr(service, "BOOKING_MCP_URL", "")
    monkeypatch.setattr(service, "RAPIDAPI_KEY", "shared-secret")
    monkeypatch.setattr(service, "AERODATABOX_MCP_URL", "")
    monkeypatch.setattr(service, "AIR_SCRAPER_MCP_URL", "https://mcp.rapidapi.com")
    monkeypatch.setattr(service, "AIR_SCRAPER_RAPIDAPI_HOST", "sky.example")
    monkeypatch.setattr(service, "AIR_SCRAPER_RAPIDAPI_KEY", "shared-secret")
    monkeypatch.setattr(service, "GEOAPIFY_MCP_URL", "")

    assert service._mcp_server_configs() == [
        (
            "air-scraper",
            "https://mcp.rapidapi.com",
            {"x-api-host": "sky.example", "x-api-key": "shared-secret"},
        ),
    ]


def test_geoapify_mcp_config_uses_api_key_header(monkeypatch):
    monkeypatch.setattr(service, "MCP_URL", "")
    monkeypatch.setattr(service, "BOOKING_MCP_URL", "")
    monkeypatch.setattr(service, "RAPIDAPI_KEY", "")
    monkeypatch.setattr(service, "AERODATABOX_MCP_URL", "")
    monkeypatch.setattr(service, "AIR_SCRAPER_MCP_URL", "")
    monkeypatch.setattr(service, "GEOAPIFY_MCP_URL", "https://api.geoapify.com/v1/mcp")
    monkeypatch.setattr(service, "GEOAPIFY_API_KEY", "geo-secret")

    assert service._mcp_server_configs() == [
        (
            "geoapify",
            "https://api.geoapify.com/v1/mcp",
            {"x-api-key": "geo-secret"},
        ),
    ]


def test_multiple_mcp_servers_combine_tools_and_headers(monkeypatch):
    transports = []

    def fake_transport(url, headers=None):
        transports.append((url, headers))
        return url, headers

    class FakeClient:
        def __init__(self, transport_factory):
            self.transport_factory = transport_factory
            self.transport = None

        def start(self):
            self.transport = self.transport_factory()

        def list_tools_sync(self):
            return [f"tool:{self.transport[0]}"]

        def stop(self, *_args):
            pass

    monkeypatch.setattr(
        service,
        "_mcp_server_configs",
        lambda: [
            ("local", "http://local/mcp", None),
            ("booking", "https://booking/mcp", {"x-api-key": "test-secret"}),
        ],
    )
    monkeypatch.setattr(mcp.client.streamable_http, "streamablehttp_client", fake_transport)
    monkeypatch.setattr(strands.tools.mcp, "MCPClient", FakeClient)

    clients, discovered = service._start_mcp_client()

    assert len(clients) == 2
    assert discovered == ["tool:http://local/mcp", "tool:https://booking/mcp"]
    assert transports == [
        ("http://local/mcp", None),
        ("https://booking/mcp", {"x-api-key": "test-secret"}),
    ]


AGENTCORE_URL = (
    "https://bedrock-agentcore.eu-central-1.amazonaws.com/runtimes/"
    "arn%3Aaws%3Abedrock-agentcore%3Aeu-central-1%3A123%3Aruntime%2F"
    "opensource_mcp_tools-abc/invocations?qualifier=DEFAULT"
)


def test_agentcore_region_detection():
    assert service._agentcore_region(AGENTCORE_URL) == "eu-central-1"
    assert service._agentcore_region("http://localhost:8000/mcp") is None
    assert service._agentcore_region("https://mcp.rapidapi.com") is None


def test_transport_factory_selects_sigv4_for_agentcore_url(monkeypatch):
    monkeypatch.setattr(service, "MCP_URL_AUTH", "auto")
    assert service._mcp_transport_factory(AGENTCORE_URL, None).transport == "sigv4"
    assert (
        service._mcp_transport_factory("http://localhost:8000/mcp", None).transport
        == "streamable_http"
    )


def test_transport_factory_honours_explicit_auth_setting(monkeypatch):
    monkeypatch.setattr(service, "MCP_URL_AUTH", "none")
    assert service._mcp_transport_factory(AGENTCORE_URL, None).transport == "streamable_http"
    monkeypatch.setattr(service, "MCP_URL_AUTH", "sigv4")
    assert service._mcp_transport_factory("https://example.com/mcp", None).transport == "sigv4"


def test_start_mcp_client_uses_sigv4_transport_for_agentcore_url(monkeypatch):
    captured = []

    class FakeClient:
        def __init__(self, transport_factory):
            captured.append(getattr(transport_factory, "transport", "streamable_http"))

        def start(self):
            pass

        def list_tools_sync(self):
            return ["tool"]

        def stop(self, *_args):
            pass

    monkeypatch.setattr(service, "MCP_URL_AUTH", "auto")
    monkeypatch.setattr(
        service,
        "_mcp_server_configs",
        lambda: [
            ("local", AGENTCORE_URL, None),
            ("geoapify", "https://api.geoapify.com/v1/mcp", {"x-api-key": "k"}),
        ],
    )
    monkeypatch.setattr(strands.tools.mcp, "MCPClient", FakeClient)

    clients, discovered = service._start_mcp_client()

    assert len(clients) == 2
    assert discovered == ["tool", "tool"]
    assert captured == ["sigv4", "streamable_http"]


def test_booking_prompt_filters_large_remote_catalog():
    class FakeTool:
        def __init__(self, name):
            self.tool_name = name

    discovered = [
        FakeTool("webSearch"),
        FakeTool("searchMapPlaces"),
        FakeTool("searchLocation"),
        FakeTool("getForecast"),
        FakeTool("Search_Hotel_Destination"),
        FakeTool("Search_Hotels"),
        FakeTool("Search_Flights"),
        FakeTool("Search_Taxi"),
    ]

    selected = service._select_mcp_tools_for_prompt(
        "Find hotels in Lisbon with prices in EUR.", discovered
    )

    assert [tool.tool_name for tool in selected] == [
        "webSearch",
        "searchMapPlaces",
        "Search_Hotel_Destination",
        "Search_Hotels",
    ]


def test_booking_and_weather_prompt_keeps_weather_geocoder():
    class FakeTool:
        def __init__(self, name):
            self.tool_name = name

    discovered = [
        FakeTool("searchLocation"),
        FakeTool("getForecast"),
        FakeTool("Search_Hotel_Destination"),
        FakeTool("Search_Hotels"),
    ]

    selected = service._select_mcp_tools_for_prompt(
        "Find hotels in Cannes and tell me the weather there.", discovered
    )

    assert [tool.tool_name for tool in selected] == [
        "searchLocation",
        "getForecast",
        "Search_Hotel_Destination",
        "Search_Hotels",
    ]


def test_flight_prompt_filters_aerodatabox_catalog_to_read_only_tools():
    class FakeTool:
        def __init__(self, name):
            self.tool_name = name

    discovered = [
        FakeTool("webSearch"),
        FakeTool("searchLocation"),
        FakeTool("getForecast"),
        FakeTool("Airport_Search__Auto-complete__TIER_2"),
        FakeTool("Aiport_by_code__TIER_1"),
        FakeTool("Flight_status_specific_date__TIER_2"),
        FakeTool("FIDS_Airport_departures_and_arrivals_by_time_range__TIER_2"),
        FakeTool("searchAirport"),
        FakeTool("searchFlights"),
        FakeTool("Search_Flight_Location"),
        FakeTool("Get_Availability_Calendar"),
        FakeTool("getPriceCalendar"),
        FakeTool("getFlightDetails"),
        FakeTool("Create_web-hook_subscription__FREE_TIER"),
        FakeTool("Remove_web-hook_subscription__FREE_TIER"),
        FakeTool("Airline_fleet__Aircraft_list_by_airline_code__TIER_3_BETA"),
    ]

    selected = service._select_mcp_tools_for_prompt(
        "What is the status of flight BA123 on 2026-08-01?", discovered
    )

    assert [tool.tool_name for tool in selected] == [
        "webSearch",
        "Airport_Search__Auto-complete__TIER_2",
        "Aiport_by_code__TIER_1",
        "Flight_status_specific_date__TIER_2",
        "FIDS_Airport_departures_and_arrivals_by_time_range__TIER_2",
        "searchAirport",
        "searchFlights",
        "Search_Flight_Location",
        "Get_Availability_Calendar",
        "getPriceCalendar",
        "getFlightDetails",
    ]


def test_map_prompt_allows_geoapify_geocoding_tools():
    class FakeTool:
        def __init__(self, name):
            self.tool_name = name

    discovered = [
        FakeTool("webSearch"),
        FakeTool("searchMapPlaces"),
        FakeTool("findNearbyPlaces"),
        FakeTool("geocode_address"),
        FakeTool("geocode_structured_address"),
        FakeTool("reverse_geocode_coordinates"),
        FakeTool("searchFlights"),
    ]

    selected = service._select_mcp_tools_for_prompt(
        "Find attractions near 5 Avenue Anatole France, Paris.", discovered
    )

    assert [tool.tool_name for tool in selected] == [
        "webSearch",
        "searchMapPlaces",
        "findNearbyPlaces",
        "geocode_address",
        "reverse_geocode_coordinates",
    ]


def test_travel_planner_prompt_allows_trip_planning_tools():
    class FakeTool:
        def __init__(self, name):
            self.tool_name = name

    discovered = [
        FakeTool("webSearch"),
        FakeTool("searchMapPlaces"),
        FakeTool("findNearbyPlaces"),
        FakeTool("searchLocation"),
        FakeTool("getForecast"),
        FakeTool("geocode_address"),
        FakeTool("Search_Hotel_Destination"),
        FakeTool("Search_Hotels"),
        FakeTool("Airport_Search__Auto-complete__TIER_2"),
        FakeTool("searchAirport"),
        FakeTool("searchFlights"),
        FakeTool("Create_web-hook_subscription__FREE_TIER"),
    ]

    selected = service._select_mcp_tools_for_prompt(
        "Build a three-day itinerary for Cannes with hotels and flight ideas.",
        discovered,
    )

    assert [tool.tool_name for tool in selected] == [
        "webSearch",
        "searchMapPlaces",
        "findNearbyPlaces",
        "searchLocation",
        "getForecast",
        "geocode_address",
        "Search_Hotel_Destination",
        "Search_Hotels",
        "Airport_Search__Auto-complete__TIER_2",
        "searchAirport",
        "searchFlights",
    ]
