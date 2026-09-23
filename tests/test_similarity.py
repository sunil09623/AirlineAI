"""Tests for baseline similarity matching.

The reviewer keeps a library of baselines and compares each new release against
the nearest-looking one, so the failure that matters is picking the wrong
baseline - or claiming a confident match when there is none.
"""

from datetime import date

from airshop.ndc import builder, engine
from airshop.ndc.models import (
    AirShoppingRequest,
    OriginDest,
    Preferences,
    Traveler,
)
from airshop.ndc.similarity import (
    DECISIVE_SIGNALS,
    Fingerprint,
    LOW_CONFIDENCE_WEIGHT,
    MISMATCH_CAP,
    WEAK_MATCH_THRESHOLD,
    find_nearest,
    fingerprint,
    rank_candidates,
    score_match,
)


def make_rs(
    transaction_id: str,
    origin: str,
    destination: str,
    departure: date,
    cabin: str = "Y",
    pax: tuple[tuple[str, int], ...] = (("ADT", 1),),
    currency: str = "USD",
) -> str:
    request = AirShoppingRequest(
        transaction_id=transaction_id,
        travelers=[Traveler(ptc=ptc, quantity=n) for ptc, n in pax],
        origin_dests=[
            OriginDest(
                origin=origin,
                destination=destination,
                departure_date=departure,
                cabin=cabin,
            )
        ],
        preferences=Preferences(currency=currency),
    )
    return builder.build_air_shopping_rs(engine.shop(request), request)


def library() -> dict[str, str]:
    """Four baselines sharing a schema but covering different markets."""
    return {
        "LHR-JFK": make_rs("B1", "LHR", "JFK", date(2027, 3, 15)),
        "LHR-SIN": make_rs("B2", "LHR", "SIN", date(2027, 3, 15)),
        "DXB-BOM": make_rs("B3", "DXB", "BOM", date(2027, 3, 15)),
        "JNB-GRU": make_rs("B4", "JNB", "GRU", date(2027, 3, 15)),
    }


# --------------------------------------------------------------------------- #
# fingerprinting
# --------------------------------------------------------------------------- #


def test_fingerprint_reads_routes_and_dates():
    f = fingerprint(make_rs("T", "LHR", "JFK", date(2027, 3, 15)), "t")
    assert f.routes == {("LHR", "JFK")}
    assert "2027-03-15" in f.dates
    assert f.trip_type == "one-way"
    assert f.currency == "USD"
    assert f.offer_count > 0


def test_fingerprint_separates_requested_routes_from_segment_hops():
    """A hop is how the request was answered, not what was asked for.

    For a non-stop offer the hop coincides with the requested route, which is
    fine. The distinction matters for connecting itineraries: those introduce hubs
    that were never requested, and those must not enter the route set.
    """
    f = fingerprint(make_rs("T", "LHR", "JFK", date(2027, 3, 15)), "t")
    assert f.routes == {("LHR", "JFK")}, "only the requested pair counts as a route"
    assert f.hops, "segment hops are recorded separately"
    # Every hop either is the requested route or involves an unrequested hub.
    for hop in f.hops:
        assert hop == ("LHR", "JFK") or "LHR" in hop or "JFK" in hop


def test_fingerprint_reads_passengers_and_cabin():
    f = fingerprint(
        make_rs(
            "T",
            "LHR",
            "JFK",
            date(2027, 3, 15),
            cabin="C",
            pax=(("ADT", 2), ("CHD", 1)),
        ),
        "t",
    )
    assert f.pax == {"ADT": 2, "CHD": 1}
    assert "C" in f.cabins or "Business" in f.cabins


def test_fingerprint_is_content_independent():
    """Two responses for the same request differ in seed but fingerprint alike."""
    a = fingerprint(make_rs("AAA", "LHR", "JFK", date(2027, 3, 15)), "a")
    b = fingerprint(make_rs("ZZZ", "LHR", "JFK", date(2027, 3, 15)), "b")
    assert a.routes == b.routes
    assert a.pax == b.pax
    assert a.trip_type == b.trip_type


