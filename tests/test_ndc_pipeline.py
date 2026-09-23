"""Tests for the NDC parse/build/shop/validate pipeline."""

import re
from datetime import date

import pytest

from airshop.ndc import builder, engine, parser, validator
from airshop.ndc.models import (
    AirShoppingRequest,
    OriginDest,
    Preferences,
    Traveler,
)


def make_request(**overrides) -> AirShoppingRequest:
    base = dict(
        transaction_id="TRX-TEST",
        travelers=[Traveler(ptc="ADT", quantity=1)],
        origin_dests=[
            OriginDest(
                origin="LHR",
                destination="JFK",
                departure_date=date(2027, 3, 1),
                cabin="C",
            )
        ],
        preferences=Preferences(currency="USD", max_connections=2),
    )
    base.update(overrides)
    return AirShoppingRequest(**base)


def test_rq_xml_roundtrip_preserves_fields():
    request = make_request(
        travelers=[Traveler(ptc="ADT", quantity=2), Traveler(ptc="CHD", quantity=1)]
    )
    parsed = parser.parse_air_shopping_rq(builder.build_air_shopping_rq(request))
    assert parsed.transaction_id == "TRX-TEST"
    assert parsed.pax_type_counts() == {"ADT": 2, "CHD": 1}
    assert parsed.origin_dests[0].origin == "LHR"
    assert parsed.origin_dests[0].destination == "JFK"
    assert parsed.origin_dests[0].cabin == "C"


def test_parser_is_namespace_agnostic():
    xml = """<?xml version="1.0"?>
    <AirShoppingRQ>
      <PayloadAttributes><TrxID>NS-1</TrxID></PayloadAttributes>
      <Request><FlightRequest>
        <OriginDest><OriginCode>AMS</OriginCode><DestCode>SIN</DestCode>
        <DepartureDate>2027-04-02</DepartureDate></OriginDest>
      </FlightRequest></Request>
    </AirShoppingRQ>"""
    parsed = parser.parse_air_shopping_rq(xml)
    assert parsed.transaction_id == "NS-1"
    assert parsed.origin_dests[0].origin == "AMS"


def test_parser_rejects_non_rq_document():
    with pytest.raises(ValueError, match="AirShoppingRQ"):
        parser.parse_air_shopping_rq("<AirShoppingRS/>")


def test_parser_rejects_malformed_xml():
    with pytest.raises(ValueError, match="invalid"):
        parser.parse_air_shopping_rq("<AirShoppingRQ><unclosed>")


def test_engine_is_deterministic():
    request = make_request()
    first = engine.shop(request).itineraries
    second = engine.shop(request).itineraries
    assert [i.model_dump() for i in first] == [i.model_dump() for i in second]


def test_engine_sorts_by_price():
    prices = [o.price for o in engine.shop(make_request()).itineraries]
    assert prices == sorted(prices)


def test_direct_only_filter_is_honoured():
    request = make_request(preferences=Preferences(direct_only=True, max_connections=0))
    response = engine.shop(request)
    assert all(o.connections == 0 for o in response.itineraries)


def test_carrier_filter_is_honoured():
    request = make_request(preferences=Preferences(carriers=["BA"]))
    response = engine.shop(request)
    for offer in response.itineraries:
        assert offer.segments[0].marketing_carrier == "BA"


def test_max_price_filter_is_honoured():
    request = make_request(preferences=Preferences(max_price=900.0))
    response = engine.shop(request)
    assert all(o.price <= 900.0 for o in response.itineraries)


def test_unknown_codes_are_rejected():
    with pytest.raises(ValueError):
        OriginDest(origin="ZZZ", destination="JFK", departure_date=date(2027, 1, 1))
    with pytest.raises(ValueError):
        Preferences(carriers=["ZZ"])
    with pytest.raises(ValueError):
        Preferences(currency="XYZ")


def test_rs_xml_passes_full_validation():
    request = make_request()
    response = engine.shop(request)
    rs_xml = builder.build_air_shopping_rs(response, request)
    assert validator.validate_rq(request).passed
    assert validator.validate_rs_xml(rs_xml).passed
    assert validator.validate_against_request(request, response).passed


@pytest.mark.parametrize(
    "constraint",
    [
        Preferences(direct_only=True, max_connections=0),
        Preferences(max_connections=0),
        Preferences(carriers=["BA"]),
        Preferences(max_price=1200.0),
    ],
)
def test_filters_still_validate(constraint):
    request = make_request(preferences=constraint)
    response = engine.shop(request)
    rs_xml = builder.build_air_shopping_rs(response, request)
    assert validator.validate_rs_xml(rs_xml).passed
    assert validator.validate_against_request(request, response).passed


def test_validator_detects_dangling_segment_reference():
    request = make_request()
    rs_xml = builder.build_air_shopping_rs(engine.shop(request), request)
    # Point the first segment reference at an ID that is not declared in DataLists.
    broken = re.sub(
        r'(<FlightSegmentReference ref=")[^"]+(")',
        r"\1SEG-DOES-NOT-EXIST\2",
        rs_xml,
        count=1,
    )
    report = validator.validate_rs_xml(broken)
    assert not report.passed
    assert any("segment_refs_resolve" in c.name and not c.passed for c in report.checks)


def test_validator_detects_cabin_mismatch():
    request = make_request()
    response = engine.shop(request)
    for itinerary in response.itineraries:
        itinerary.cabin = "F"
    report = validator.validate_against_request(request, response)
    assert not report.passed


def test_validator_flags_broken_segment_chain():
    request = make_request(preferences=Preferences(max_connections=2))
    response = engine.shop(request)
    response.itineraries[0].segments[0].destination = "GRU"
    report = validator.validate_against_request(request, response)
    assert any(c.name == "rs.segments_chain" and not c.passed for c in report.checks)
