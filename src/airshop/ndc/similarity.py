"""Fingerprinting and similarity matching for AirShopping responses.

The reviewer's real problem is a *library* of baselines plus a stream of new
releases. Comparing a new response against the wrong baseline produces a
meaningless report, so the tool needs to choose the nearest-looking baseline
automatically and be able to justify that choice.

A baseline and a new response "look alike" when they answer a similar request.
This module extracts a content-independent fingerprint and scores pairs across
independent signals, renormalising so a signal that one side does not carry
(a response with no origin/destination list, say) is skipped rather than counted
as a mismatch.

Every score is explainable: :class:`MatchReason` records what each signal
contributed, so the UI can show *why* a baseline was chosen instead of just
asserting a number.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from lxml import etree

from airshop.ndc.diff import (
    ENTITY_KEYS,
    collect_entity_ids,
    flatten,
)
from airshop.ndc.trip import trip_shape_from_rs
from airshop.ndc.xmlutil import localname

# Relative importance of each signal.
#
# Signals describing *what was asked for* (route, airports, dates, trip type,
# passengers, cabin, currency) are far more diagnostic of "same kind of request"
# than signals describing *how it was answered* (node-path shape, entity mix,
# offer count). The latter are near-identical across any two responses from the
# same schema, so they barely discriminate - weighting them equally compressed
# every score into a narrow band and let a wrong route win on incidental
# agreement. Request-side signals therefore dominate.
WEIGHTS: dict[str, float] = {
    "routes": 6.0,
    "dates": 3.0,
    "airports": 2.5,
    "trip_type": 2.5,
    "pax": 2.0,
    "carriers": 2.0,
    "cabins": 2.0,
    "currency": 1.5,
    "entities": 1.5,
    "hops": 1.0,
    "structure": 1.0,
    "offer_count": 1.0,
}

# Below this, the match is weak enough that the reviewer should be told.
WEAK_MATCH_THRESHOLD = 0.45

# Signals that are decisive on their own. Two requests sharing no airport at all
# are for different markets, however similar their schemas are, so when every
# available decisive signal scores zero the total is capped rather than allowed
# to accumulate credit from schema-level agreement.
DECISIVE_SIGNALS: tuple[str, ...] = ("routes", "airports")
MISMATCH_CAP = 0.25

# Below this total available weight, too few signals were comparable for the
# score to mean much.
LOW_CONFIDENCE_WEIGHT = 6.0


@dataclass
class Fingerprint:
    """Content-independent signature of an AirShopping message."""

    label: str = ""
    root: str = ""
    trip_type: str = "unknown"
    routes: set[tuple[str, str]] = field(default_factory=set)
    hops: set[tuple[str, str]] = field(default_factory=set)
    airports: set[str] = field(default_factory=set)
    pax: dict[str, int] = field(default_factory=dict)
    cabins: set[str] = field(default_factory=set)
    carriers: set[str] = field(default_factory=set)
    currency: str = ""
    dates: set[str] = field(default_factory=set)
    structs: set[str] = field(default_factory=set)
    entity_counts: dict[str, int] = field(default_factory=dict)
    offer_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "root": self.root,
            "trip_type": self.trip_type,
            "routes": sorted(f"{o}->{d}" for o, d in self.routes),
            "hops": sorted(f"{o}->{d}" for o, d in self.hops),
            "airports": sorted(self.airports),
            "pax": dict(self.pax),
            "cabins": sorted(self.cabins),
            "carriers": sorted(self.carriers),
            "currency": self.currency,
            "dates": sorted(self.dates),
            "struct_count": len(self.structs),
            "entity_counts": dict(self.entity_counts),
            "offer_count": self.offer_count,
        }

    def describe(self) -> str:
        """One-line human summary, for the UI and for match explanations."""
        routes = ", ".join(f"{o}->{d}" for o, d in sorted(self.routes)[:4]) or "no routes"
        if len(self.routes) > 4:
            routes += f" (+{len(self.routes) - 4} more)"
        pax = ", ".join(f"{n} {p}" for p, n in sorted(self.pax.items())) or "no pax"
        cabins = ", ".join(sorted(self.cabins)) or "unspecified cabin"
        bits = [self.trip_type, routes, pax, cabins]
        if self.currency:
            bits.append(self.currency)
        if self.offer_count:
            bits.append(f"{self.offer_count} offer(s)")
        return " | ".join(bits)


def _routes_hops_airports(
    root: etree._Element,
) -> tuple[set[tuple[str, str]], set[tuple[str, str]], set[str]]:
    """Requested routes, segment hops and referenced airports.

    These are deliberately kept apart. An ``OriginDest`` pair is what was *asked
    for* and identifies the market. A segment hop is *how it was answered*: the
    same LHR->JFK request can be served non-stop or via CDG, IST and MAD depending
    on availability. Folding hops into the route set made two responses for the
    same market look unrelated, and inflated the similarity of two responses that
    merely shared a hub.
    """
    routes: set[tuple[str, str]] = set()
    hops: set[tuple[str, str]] = set()
    airports: set[str] = set()

    for od in root.iter():
        if not isinstance(od.tag, str) or localname(od.tag) != "OriginDest":
            continue
        origin = dest = None
        for child in od.iter():
            if not isinstance(child.tag, str):
                continue
            name = localname(child.tag)
            if name == "OriginCode" and child.text:
                origin = child.text.strip()
            elif name == "DestCode" and child.text:
                dest = child.text.strip()
        if origin and dest:
            routes.add((origin, dest))
            airports.update((origin, dest))

    for segment in root.iter():
        if not isinstance(segment.tag, str):
            continue
        if localname(segment.tag) != "FlightSegment":
            continue
        # The departure and arrival codes inside one segment form a hop.
        codes = [
            child.text.strip()
            for child in segment.iter()
            if isinstance(child.tag, str)
            and localname(child.tag) == "AirportCode"
            and child.text
        ]
        if len(codes) >= 2:
            hops.add((codes[0], codes[1]))

    # Airports include every code seen, so markets still overlap when a response
    # carries no OriginDest list and only segment data.
    for element in root.iter():
        if (
            isinstance(element.tag, str)
            and localname(element.tag) == "AirportCode"
            and element.text
        ):
            airports.add(element.text.strip())

    return routes, hops, airports


def _dates(root: etree._Element) -> set[str]:
    """Departure dates referenced anywhere in the message."""
    found: set[str] = set()
    for element in root.iter():
        if not isinstance(element.tag, str):
            continue
        name = localname(element.tag)
        if name in {"DepartureDate", "DepartureDateTime"} and element.text:
            found.add(element.text.strip()[:10])
        elif name == "Date" and element.text:
            text = element.text.strip()
            # Segment dates sit next to times, so keep only date-shaped values.
            if len(text) >= 10 and text[4:5] == "-" and text[7:8] == "-":
                found.add(text[:10])
    return found


def fingerprint(xml: str | bytes, label: str = "") -> Fingerprint:
    """Build a fingerprint from an AirShopping message."""
    if isinstance(xml, str):
        xml = xml.encode("utf-8")
    root = etree.fromstring(xml)

    routes, hops, airports = _routes_hops_airports(root)

    cabins: set[str] = set()
    carriers: set[str] = set()
    for element in root.iter():
        if not isinstance(element.tag, str):
            continue
        name = localname(element.tag)
        if name in {"CabinTypeCode", "CabinTypeName"} and element.text:
            cabins.add(element.text.strip())
        elif name in {
            "OwnerCode",
            "ValidatingCarrierCode",
            "MarketingCarrierAirlineID",
            "OperatingCarrierAirlineID",
            "AirlineDesigCode",
        } and element.text:
            carriers.add(element.text.strip())

    currency = ""
    for element in root.iter():
        if (
            isinstance(element.tag, str)
            and localname(element.tag) == "CurCode"
            and element.text
        ):
            currency = element.text.strip()
            break

    shape = trip_shape_from_rs(xml)
    entity_counts = {k: len(v) for k, v in collect_entity_ids(root).items()}

    return Fingerprint(
        label=label,
        root=localname(root.tag),
        trip_type=shape.trip_type,
        routes=routes,
        hops=hops,
        airports=airports,
        pax=shape.pax,
        cabins=cabins,
        carriers=carriers,
        currency=currency,
        dates=_dates(root),
        structs={record.struct for record in flatten(root)},
        entity_counts=entity_counts,
        offer_count=entity_counts.get("Offer", 0),
    )


# --------------------------------------------------------------------------- #
# signal comparisons — each returns 0..1, or None when not comparable
# --------------------------------------------------------------------------- #


def _jaccard(left: set, right: set) -> float | None:
    """Overlap of two sets, or None when either side lacks the signal.

    Returning None rather than 0 when one side is empty is deliberate. A response
    with no carriers listed, or - as real carrier payloads often are - with no
    ``OriginDest`` list at all, has simply not expressed an opinion on that
    signal. Scoring 0 would read as "disagrees about routes" and penalise a
    legitimate baseline for a schema difference, so the signal is skipped and the
    remaining weights renormalise.
    """
    if not left or not right:
        return None
    union = left | right
    if not union:
        return None
    return len(left & right) / len(union)


def _pax_similarity(left: dict[str, int], right: dict[str, int]) -> float | None:
    """1 - normalised L1 distance between passenger mixes."""
    if not left and not right:
        return None
    keys = set(left) | set(right)
    total = sum(max(left.get(k, 0), right.get(k, 0)) for k in keys)
    if total == 0:
        return None
    distance = sum(abs(left.get(k, 0) - right.get(k, 0)) for k in keys)
    return 1.0 - (distance / total)


def _count_similarity(left: int, right: int) -> float | None:
    if left == 0 and right == 0:
        return None
    return 1.0 - abs(left - right) / max(left, right)


def _entity_similarity(left: dict[str, int], right: dict[str, int]) -> float | None:
    """Overlap of entity-type mixes, scaled by the larger side."""
    if not left and not right:
        return None
    keys = set(left) | set(right)
    total = sum(max(left.get(k, 0), right.get(k, 0)) for k in keys)
    if total == 0:
        return None
    overlap = sum(min(left.get(k, 0), right.get(k, 0)) for k in keys)
    return overlap / total


@dataclass
class MatchReason:
    """One signal's contribution to a match score."""

    signal: str
    score: float
    weight: float
    explanation: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal": self.signal,
            "score": round(self.score, 4),
            "weight": self.weight,
            "explanation": self.explanation,
        }