def test_fingerprint_serialises():
    payload = fingerprint(make_rs("T", "LHR", "JFK", date(2027, 3, 15)), "t").to_dict()
    assert payload["routes"] == ["LHR->JFK"]
    assert payload["trip_type"] == "one-way"
    assert "hops" in payload


# --------------------------------------------------------------------------- #
# scoring
# --------------------------------------------------------------------------- #


def test_identical_messages_score_high():
    xml = make_rs("T", "LHR", "JFK", date(2027, 3, 15))
    result = score_match(fingerprint(xml, "a"), fingerprint(xml, "b"))
    assert result.score > 0.95
    assert result.confidence == "high"
    assert not result.weak


def test_same_market_different_seed_still_matches():
    """The same request answered differently must still look like the same market."""
    a = make_rs("B", "LHR", "JFK", date(2027, 3, 15), cabin="C")
    b = make_rs("N", "LHR", "JFK", date(2027, 3, 15), cabin="C")
    result = score_match(fingerprint(a, "b"), fingerprint(b, "n"))
    assert result.score > 0.6, "same requested route should score well"
    assert not result.weak


def test_different_market_scores_low_despite_identical_schema():
    """Schema agreement must not rescue a response from another market."""
    a = make_rs("A", "JNB", "GRU", date(2027, 3, 15))
    b = make_rs("B", "LHR", "JFK", date(2027, 3, 15))
    result = score_match(fingerprint(a, "a"), fingerprint(b, "b"))
    assert result.score <= MISMATCH_CAP + 1e-9
    assert result.weak
    assert any(r.signal == "decisive_mismatch" for r in result.reasons)


def test_unavailable_signals_are_skipped_not_penalised():
    """A side missing a signal must not be counted as disagreeing about it."""
    bare = Fingerprint(label="bare")
    full = fingerprint(make_rs("T", "LHR", "JFK", date(2027, 3, 15)), "full")
    result = score_match(bare, full)
    compared = {r.signal for r in result.reasons}
    assert "routes" not in compared, "no routes on one side means the signal is skipped"
    assert result.low_confidence


def test_low_confidence_is_reported_when_few_signals_available():
    bare = Fingerprint(label="bare")
    result = score_match(bare, Fingerprint(label="bare2"))
    assert result.available_weight < LOW_CONFIDENCE_WEIGHT
    assert result.confidence == "low"
    assert "rough guide" in result.explain()


def test_explanation_names_the_contributing_signals():
    a = make_rs("A", "LHR", "JFK", date(2027, 3, 15), cabin="C", pax=(("ADT", 2),))
    b = make_rs("B", "LHR", "JFK", date(2027, 3, 15), cabin="C", pax=(("ADT", 2),))
    text = score_match(fingerprint(a, "a"), fingerprint(b, "b")).explain()
    assert "routes" in text
    assert "pax" in text


def test_pax_mismatch_lowers_the_score():
    same_pax = score_match(
        fingerprint(make_rs("A", "LHR", "JFK", date(2027, 3, 15), pax=(("ADT", 2),)), "a"),
        fingerprint(make_rs("B", "LHR", "JFK", date(2027, 3, 15), pax=(("ADT", 2),)), "b"),
    )
    different_pax = score_match(
        fingerprint(make_rs("A", "LHR", "JFK", date(2027, 3, 15), pax=(("ADT", 2),)), "a"),
        fingerprint(
            make_rs("B", "LHR", "JFK", date(2027, 3, 15), pax=(("ADT", 4), ("CHD", 2))), "b"
        ),
    )
    assert different_pax.score < same_pax.score


def test_currency_mismatch_lowers_the_score():
    same = score_match(
        fingerprint(make_rs("A", "LHR", "JFK", date(2027, 3, 15), currency="USD"), "a"),
        fingerprint(make_rs("B", "LHR", "JFK", date(2027, 3, 15), currency="USD"), "b"),
    )
    different = score_match(
        fingerprint(make_rs("A", "LHR", "JFK", date(2027, 3, 15), currency="USD"), "a"),
        fingerprint(make_rs("B", "LHR", "JFK", date(2027, 3, 15), currency="EUR"), "b"),
    )
    assert different.score < same.score


