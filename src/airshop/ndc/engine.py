"""Deterministic local AirShopping engine.

Stands in for a carrier's shopping backend so the agent can be built, tested and
demoed with no network. All randomness is seeded from the request, so the same
request always yields the same offers — which is what makes the generated
training data reproducible.
"""

from __future__ import annotations

import hashlib
import random
from datetime import datetime, timedelta

from airshop.data.reference import (
    AIRPORTS,
    CARRIERS,
    FARE_BASIS_PREFIX,
)
from airshop.ndc.models import (
    AirShoppingRequest,
    AirShoppingResponse,
    Itinerary,
    Segment,
)

# Approximate great-circle-ish flight time in minutes between regions.
_REGION_BLOCK_MINUTES = {
    ("Europe", "Europe"): 120,
    ("Europe", "North America"): 480,
    ("North America", "Europe"): 480,
    ("Europe", "Middle East"): 360,
    ("Middle East", "Europe"): 360,
    ("Europe", "Asia"): 600,
    ("Asia", "Europe"): 600,
    ("North America", "Asia"): 720,
    ("Asia", "North America"): 720,
    ("Asia", "Asia"): 300,
    ("Middle East", "Asia"): 300,
    ("Asia", "Middle East"): 300,
    ("Oceania", "Asia"): 480,
    ("Asia", "Oceania"): 480,
    ("South America", "Europe"): 660,
    ("Europe", "South America"): 660,
    ("Africa", "Europe"): 480,
    ("Europe", "Africa"): 480,
}

_AIRCRAFT = ["320", "321", "738", "77W", "789", "350", "380"]
_BOOKING_CLASSES = {"Y": "K", "S": "T", "C": "J", "J": "C", "F": "A"}


def _seed_for(request: AirShoppingRequest) -> int:
    key = "|".join(
        [
            request.transaction_id,
            ",".join(f"{t.ptc}{t.quantity}" for t in request.travelers),
            ",".join(
                f"{od.origin}{od.destination}{od.departure_date}{od.cabin}"
                for od in request.origin_dests
            ),
        ]
    )
    return int(hashlib.sha256(key.encode()).hexdigest()[:8], 16)


def _duration(origin: str, destination: str, rng: random.Random) -> int:
    o_region = AIRPORTS[origin][2]
    d_region = AIRPORTS[destination][2]
    base = _REGION_BLOCK_MINUTES.get((o_region, d_region))
    if base is None:
        if o_region == d_region:
            base = 150
        else:
            base = 600
    return base + rng.choice([-20, -10, 0, 10, 25, 40])


def _cabin_pool(request: AirShoppingRequest) -> list[str]:
    requested = request.cabin
    if requested:
        return [requested]
    return ["Y", "S", "C"]


def _build_segment(
    ref: str,
    origin: str,
    destination: str,
    depart: datetime,
    carrier: str,
    flight_number: str,
    cabin: str,
    rng: random.Random,
) -> Segment:
    minutes = _duration(origin, destination, rng)
    arrival = depart + timedelta(minutes=minutes)
    return Segment(
        segment_ref=ref,
        origin=origin,
        destination=destination,
        departure=depart.isoformat(timespec="seconds"),
        arrival=arrival.isoformat(timespec="seconds"),
        marketing_carrier=carrier,
        operating_carrier=carrier,
        flight_number=flight_number,
        aircraft=rng.choice(_AIRCRAFT),
        cabin=cabin,
        booking_class=_BOOKING_CLASSES.get(cabin, "K"),
        duration_minutes=minutes,
    )


def _price(
    itinerary_segments: list[Segment],
    cabin: str,
    passengers: int,
    rng: random.Random,
) -> float:
    total_minutes = sum(s.duration_minutes for s in itinerary_segments)
    # Base fare per hour, multiplied by a cabin factor and per-passenger count.
    cabin_factor = {"Y": 1.0, "S": 1.45, "C": 2.9, "J": 3.1, "F": 4.8}[cabin]
    per_pax = (total_minutes / 60.0) * 85.0 * cabin_factor
    per_pax *= rng.uniform(0.85, 1.25)
    if len(itinerary_segments) > 1:
        per_pax *= 0.95  # connection discount
    return round(per_pax * passengers, 2)


