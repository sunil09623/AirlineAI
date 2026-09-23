"""Trip-shape and passenger comparison for AirShopping reviews.

Two AirShoppingRS documents from different releases often describe different
*journeys*, not just different prices: a request may have moved from one-way to
round-trip or multi-city, or the passenger mix may have changed. Those are the
differences a release reviewer actually cares about, and they are invisible to a
pure node-by-node diff.

This module extracts the trip shape and passenger mix from an AirShoppingRQ (the
authoritative source of what was asked for) or, failing that, from an
AirShoppingRS (what was answered), and reports what changed between two of them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from lxml import etree

from airshop.ndc.xmlutil import find, find_all, first, localname, text

# Passenger type codes and their plain-English names.
PTC_NAMES: dict[str, str] = {
    "ADT": "Adult",
    "CHD": "Child",
    "INF": "Infant",
    "YTH": "Youth",
    "SRC": "Senior",
    "INS": "Infant seated",
    "UNN": "Unaccompanied minor",
    "STU": "Student",
    "MIL": "Military",
}


@dataclass
class Leg:
    """One requested origin/destination pair."""

    origin: str
    destination: str
    departure_date: str = ""

    def describe(self) -> str:
        base = f"{self.origin}->{self.destination}"
        return f"{base} on {self.departure_date}" if self.departure_date else base


@dataclass
class TripShape:
    """The trip a message describes: its legs and its passenger mix."""

    trip_type: str = "unknown"  # one-way | round-trip | multi-city | unknown
    legs: list[Leg] = field(default_factory=list)
    pax: dict[str, int] = field(default_factory=dict)  # PTC -> count
    cabin: str | None = None
    source: str = "unknown"  # request | response
    currency: str = ""

    @property
    def pax_total(self) -> int:
        return sum(self.pax.values())

    @property
    def pax_summary(self) -> str:
        if not self.pax:
            return "unknown"
        parts = [
            f"{count} {PTC_NAMES.get(ptc, ptc)}"
            for ptc, count in sorted(self.pax.items())
        ]
        return ", ".join(parts)

    def describe(self) -> str:
        legs = "; ".join(leg.describe() for leg in self.legs) or "no legs"
        cabin = self.cabin or "no cabin preference"
        return (
            f"{self.trip_type} ({legs}), {self.pax_summary}, {cabin}"
            + (f", {self.currency}" if self.currency else "")
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "trip_type": self.trip_type,
            "legs": [
                {"origin": l.origin, "destination": l.destination, "date": l.departure_date}
                for l in self.legs
            ],
            "pax": dict(self.pax),
            "pax_total": self.pax_total,
            "pax_summary": self.pax_summary,
            "cabin": self.cabin,
            "currency": self.currency,
            "source": self.source,
        }


def _classify(legs: list[Leg]) -> str:
    """Classify a leg list as one-way, round-trip or multi-city."""
    if not legs:
        return "unknown"
    if len(legs) == 1:
        return "one-way"
    if len(legs) == 2:
        first, second = legs[0], legs[1]
        # Round trip is an exact A->B followed by B->A.
        if first.origin == second.destination and first.destination == second.origin:
            return "round-trip"
        return "multi-city"
    return "multi-city"


def trip_shape_from_rq(xml: str | bytes) -> TripShape:
    """Extract trip shape and passengers from an AirShoppingRQ (authoritative)."""
    root = _root(xml)
    legs = _legs_from_rq(root)
    pax = _pax_from_rq(root)

    cabin = None
    cabin_el = first(find(root, "CabinTypeName"), find(root, "CabinTypeCode"))
    if cabin_el is not None:
        cabin = text(cabin_el)

    currency = text(find(root, "CurCode")) or ""

    return TripShape(
        trip_type=_classify(legs),
        legs=legs,
        pax=pax,
        cabin=cabin,
        source="request",
        currency=currency,
    )


def trip_shape_from_rs(xml: str | bytes) -> TripShape:
    """Extract trip shape from an AirShoppingRS.

    A response has no explicit leg list, so the requested pairs are reconstructed
    from OriginDest entries and the distinct segment routes.
    """
    root = _root(xml)
    legs = _legs_from_rs(root)
    pax = _pax_from_rs(root)
    currency = text(find(root, "CurCode")) or ""

    return TripShape(
        trip_type=_classify(legs),
        legs=legs,
        pax=pax,
        source="response",
        currency=currency,
    )


def _root(xml: str | bytes) -> etree._Element:
    if isinstance(xml, str):
        xml = xml.encode("utf-8")
    return etree.fromstring(xml)


def _legs_from_rq(root: etree._Element) -> list[Leg]:
    legs: list[Leg] = []
    for od in find_all(root, "OriginDest"):
        origin = text(find(od, "OriginCode"))
        dest = text(find(od, "DestCode"))
        date = text(find(od, "DepartureDate")) or text(find(od, "DepartureDateTime")) or ""
        if origin and dest:
            legs.append(Leg(origin=origin, destination=dest, departure_date=date[:10]))
    return legs


def _legs_from_rs(root: etree._Element) -> list[Leg]:
    legs: list[Leg] = []
    for od in find_all(root, "OriginDest"):
        origin = text(find(od, "OriginCode"))
        dest = text(find(od, "DestCode"))
        if origin and dest:
            legs.append(Leg(origin=origin, destination=dest))
    if legs:
        return legs
    # No OriginDestList: fall back to the distinct segment routes.
    seen: set[tuple[str, str]] = set()
    for seg in find_all(root, "FlightSegment"):
        dep = find(seg, "Departure")
        arr = find(seg, "Arrival")
        origin = text(find(dep, "AirportCode")) if dep is not None else None
        dest = text(find(arr, "AirportCode")) if arr is not None else None
        if origin and dest and (origin, dest) not in seen:
            seen.add((origin, dest))
            legs.append(Leg(origin=origin, destination=dest))
    return legs


def _pax_from_rq(root: etree._Element) -> dict[str, int]:
    counts: dict[str, int] = {}
    for pax in find_all(root, "Pax"):
        ptc = text(find(pax, "PTC")) or text(find(pax, "PaxType"))
        if not ptc:
            continue
        ptc = ptc.upper()
        counts[ptc] = counts.get(ptc, 0) + 1
    return counts


def _pax_from_rs(root: etree._Element) -> dict[str, int]:
    counts: dict[str, int] = {}
    for pax in find_all(root, "Pax"):
        ptc = text(find(pax, "PTC"))
        if ptc:
            ptc = ptc.upper()
            counts[ptc] = counts.get(ptc, 0) + 1
    return counts


@dataclass
class ShapeChange:
    """What changed between two trip shapes."""

    baseline: TripShape
    new: TripShape
    trip_type_changed: bool = False
    legs_added: list[Leg] = field(default_factory=list)
    legs_removed: list[Leg] = field(default_factory=list)
    pax_added: dict[str, int] = field(default_factory=dict)
    pax_removed: dict[str, int] = field(default_factory=dict)
    cabin_changed: tuple[str | None, str | None] | None = None
    currency_changed: tuple[str, str] | None = None

    @property
    def changed(self) -> bool:
        return bool(
            self.trip_type_changed
            or self.legs_added
            or self.legs_removed
            or self.pax_added
            or self.pax_removed
            or self.cabin_changed
            or self.currency_changed
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "changed": self.changed,
            "baseline": self.baseline.to_dict(),
            "new": self.new.to_dict(),
            "trip_type_changed": self.trip_type_changed,
            "legs_added": [l.describe() for l in self.legs_added],
            "legs_removed": [l.describe() for l in self.legs_removed],
            "pax_added": dict(self.pax_added),
            "pax_removed": dict(self.pax_removed),
            "cabin_changed": list(self.cabin_changed) if self.cabin_changed else None,
            "currency_changed": list(self.currency_changed) if self.currency_changed else None,
        }

    def render(self) -> str:
        if not self.changed:
            return (
                f"Trip shape unchanged: {self.new.describe()}"
            )
        lines = ["TRIP / PASSENGER changes:"]
        if self.trip_type_changed:
            lines.append(
                f"  - trip type: {self.baseline.trip_type} -> {self.new.trip_type}"
            )
        for leg in self.legs_removed:
            lines.append(f"  - leg no longer requested: {leg.describe()}")
        for leg in self.legs_added:
            lines.append(f"  + leg newly requested: {leg.describe()}")
        for ptc, count in sorted(self.pax_removed.items()):
            name = PTC_NAMES.get(ptc, ptc)
            lines.append(f"  - {count} {name} ({ptc}) no longer carried")
        for ptc, count in sorted(self.pax_added.items()):
            name = PTC_NAMES.get(ptc, ptc)
            lines.append(f"  + {count} {name} ({ptc}) added")
        if self.cabin_changed:
            was, now = self.cabin_changed
            lines.append(f"  - cabin: {was or 'none'} -> {now or 'none'}")
        if self.currency_changed:
            was, now = self.currency_changed
            lines.append(f"  - currency: {was} -> {now}")
        lines.append(f"  baseline: {self.baseline.describe()}")
        lines.append(f"  new:      {self.new.describe()}")
        return "\n".join(lines)


def compare_trip_shape(baseline: TripShape, new: TripShape) -> ShapeChange:
    """Diff two trip shapes into a reviewable change set."""
    base_keys = {(l.origin, l.destination) for l in baseline.legs}
    new_keys = {(l.origin, l.destination) for l in new.legs}

    legs_added = [l for l in new.legs if (l.origin, l.destination) not in base_keys]
    legs_removed = [l for l in baseline.legs if (l.origin, l.destination) not in new_keys]

    pax_added: dict[str, int] = {}
    pax_removed: dict[str, int] = {}
    for ptc in sorted(set(baseline.pax) | set(new.pax)):
        delta = new.pax.get(ptc, 0) - baseline.pax.get(ptc, 0)
        if delta > 0:
            pax_added[ptc] = delta
        elif delta < 0:
            pax_removed[ptc] = -delta

    cabin_changed = None
    if baseline.cabin != new.cabin:
        cabin_changed = (baseline.cabin, new.cabin)

    currency_changed = None
    if baseline.currency and new.currency and baseline.currency != new.currency:
        currency_changed = (baseline.currency, new.currency)

    return ShapeChange(
        baseline=baseline,
        new=new,
        trip_type_changed=baseline.trip_type != new.trip_type,
        legs_added=legs_added,
        legs_removed=legs_removed,
        pax_added=pax_added,
        pax_removed=pax_removed,
        cabin_changed=cabin_changed,
        currency_changed=currency_changed,
    )


def detect_kind(root: etree._Element) -> str:
    """Return 'request' or 'response' for a parsed NDC document."""
    name = localname(root.tag)
    if name.endswith("RQ"):
        return "request"
    if name.endswith("RS"):
        return "response"
    return "unknown"