@dataclass
class MatchResult:
    """How well one candidate baseline matches a new response."""

    label: str
    score: float
    reasons: list[MatchReason] = field(default_factory=list)
    available_weight: float = 0.0
    fingerprint: Fingerprint | None = None

    @property
    def weak(self) -> bool:
        return self.score < WEAK_MATCH_THRESHOLD

    @property
    def low_confidence(self) -> bool:
        """Too few comparable signals for the score to be trusted."""
        return self.available_weight < LOW_CONFIDENCE_WEIGHT

    @property
    def confidence(self) -> str:
        if self.low_confidence:
            return "low"
        if self.score >= 0.75:
            return "high"
        if self.score >= WEAK_MATCH_THRESHOLD:
            return "medium"
        return "low"

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "score": round(self.score, 4),
            "confidence": self.confidence,
            "weak": self.weak,
            "low_confidence": self.low_confidence,
            "available_weight": round(self.available_weight, 3),
            "reasons": [r.to_dict() for r in self.reasons],
            "fingerprint": self.fingerprint.to_dict() if self.fingerprint else None,
        }

    def explain(self, top: int = 5) -> str:
        """Readable account of why this baseline scored as it did."""
        lines = [
            f"{self.label}: score {self.score:.2f} ({self.confidence} confidence)"
        ]
        ranked = sorted(self.reasons, key=lambda r: -(r.score * r.weight))
        for reason in ranked[:top]:
            lines.append(
                f"  {reason.signal}: {reason.score:.2f} x{reason.weight:g} "
                f"\u2014 {reason.explanation}"
            )
        if self.low_confidence:
            lines.append(
                "  note: few comparable fields, so treat this as a rough guide"
            )
        return "\n".join(lines)