def round_trip_rs() -> str:
    """A response carrying two requested pairs, as a real round trip would.

    Built directly rather than via the local engine, which only shops the first
    origin/destination pair and so cannot represent a multi-leg trip.
    """
    return (
        '<AirShoppingRS Version="21.3"><Response><DataLists><OriginDestList>'
        "<OriginDest><OriginCode>LHR</OriginCode><DestCode>JFK</DestCode>"
        "<OriginDestID>OD1</OriginDestID></OriginDest>"
        "<OriginDest><OriginCode>JFK</OriginCode><DestCode>LHR</DestCode>"
        "<OriginDestID>OD2</OriginDestID></OriginDest>"
        "</OriginDestList></DataLists></Response></AirShoppingRS>"
    )


def test_trip_type_change_lowers_the_score():
    one_way = make_rs("A", "LHR", "JFK", date(2027, 3, 15))
    result = score_match(fingerprint(round_trip_rs(), "rt"), fingerprint(one_way, "ow"))

    trip_reason = next((r for r in result.reasons if r.signal == "trip_type"), None)
    assert trip_reason is not None, "the two trips differ, so trip_type is comparable"
    assert trip_reason.score == 0.0
    assert result.score < 0.75


def test_matching_trip_type_scores_full_marks():
    a = fingerprint(round_trip_rs(), "rt1")
    b = fingerprint(round_trip_rs(), "rt2")
    assert a.trip_type == "round-trip"
    trip_reason = next(r for r in score_match(a, b).reasons if r.signal == "trip_type")
    assert trip_reason.score == 1.0


# --------------------------------------------------------------------------- #
# ranking and selection
# --------------------------------------------------------------------------- #


def test_ranking_selects_the_right_market():
    lib = library()
    for wanted in ("LHR-JFK", "DXB-BOM", "JNB-GRU", "LHR-SIN"):
        origin, destination = wanted.split("-")
        new = make_rs("N", origin, destination, date(2027, 3, 15))
        ranked = rank_candidates(new, lib)
        assert ranked[0].label == wanted, f"expected {wanted}, got {ranked[0].label}"


def test_ranking_is_ordered_and_labelled():
    lib = library()
    new = make_rs("N", "LHR", "JFK", date(2027, 3, 15))
    ranked = rank_candidates(new, lib)
    assert len(ranked) == len(lib)
    scores = [r.score for r in ranked]
    assert scores == sorted(scores, reverse=True)
    assert {r.label for r in ranked} == set(lib)


def test_ranking_prefers_route_over_cabin():
    """Cabin is secondary; the route decides which baseline is the right one."""
    lib = {
        "LHR-JFK-economy": make_rs("A", "LHR", "JFK", date(2027, 3, 15), cabin="Y"),
        "LHR-SIN-business": make_rs("B", "LHR", "SIN", date(2027, 3, 15), cabin="C"),
    }
    new = make_rs("N", "LHR", "JFK", date(2027, 3, 15), cabin="C")
    ranked = rank_candidates(new, lib)
    assert ranked[0].label == "LHR-JFK-economy"


def test_find_nearest_returns_best_and_none_when_empty():
    lib = library()
    new = make_rs("N", "DXB", "BOM", date(2027, 3, 15))
    best = find_nearest(new, lib)
    assert best is not None and best.label == "DXB-BOM"
    assert find_nearest(new, {}) is None


def test_match_result_serialises_for_the_ui():
    lib = library()
    new = make_rs("N", "LHR", "JFK", date(2027, 3, 15))
    payload = rank_candidates(new, lib)[0].to_dict()
    assert payload["label"] == "LHR-JFK"
    assert 0.0 <= payload["score"] <= 1.0
    assert payload["confidence"] in {"high", "medium", "low"}
    assert payload["reasons"]
    assert payload["fingerprint"]["routes"]


def test_decisive_signals_are_declared():
    assert "routes" in DECISIVE_SIGNALS
    assert "airports" in DECISIVE_SIGNALS
    assert WEAK_MATCH_THRESHOLD < 0.5
