---
name: flight-search
description: Use when the user asks for flight search, flight prices, live flight status, airport departures, arrivals, airport lookup, or flight timing
keywords: flight, flights, airfare, airfares, ticket, tickets, airport, airports, airline, airlines, departure, departures, arrival, arrivals, delayed, delay, gate, terminal, boarding, landed, cancelled, canceled, aircraft, avion, vol, vols, billet, billets, aeroport, aéroport, départ, arrivée, retard
---
## When to use

Use this skill when the user asks for flight shopping/search, flight prices,
current or scheduled flight information, airport arrivals/departures, airport
lookup, or flight timing. Use Air Scraper for flight shopping/search and
AeroDataBox for operational status/airport boards. Data can be delayed,
incomplete, or unavailable depending on route, airport, airline, date, and
RapidAPI tier.

## Outcome

Return a compact, source-backed answer with flight or airport identifiers,
local dates/times, prices or fare leads when returned, status,
terminal/gate/baggage details when returned, and any uncertainty or provider
limitation.

## Procedure

1. Establish origin, destination, date(s), passenger count, currency, and
   whether the user wants flight shopping, a price calendar, multi-stop search,
   airport boards, current status, or airport details. Ask only for missing
   fields required by the selected tool.
2. For flight shopping/search, use Air Scraper tools: resolve airports with
   `searchAirport`, `Search_Flight_Location`, or `getNearByAirports`, then use
   `searchFlights`, `searchFlights_Version_2`, `Search_Flights`,
   `getPriceCalendar`, `Get_Availability_Calendar`,
   `searchFlightsMultiStops`, `searchFlightEverywhere`, or `getFlightDetails`
   as appropriate.
   - For a city with multiple commercial airports, preserve the metropolitan
     result or search every relevant returned airport. Do not silently select
     only the first airport. For example, Tokyo searches must include both HND
     and NRT (or a returned TYO metropolitan identifier) when the user did not
     name a specific airport.
   - A usable result must match the requested direction, date, trip type,
     origin/destination scope, and cabin when specified. Once usable results
     are returned, stop the retrieval path and answer from them.
   - If airport resolution or flight search is unavailable, returns an error
     or malformed response, or returns no usable itinerary, make at most one
     corrected specialty-tool retry. Correct airport scope or parameters; do
     not repeat an unchanged call.
   - Only after that retry fails, use `webSearch` as a degraded fallback. Use
     browser automation only when it is available and web search still lacks
     concrete itinerary details. State that fallback was required and do not
     present fallback prices as booking guarantees.
3. For city or airport names in operational-status prompts, use
   `Airport_Search__Auto-complete__TIER_2` or `Aiport_by_code__TIER_1` to
   resolve the IATA/ICAO code before calling status or FIDS tools.
4. For a specific flight, prefer `Flight_status_specific_date__TIER_2` when a
   date is supplied; use `Flight_status_nearest_day__TIER_2` only when the user
   did not provide a date.
5. For airport boards, use the FIDS departures/arrivals tools with the airport
   code and a bounded time window.
6. State that results are search/operational data, not a booking or guarantee.

## Never

- Never book, cancel, check in, change a flight, or submit passenger/payment
  details.
- Never call webhook create/remove/refill tools or other mutating operations.
- Never call booking/order/purchase/payment tools if the provider adds them.
- Never expose credentials, request headers, or internal MCP configuration.
- Never invent missing price, fare, gate, terminal, baggage, delay, or
  cancellation data.