def shop(request: AirShoppingRequest) -> AirShoppingResponse:
    """Produce offers for a request from local inventory."""
    rng = random.Random(_seed_for(request))
    passengers = request.total_passengers
    itineraries: list[Itinerary] = []
    warnings: list[str] = []

    od = request.origin_dests[0]
    cabins = _cabin_pool(request)
    max_connections = 0 if request.preferences.direct_only else request.preferences.max_connections
    carriers = request.preferences.carriers or list(CARRIERS)

    offer_index = 0
    for cabin in cabins:
        for _ in range(rng.randint(2, 4)):
            connections = 0 if max_connections == 0 else rng.randint(0, max_connections)
            carrier = rng.choice(carriers)
            depart_day = od.departure_date
            depart = datetime.combine(
                depart_day, datetime.min.time()
            ) + timedelta(hours=rng.randint(6, 21), minutes=rng.choice([0, 15, 30, 45]))

            segments: list[Segment] = []
            if connections == 0:
                seg = _build_segment(
                    f"SEG{offer_index + 1}",
                    od.origin,
                    od.destination,
                    depart,
                    carrier,
                    f"{carrier}{rng.randint(100, 999)}",
                    cabin,
                    rng,
                )
                segments.append(seg)
            else:
                # Insert connections via hub airports in the same region.
                hubs = [
                    code
                    for code, (_c, _co, region) in AIRPORTS.items()
                    if region == AIRPORTS[od.origin][2]
                    and code not in {od.origin, od.destination}
                ] or list(AIRPORTS)
                stops = [od.origin]
                cursor = depart
                for i in range(connections):
                    hub = rng.choice(hubs)
                    if hub in stops:
                        continue
                    seg = _build_segment(
                        f"SEG{offer_index + 1}_{i + 1}",
                        stops[-1],
                        hub,
                        cursor,
                        carrier,
                        f"{carrier}{rng.randint(100, 999)}",
                        cabin,
                        rng,
                    )
                    segments.append(seg)
                    cursor = datetime.fromisoformat(seg.arrival) + timedelta(
                        minutes=rng.randint(45, 180)
                    )
                    stops.append(hub)
                final = _build_segment(
                    f"SEG{offer_index + 1}_{connections + 1}",
                    stops[-1],
                    od.destination,
                    cursor,
                    carrier,
                    f"{carrier}{rng.randint(100, 999)}",
                    cabin,
                    rng,
                )
                segments.append(final)

            offer_index += 1
            price = _price(segments, cabin, passengers, rng)
            total_duration = sum(s.duration_minutes for s in segments)
            # Account for layover time in the elapsed itinerary duration.
            if len(segments) > 1:
                elapsed = (
                    datetime.fromisoformat(segments[-1].arrival)
                    - datetime.fromisoformat(segments[0].departure)
                )
                total_duration = int(elapsed.total_seconds() // 60)

            refundable = cabin in {"C", "J", "F"} or rng.random() < 0.2
            itineraries.append(
                Itinerary(
                    itinerary_id=f"IT{offer_index}",
                    origin=od.origin,
                    destination=od.destination,
                    departure_date=od.departure_date,
                    segments=segments,
                    connections=len(segments) - 1,
                    total_duration_minutes=total_duration,
                    cabin=cabin,
                    fare_basis_code=(
                        f"{FARE_BASIS_PREFIX[cabin]}{rng.choice('KLMNQSTVWX')}"
                        f"{rng.randint(100, 999)}"
                    ),
                    price=price,
                    currency=request.preferences.currency,
                    seats_available=rng.randint(1, 9),
                    refundable=refundable,
                    changeable=refundable or rng.random() < 0.5,
                )
            )

    # Apply preference filters.
    if request.preferences.carriers:
        allowed = set(request.preferences.carriers)
        itineraries = [
            it for it in itineraries if it.segments[0].marketing_carrier in allowed
        ]
    if request.preferences.max_price is not None:
        itineraries = [
            it for it in itineraries if it.price <= request.preferences.max_price
        ]
    if max_connections == 0:
        itineraries = [it for it in itineraries if it.connections == 0]

    if not itineraries:
        warnings.append(
            "No offers matched the requested criteria; consider relaxing filters."
        )

    itineraries.sort(key=lambda it: it.price)
    response_id = hashlib.sha256(
        f"{request.transaction_id}{offer_index}".encode()
    ).hexdigest()[:20].upper()

    return AirShoppingResponse(
        transaction_id=request.transaction_id,
        response_id=response_id,
        currency=request.preferences.currency,
        itineraries=itineraries,
        warnings=warnings,
    )
