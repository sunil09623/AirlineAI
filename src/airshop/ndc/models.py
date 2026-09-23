"""Normalized (provider-agnostic) models for an NDC AirShopping transaction.

These sit between raw NDC XML and the agent's tools: XML in -> normalized model,
normalized model -> XML out. Keeping them separate means the agent never has to
reason about XML namespaces, and the same models drive the training-data
generator.
"""

from __future__ import annotations

from datetime import date as Date
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from airshop.data.reference import AIRPORTS, CABINS, CARRIERS, CURRENCIES, PASSENGER_TYPES


class Traveler(BaseModel):
    """An individual passenger and its count, e.g. ``3x ADT``."""

    ptc: str = Field(description="IATA passenger type code, e.g. ADT/CHD/INF.")
    quantity: int = Field(default=1, ge=1, le=9)
    name: str | None = None

    @field_validator("ptc")
    @classmethod
    def _known_ptc(cls, v: str) -> str:
        v = v.upper()
        if v not in PASSENGER_TYPES:
            raise ValueError(
                f"unknown PTC {v!r}; expected one of {sorted(PASSENGER_TYPES)}"
            )
        return v


class OriginDest(BaseModel):
    """One requested origin/destination pair for a given departure date."""

    origin: str
    destination: str
    departure_date: Date
    cabin: str | None = Field(
        default=None, description="Cabin code Y/S/C/J/F; None means no preference."
    )

    @field_validator("origin", "destination")
    @classmethod
    def _known_airport(cls, v: str) -> str:
        v = v.upper()
        if v not in AIRPORTS:
            raise ValueError(
                f"unknown airport {v!r}; {len(AIRPORTS)} airports are available"
            )
        return v

    @field_validator("cabin")
    @classmethod
    def _known_cabin(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.upper()
        if v not in CABINS:
            raise ValueError(f"unknown cabin {v!r}; expected {sorted(CABINS)}")
        return v


class Preferences(BaseModel):
    """Non-functional shopping criteria carried on the request."""

    currency: str = "USD"
    max_connections: int = Field(default=2, ge=0, le=4)
    direct_only: bool = False
    carriers: list[str] = Field(default_factory=list)
    max_price: float | None = Field(default=None, gt=0)
    language: str = "en"

    @field_validator("currency")
    @classmethod
    def _known_currency(cls, v: str) -> str:
        v = v.upper()
        if v not in CURRENCIES:
            raise ValueError(f"unsupported currency {v!r}; expected {sorted(CURRENCIES)}")
        return v

    @field_validator("carriers")
    @classmethod
    def _known_carriers(cls, v: list[str]) -> list[str]:
        unknown = [c.upper() for c in v if c.upper() not in CARRIERS]
        if unknown:
            raise ValueError(
                f"unknown carrier code(s) {unknown}; expected {sorted(CARRIERS)}"
            )
        return [c.upper() for c in v]


class AirShoppingRequest(BaseModel):
    """Normalized AirShoppingRQ."""

    transaction_id: str = Field(description="NDC TrxID / CorrelationID.")
    travelers: list[Traveler]
    origin_dests: list[OriginDest]
    preferences: Preferences = Field(default_factory=Preferences)

    @property
    def cabin(self) -> str | None:
        for od in self.origin_dests:
            if od.cabin:
                return od.cabin
        return None

    @property
    def total_passengers(self) -> int:
        return sum(t.quantity for t in self.travelers)

    def pax_type_counts(self) -> dict[str, int]:
        return {t.ptc: t.quantity for t in self.travelers}


class Segment(BaseModel):
    """A single flown leg."""

    segment_ref: str
    origin: str
    destination: str
    departure: str = Field(description="ISO-8601 local datetime.")
    arrival: str
    marketing_carrier: str
    operating_carrier: str
    flight_number: str
    aircraft: str
    cabin: str
    booking_class: str
    duration_minutes: int


class Itinerary(BaseModel):
    """A priced itinerary = ordered segments + fare details."""

    itinerary_id: str
    origin: str
    destination: str
    departure_date: Date
    segments: list[Segment]
    connections: int
    total_duration_minutes: int
    cabin: str
    fare_basis_code: str
    price: float
    currency: str
    seats_available: int
    refundable: bool
    changeable: bool


class AirShoppingResponse(BaseModel):
    """Normalized AirShoppingRS."""

    transaction_id: str
    response_id: str
    currency: str
    itineraries: list[Itinerary]
    warnings: list[str] = Field(default_factory=list)

    @property
    def offer_count(self) -> int:
        return len(self.itineraries)


TripType = Literal["oneway", "roundtrip", "multicity"]
