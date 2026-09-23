"""Deterministic validation of NDC AirShopping messages.

Two jobs:

1. Structural conformance — every ``FlightSegmentReference``/``OriginDestRef``/
   ``FareRef`` inside an Offer must resolve to an entry in ``DataLists``.
2. Business-rules conformance — cabin, connection count, carrier and price
   filters actually hold against the request.

Both produce machine-checkable results, which is what lets a generated
trajectory be scored automatically for training.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from lxml import etree

from airshop.ndc.models import AirShoppingRequest, AirShoppingResponse
from airshop.ndc.xmlutil import localname as _localname


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""

    def __str__(self) -> str:
        mark = "PASS" if self.passed else "FAIL"
        return f"[{mark}] {self.name}" + (f" — {self.detail}" if self.detail else "")


@dataclass
class ValidationReport:
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    @property
    def score(self) -> float:
        if not self.checks:
            return 1.0
        return sum(1 for c in self.checks if c.passed) / len(self.checks)

    def summary(self) -> str:
        if not self.checks:
            return "no checks run"
        passed = sum(1 for c in self.checks if c.passed)
        return f"{passed}/{len(self.checks)} checks passed (score={self.score:.2f})"


def validate_rq(request: AirShoppingRequest) -> ValidationReport:
    """Validate a parsed request is internally coherent before shopping."""
    report = ValidationReport()

    report.checks.append(
        CheckResult(
            "rq.has_transaction_id",
            bool(request.transaction_id and request.transaction_id != "AUTO"),
            f"TrxID={request.transaction_id!r}",
        )
    )
    report.checks.append(
        CheckResult(
            "rq.has_origin_dest",
            len(request.origin_dests) >= 1,
            f"{len(request.origin_dests)} origin/destination pair(s)",
        )
    )
    report.checks.append(
        CheckResult(
            "rq.has_passengers",
            request.total_passengers >= 1,
            f"{request.total_passengers} passenger(s): {request.pax_type_counts()}",
        )
    )
    for i, od in enumerate(request.origin_dests):
        report.checks.append(
            CheckResult(
                f"rq.od[{i}].endpoints_differ",
                od.origin != od.destination,
                f"{od.origin}->{od.destination}",
            )
        )
    return report


def validate_rs_xml(xml: str | bytes) -> ValidationReport:
    """Validate reference integrity of an AirShoppingRS document."""
    report = ValidationReport()
    if isinstance(xml, str):
        xml = xml.encode("utf-8")

    try:
        root = etree.fromstring(xml)
    except etree.XMLSyntaxError as exc:
        report.checks.append(CheckResult("rs.well_formed_xml", False, str(exc)))
        return report

    report.checks.append(CheckResult("rs.well_formed_xml", True))
    report.checks.append(
        CheckResult(
            "rs.root_is_air_shopping_rs",
            _localname(root.tag) in {"AirShoppingRS", "IATA_AirShoppingRS"},
            f"root=<{_localname(root.tag)}>",
        )
    )

    # Collect declared IDs from DataLists.
    segment_refs = {
        e.get("ref")
        for e in root.iter()
        if _localname(e.tag) == "FlightSegmentReference" and e.get("ref")
    }
    declared_segments = {
        e.text.strip()
        for e in root.iter()
        if _localname(e.tag) == "SegmentID" and e.text
    }

    dangling = sorted(segment_refs - declared_segments)
    report.checks.append(
        CheckResult(
            "rs.segment_refs_resolve",
            not dangling,
            f"dangling refs: {dangling}" if dangling else f"{len(segment_refs)} refs OK",
        )
    )

    fare_codes = {
        e.text.strip() for e in root.iter() if _localname(e.tag) == "FareCode" and e.text
    }
    fare_refs = {
        e.text.strip() for e in root.iter() if _localname(e.tag) == "FareRef" and e.text
    }
    dangling_fares = sorted(fare_refs - fare_codes)
    report.checks.append(
        CheckResult(
            "rs.fare_refs_resolve",
            not dangling_fares,
            f"dangling fare refs: {dangling_fares}"
            if dangling_fares
            else f"{len(fare_refs)} fare refs OK",
        )
    )

    has_offers = any(_localname(e.tag) == "Offers" for e in root.iter())
    report.checks.append(
        CheckResult(
            "rs.has_offers_element",
            has_offers,
            "Offers element present" if has_offers else "no Offers element",
        )
    )
    return report


def validate_against_request(
    request: AirShoppingRequest,
    response: AirShoppingResponse,
) -> ValidationReport:
    """Check that offered itineraries actually respect the request's criteria."""
    report = ValidationReport()
    prefs = request.preferences
    requested_cabin = request.cabin

    if response.itineraries:
        report.checks.append(CheckResult("rs.has_offers", True, f"{response.offer_count} offer(s)"))
    else:
        report.checks.append(
            CheckResult(
                "rs.has_offers",
                bool(response.warnings),
                "no offers, but a warning was returned",
            )
        )

    if requested_cabin:
        bad = [
            it.itinerary_id
            for it in response.itineraries
            if it.cabin != requested_cabin
        ]
        report.checks.append(
            CheckResult(
                "rs.cabin_matches_request",
                not bad,
                f"non-matching: {bad}" if bad else f"all in cabin {requested_cabin}",
            )
        )

    if prefs.direct_only:
        bad = [it.itinerary_id for it in response.itineraries if it.connections != 0]
        report.checks.append(
            CheckResult("rs.direct_only", not bad, f"connecting offers: {bad}" if bad else "all direct")
        )
    elif prefs.max_connections is not None:
        bad = [
            it.itinerary_id
            for it in response.itineraries
            if it.connections > prefs.max_connections
        ]
        report.checks.append(
            CheckResult(
                "rs.max_connections",
                not bad,
                f"too many connections: {bad}" if bad else f"<= {prefs.max_connections}",
            )
        )

    if prefs.carriers:
        allowed = set(prefs.carriers)
        bad = [
            it.itinerary_id
            for it in response.itineraries
            if it.segments[0].marketing_carrier not in allowed
        ]
        report.checks.append(
            CheckResult(
                "rs.carrier_filter",
                not bad,
                f"disallowed carriers in: {bad}" if bad else f"only {sorted(allowed)}",
            )
        )

    if prefs.max_price is not None:
        bad = [it.itinerary_id for it in response.itineraries if it.price > prefs.max_price]
        report.checks.append(
            CheckResult(
                "rs.max_price",
                not bad,
                f"over budget: {bad}" if bad else f"<= {prefs.max_price}",
            )
        )

    # Every offer's price must be positive and its duration consistent with segments.
    bad_price = [it.itinerary_id for it in response.itineraries if it.price <= 0]
    report.checks.append(
        CheckResult(
            "rs.prices_positive",
            not bad_price,
            f"non-positive prices: {bad_price}" if bad_price else f"{response.offer_count} priced",
        )
    )

    bad_duration = []
    for it in response.itineraries:
        if not it.segments:
            bad_duration.append(it.itinerary_id)
            continue
        start = datetime.fromisoformat(it.segments[0].departure)
        end = datetime.fromisoformat(it.segments[-1].arrival)
        expected = int((end - start).total_seconds() // 60)
        if abs(expected - it.total_duration_minutes) > 1:
            bad_duration.append(it.itinerary_id)
    report.checks.append(
        CheckResult(
            "rs.duration_consistent",
            not bad_duration,
            f"inconsistent: {bad_duration}" if bad_duration else "all itineraries consistent",
        )
    )

    # Segment chaining: each segment must start where the previous one ended.
    bad_chain = []
    for it in response.itineraries:
        for prev, nxt in zip(it.segments, it.segments[1:]):
            if prev.destination != nxt.origin:
                bad_chain.append(it.itinerary_id)
                break
    report.checks.append(
        CheckResult(
            "rs.segments_chain",
            not bad_chain,
            f"broken chains: {bad_chain}" if bad_chain else "all segments chain",
        )
    )

    return report