# Human phrasing per signal, for the explanations.
def _explain(signal: str, score: float, left: Fingerprint, right: Fingerprint) -> str:
    if signal == "routes":
        shared = left.routes & right.routes
        return f"{len(shared)} shared route(s) of {len(left.routes | right.routes)}"
    if signal == "trip_type":
        return f"both {left.trip_type}" if score == 1 else f"{left.trip_type} vs {right.trip_type}"
    if signal == "structure":
        return f"{len(left.structs & right.structs)} shared node paths of {len(left.structs | right.structs)}"
    if signal == "dates":
        return f"{len(left.dates & right.dates)} shared date(s) of {len(left.dates | right.dates)}"
    if signal == "pax":
        return f"{left.pax or 'none'} vs {right.pax or 'none'}"
    if signal == "carriers":
        return f"{sorted(left.carriers & right.carriers) or 'none shared'}"
    if signal == "entities":
        return f"entity mix overlap {score:.0%}"
    if signal == "airports":
        return f"{len(left.airports & right.airports)} shared airport(s)"
    if signal == "hops":
        return f"{len(left.hops & right.hops)} shared connection(s)"
    if signal == "cabins":
        return f"{sorted(left.cabins & right.cabins) or 'none shared'}"
    if signal == "currency":
        return f"{left.currency or 'unset'} vs {right.currency or 'unset'}"
    if signal == "offer_count":
        return f"{left.offer_count} vs {right.offer_count} offer(s)"
    return f"score {score:.2f}"


