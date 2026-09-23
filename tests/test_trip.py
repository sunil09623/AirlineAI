"""Tests for trip-shape and passenger comparison across releases."""

from datetime import date

from airshop.ndc.builder import build_air_shopping_rq, build_air_shopping_rs
from airshop.ndc.engine import shop
from airshop.ndc.models import (
    AirShoppingRequest,
    OriginDest,
    Preferences,
    Traveler,
)
from airshop.ndc.trip import (
    compare_trip_shape,
    trip_shape_from_rq,
    trip_shape_from_rs,
)


def one_way(pax=("ADT",), cabin="Y") -> AirShoppingRequest:
    travelers = [Traveler(ptc=p, quantity=1) for p in pax]
    return AirShoppingRequest(
        transaction_id="TRX-OW",
        travelers=travelers,
        origin_dests=[
            OriginDest(
                origin="LHR",
                destination="JFK",
                departure_date=date(2027, 3, 15),
                cabin=cabin,
            )
        ],
        preferences=Preferences(currency="USD"),
    )


def round_trip(pax=("ADT",), cabin="Y") -> AirShoppingRequest:
    travelers = [Traveler(ptc=p, quantity=1) for p in pax]
    return AirShoppingRequest(
        transaction_id="TRX-RT",
        travelers=travelers,
        origin_dests=[
            OriginDest(
                origin="LHR",
                destination="JFK",
                departure_date=date(2027, 3, 15),
                cabin=cabin,
            ),
            OriginDest(
                origin="JFK",
                destination="LHR",
                departure_date=date(2027, 3, 22),
                cabin=cabin,
            ),
        ],
        preferences=Preferences(currency="USD"),
    )


def multi_city() -> AirShoppingRequest:
    return AirShoppingRequest(
        transaction_id="TRX-MC",
        travelers=[Traveler(ptc="ADT", quantity=1)],
        origin_dests=[
            OriginDest(origin="LHR", destination="JFK", departure_date=date(2027, 3, 15)),
            OriginDest(origin="JFK", destination="CDG", departure_date=date(2027, 3, 20)),
            OriginDest(origin="CDG", destination="LHR", departure_date=date(2027, 3, 25)),
        ],
        preferences=Preferences(currency="EUR"),
    )


def test_one_way_is_classified():
    shape = trip_shape_from_rq(build_air_shopping_rq(one_way()))
    assert shape.trip_type == "one-way"
    assert len(shape.legs) == 1


def test_round_trip_is_classified():
    shape = trip_shape_from_rq(build_air_shopping_rq(round_trip()))
    assert shape.trip_type == "round-trip"
    assert len(shape.legs) == 2


def test_multi_city_is_classified():
    shape = trip_shape_from_rq(build_air_shopping_rq(multi_city()))
    assert shape.trip_type == "multi-city"
    assert len(shape.legs) == 3


def test_pax_mix_and_totals_are_read():
    request = AirShoppingRequest(
        transaction_id="TRX-PAX",
        travelers=[
            Traveler(ptc="ADT", quantity=2),
            Traveler(ptc="CHD", quantity=1),
        ],
        origin_dests=[
            OriginDest(origin="LHR", destination="JFK", departure_date=date(2027, 3, 15))
        ],
    )
    shape = trip_shape_from_rq(build_air_shopping_rq(request))
    assert shape.pax == {"ADT": 2, "CHD": 1}
    assert shape.pax_total == 3
    assert "2 Adult" in shape.pax_summary and "1 Child" in shape.pax_summary


def test_no_change_reports_unchanged():
    a = trip_shape_from_rq(build_air_shopping_rq(one_way()))
    b = trip_shape_from_rq(build_air_shopping_rq(one_way()))
    change = compare_trip_shape(a, b)
    assert not change.changed
    assert "unchanged" in change.render()


def test_trip_type_change_is_detected():
    a = trip_shape_from_rq(build_air_shopping_rq(one_way()))
    b = trip_shape_from_rq(build_air_shopping_rq(round_trip()))
    change = compare_trip_shape(a, b)
    assert change.changed
    assert change.trip_type_changed
    assert [l.describe() for l in change.legs_added] == ["JFK->LHR on 2027-03-22"]
    assert "one-way -> round-trip" in change.render()


def test_pax_change_is_detected():
    a = trip_shape_from_rq(build_air_shopping_rq(one_way(pax=("ADT",))))
    b = trip_shape_from_rq(build_air_shopping_rq(one_way(pax=("ADT", "ADT", "CHD"))))
    change = compare_trip_shape(a, b)
    assert change.pax_added == {"ADT": 1, "CHD": 1}
    assert change.pax_removed == {}
    assert "1 Adult" in change.render()


def test_pax_reduction_is_detected():
    a = trip_shape_from_rq(build_air_shopping_rq(one_way(pax=("ADT", "ADT"))))
    b = trip_shape_from_rq(build_air_shopping_rq(one_way(pax=("ADT",))))
    change = compare_trip_shape(a, b)
    assert change.pax_removed == {"ADT": 1}
    assert change.pax_added == {}


def test_cabin_change_is_detected():
    a = trip_shape_from_rq(build_air_shopping_rq(one_way(cabin="Y")))
    b = trip_shape_from_rq(build_air_shopping_rq(one_way(cabin="C")))
    change = compare_trip_shape(a, b)
    # Cabin is reported with its friendly NDC name, not the single-letter code.
    assert change.cabin_changed == ("Economy", "Business")
    assert "cabin" in change.render()


def test_shape_from_response_is_read():
    """The response is parseable and classified from its own contents.

    Note: the local synthetic engine only shops the first origin/destination
    pair, so an RS it generates for a round trip carries one leg. This test pins
    the response-parsing path itself; against real carrier responses, which
    include every requested pair, the classification reflects the full trip.
    """
    request = one_way()
    response = shop(request)
    shape = trip_shape_from_rs(build_air_shopping_rs(response, request))
    assert shape.source == "response"
    assert shape.trip_type == "one-way"
    assert shape.legs  # reconstructed from the response
    assert shape.pax == {"ADT": 1}


def test_shape_change_serialises():
    a = trip_shape_from_rq(build_air_shopping_rq(one_way()))
    b = trip_shape_from_rq(build_air_shopping_rq(round_trip()))
    payload = compare_trip_shape(a, b).to_dict()
    assert payload["changed"] is True
    assert payload["trip_type_changed"] is True
    assert payload["baseline"]["trip_type"] == "one-way"
    assert payload["new"]["trip_type"] == "round-trip"
