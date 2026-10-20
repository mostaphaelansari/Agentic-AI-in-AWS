import json
import urllib.parse
import urllib.request

# Fields that bloat geocoding results without helping the model pick coordinates.
# postcodes alone can be 200+ entries for a single city.
_LOCATION_DROP = {"postcodes", "admin1_id", "admin2_id", "admin3_id", "admin4_id", "country_id"}


def _fetch(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=20) as response:
        return json.loads(response.read().decode())


def _clean_location(result: dict) -> dict:
    return {k: v for k, v in result.items() if k not in _LOCATION_DROP}


def _normalize_tool_name(raw_name: str) -> str:
    if "___" in raw_name:
        return raw_name.split("___", 1)[1]
    return raw_name


def lambda_handler(event, context):
    tool_name = ""
    if context and context.client_context and context.client_context.custom:
        tool_name = _normalize_tool_name(
            context.client_context.custom.get("bedrockAgentCoreToolName", "")
        )

    try:
        if tool_name == "getForecast":
            params = {
                "latitude": event["latitude"],
                "longitude": event["longitude"],
            }
            for key in ("current_weather", "daily", "hourly", "forecast_days", "timezone"):
                if key in event and event[key] is not None:
                    params[key] = event[key]
            url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(
                params, doseq=True
            )
        elif tool_name == "searchLocation":
            params = {"name": event["name"]}
            for key in ("count", "language"):
                if key in event and event[key] is not None:
                    params[key] = event[key]
            url = "https://geocoding-api.open-meteo.com/v1/search?" + urllib.parse.urlencode(params)
        else:
            return {
                "statusCode": 400,
                "body": json.dumps({"error": f"Unknown tool: {tool_name or 'missing'}"}),
            }

        data = _fetch(url)
        if tool_name == "searchLocation" and "results" in data:
            data = {"results": [_clean_location(r) for r in data["results"]]}
        return {"statusCode": 200, "body": json.dumps(data)}
    except Exception as exc:
        return {"statusCode": 500, "body": json.dumps({"error": str(exc)})}
