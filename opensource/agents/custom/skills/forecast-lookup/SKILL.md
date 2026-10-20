---
name: forecast-lookup
description: Use when the user asks about current weather or a weather forecast for a place
keywords: weather, forecast, rain, snow, temperature, wind, humidity, sunny, cloudy, cold, hot, warm, today, tomorrow, tonight, evening, weekend, week, meteo, météo, pluie, pleuvoir, temps, demain, neige, vent
---
## When to use

Use this skill when the user asks about current weather, weather conditions,
or a forecast for a specific place or period.

## Outcome

Provide an accurate, location-specific answer based on live weather data and
the correct local forecast period.

## Procedure

1. `searchLocation` with the city name
2. `getForecast` with these parameters every time:
   - `current_weather=true` — temperature, wind, conditions
   - `hourly=relativehumidity_2m,precipitation_probability` — humidity and rain chance
   - `forecast_days=1` — today only (increase for multi-day questions)
   - `timezone=auto` — correct local times
   For multi-day forecasts, also add
   `daily=temperature_2m_max,temperature_2m_min,precipitation_sum,weathercode`.

**City name encoding:** `searchLocation` uses English or local-language
names, not French exonyms. Translate before calling: `Barcelona` not
`Barcelone`, `London` not `Londres`, `Vienna` not `Vienne`, `Lisbon` not
`Lisbonne`, `Geneva` not `Genève`. Pick the first result whose
`country_code` matches the country the user means.

## Never

- Never guess coordinates or weather conditions.
- Never select a location whose country conflicts with the user's request.
- Never present historical climate data as a live forecast.
- Never claim forecast coverage beyond the period returned by the tool.