def score_match(baseline: Fingerprint, candidate: Fingerprint) -> MatchResult:
    """Score how closely ``candidate`` resembles ``baseline``.

    Signals that are unavailable on either side are skipped and the remaining
    weights renormalise, so a response lacking, say, an origin/destination list
    is not penalised for it.
    """
    signals: list[tuple[str, float | None]] = []

    signals.append(("routes", _jaccard(baseline.routes, candidate.routes)))

    if baseline.trip_type != "unknown" and candidate.trip_type != "unknown":
        signals.append(
            ("trip_type", 1.0 if baseline.trip_type == candidate.trip_type else 0.0)
        )
    else:
        signals.append(("trip_type", None))

    signals.append(("structure", _jaccard(baseline.structs, candidate.structs)))
    signals.append(("dates", _jaccard(baseline.dates, candidate.dates)))
    signals.append(("pax", _pax_similarity(baseline.pax, candidate.pax)))
    signals.append(("carriers", _jaccard(baseline.carriers, candidate.carriers)))
    signals.append(
        ("entities", _entity_similarity(baseline.entity_counts, candidate.entity_counts))
    )
    signals.append(("airports", _jaccard(baseline.airports, candidate.airports)))
    signals.append(("hops", _jaccard(baseline.hops, candidate.hops)))
    signals.append(("cabins", _jaccard(baseline.cabins, candidate.cabins)))

    if baseline.currency and candidate.currency:
        signals.append(
            ("currency", 1.0 if baseline.currency == candidate.currency else 0.0)
        )
    else:
        signals.append(("currency", None))

    signals.append(
        ("offer_count", _count_similarity(baseline.offer_count, candidate.offer_count))
    )

    reasons: list[MatchReason] = []
    weighted = 0.0
    available = 0.0
    decisive_seen = 0
    decisive_matched = 0
    for signal, score in signals:
        if score is None:
            continue
        weight = WEIGHTS[signal]
        weighted += score * weight
        available += weight
        if signal in DECISIVE_SIGNALS:
            decisive_seen += 1
            if score > 0:
                decisive_matched += 1
        reasons.append(
            MatchReason(
                signal=signal,
                score=score,
                weight=weight,
                explanation=_explain(signal, score, baseline, candidate),
            )
        )

    overall = weighted / available if available else 0.0

    # Sharing no route and no airport means a different market. Schema-level
    # agreement must not talk the score back up, so cap it and say why.
    capped = decisive_seen > 0 and decisive_matched == 0
    if capped:
        overall = min(overall, MISMATCH_CAP)
        reasons.append(
            MatchReason(
                signal="decisive_mismatch",
                score=0.0,
                weight=0.0,
                explanation=(
                    "no shared route or airport, so this is treated as a different "
                    "market regardless of schema agreement"
                ),
            )
        )

    return MatchResult(
        label=candidate.label,
        score=overall,
        reasons=reasons,
        available_weight=available,
        fingerprint=candidate,
    )


def rank_candidates(
    new_xml: str | bytes,
    baselines: dict[str, str | bytes],
    new_label: str = "new",
) -> list[MatchResult]:
    """Rank every baseline by similarity to ``new_xml``, best first.

    The new message is the reference and each baseline the candidate, so the
    returned results carry the *baseline's* label.
    """
    target = fingerprint(new_xml, new_label)
    results = [
        score_match(target, fingerprint(xml, label))
        for label, xml in baselines.items()
    ]
    results.sort(key=lambda r: (-r.score, r.label))
    return results


def find_nearest(
    new_xml: str | bytes,
    baselines: dict[str, str | bytes],
    new_label: str = "new",
) -> MatchResult | None:
    """Return the single nearest-looking baseline, or None if there are none."""
    ranked = rank_candidates(new_xml, baselines, new_label)
    return ranked[0] if ranked else None


__all__ = [
    "Fingerprint",
    "MatchReason",
    "MatchResult",
    "WEIGHTS",
    "WEAK_MATCH_THRESHOLD",
    "LOW_CONFIDENCE_WEIGHT",
    "fingerprint",
    "score_match",
    "rank_candidates",
    "find_nearest",
]
