---
name: compare-cities
description: Use when the user asks to compare weather across two or more cities
keywords: compare, comparison, versus, vs, both, difference, between, which city, hotter, colder, warmer, wetter, comparer, comparaison
---
## When to use

Use this skill when the user asks to compare weather conditions or forecasts
across two or more cities.

## Outcome

Provide one accurate, easy-to-scan comparison based on separately retrieved
weather data for every requested city.

## Procedure

1. Call `searchLocation` separately for each city.
2. Call `getForecast` separately for each resolved location.
3. Compare equivalent measurements and forecast periods in a single answer.
4. Use a compact table when comparing three or more cities.

## Never

- Never guess coordinates.
- Never reuse one city's location or forecast results for another city.
- Never compare different time periods as though they were equivalent.
