---
name: booking-search
description: Use when the user asks for live accommodation search, availability, or prices
keywords: accommodation, accommodations, booking, availability, room, rooms, stay, stays, lodging, hotel, hotels, price, prices, hébergement, hebergement, disponibilité, disponibilite, chambre, chambres, séjour, sejour
---
## When to use

Use this skill when the user asks to search current accommodation inventory,
availability, or prices for a destination and date range. This first version
intentionally exposes only destination lookup and hotel search; detailed room,
photo, policy, and booking operations are outside its tool scope.

## Outcome

Return a compact comparison based on successful Booking.com MCP tool results,
with destination, dates, occupancy, currency, price basis, and material caveats
stated clearly.

## Procedure

1. Establish destination, check-in, check-out, number of adults, number and
   ages of children, room count, and preferred currency. Ask only for missing
   fields required by the available Booking.com tool.
2. Resolve destination identifiers using the Booking.com MCP tools when the
   API requires them; never guess an identifier. For `Search_Hotels`, pass the
   returned `dest_id` as a string and preserve the lowercase `search_type`
   returned by `Search_Hotel_Destination`. The provider's generated schema may
   describe `dest_id` as numeric, but its live endpoint rejects that form.
3. Search live inventory with the exact dates and occupancy supplied by the
   user.
4. Compare like-for-like price bases. State whether taxes, fees, breakfast,
   cancellation, and payment terms are included when the tool returns them.
5. Include source or property URLs returned by the tool.

## Never

- Never claim availability or a price without a successful live tool result.
- Never treat a search result as a confirmed reservation.
- Never book, purchase, or submit payment details.
- Never expose credentials, request headers, or internal tool configuration.
- Never invent missing taxes, fees, cancellation terms, ratings, or amenities.
