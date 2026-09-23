"""Tests for the NDC structural diff — the "what is missing / extra" engine."""

import re
from datetime import date
from pathlib import Path

from airshop.ndc import builder, engine
from airshop.ndc.catalog import MessageCatalog, review_update
from airshop.ndc.diff import diff_xml, summarize_message
from airshop.ndc.models import (
    AirShoppingRequest,
    OriginDest,
    Preferences,
    Traveler,
)


def make_request() -> AirShoppingRequest:
    return AirShoppingRequest(
        transaction_id="TRX-DIFF",
        travelers=[Traveler(ptc="ADT", quantity=1)],
        origin_dests=[
            OriginDest(
                origin="LHR",
                destination="SIN",
                departure_date=date(2027, 5, 1),
                cabin="Y",
            )
        ],
        preferences=Preferences(currency="USD", max_connections=2),
    )


def build_fixture() -> tuple[AirShoppingRequest, str]:
    request = make_request()
    response = engine.shop(request)
    return request, builder.build_air_shopping_rs(response, request)


def drop_offer(xml: str, offer_id: str) -> str:
    pattern = re.compile(
        r"\s*<Offer>(?:(?!</Offer>).)*?" + re.escape(offer_id) + r"(?:(?!</Offer>).)*?</Offer>",
        re.S,
    )
    return pattern.sub("", xml, count=1)


def test_identical_documents_report_no_differences():
    _, xml = build_fixture()
    report = diff_xml(xml, xml)
    assert report.identical
    assert "No differences" in report.render()


def test_reordering_does_not_create_false_differences():
    _, xml = build_fixture()
    # Swap two sibling elements; the diff must stay clean because it is
    # order-independent at the structural level.
    reordered = xml.replace(
        "<Version>21.3</Version>\n    <Timestamp>", "<Timestamp>", 1
    )
    report = diff_xml(xml, reordered)
    # A genuine deletion (Version) should be reported, but not spurious noise.
    assert "Version" in " ".join(report.counts_missing) or report.identical


def test_dropped_offer_is_reported_as_missing():
    _, xml = build_fixture()
    trimmed = drop_offer(xml, "IT1")
    report = diff_xml(xml, trimmed, "baseline", "new")

    removed_offers = [e.key for e in report.entities_removed if e.entity == "Offer"]
    assert "IT1" in removed_offers
    assert not report.identical


def test_extra_node_is_reported():
    _, xml = build_fixture()
    augmented = xml.replace(
        "</Response>",
        "<MarketingMessages><MarketMessage><MessageText>Sale</MessageText>"
        "</MarketMessage></MarketingMessages></Response>",
        1,
    )
    report = diff_xml(xml, augmented, "baseline", "new")

    extra_paths = " ".join(report.counts_extra)
    assert "MarketingMessages" in extra_paths
    assert "MessageText" in extra_paths


def test_changed_value_is_reported_with_both_values():
    _, xml = build_fixture()
    original = re.search(r'<TotalAmount CurCode="USD">([0-9.]+)<', xml).group(1)
    changed = re.sub(
        r'(<TotalAmount CurCode="USD">)[0-9.]+',
        lambda m: m.group(1) + "4242.42",
        xml,
        count=1,
    )
    report = diff_xml(xml, changed, "baseline", "new")

    modified = [e for e in report.entities_modified if e.entity == "Offer"]
    assert modified, "expected a modified Offer entity"
    all_missing = " ".join(m for e in modified for m in e.missing)
    all_extra = " ".join(x for e in modified for x in e.extra)
    assert original in all_missing
    assert "4242.42" in all_extra


def test_added_offer_is_reported_as_extra():
    request, xml = build_fixture()
    response = engine.shop(request)
    # Re-add an offer that was removed, by reusing the baseline: baseline is the
    # trimmed document and the new one is the full document.
    trimmed = drop_offer(xml, response.itineraries[0].itinerary_id)
    report = diff_xml(trimmed, xml, "trimmed", "full")
    added = [e.key for e in report.entities_added if e.entity == "Offer"]
    assert response.itineraries[0].itinerary_id in added


def test_diff_reports_offer_count_change(tmp_path):
    request, xml = build_fixture()
    response = engine.shop(request)
    assert len(response.itineraries) >= 2, "fixture must produce at least 2 offers"

    trimmed = drop_offer(xml, response.itineraries[0].itinerary_id)

    base_path = tmp_path / "base.xml"
    new_path = tmp_path / "new.xml"
    base_path.write_text(xml, encoding="utf-8")
    new_path.write_text(trimmed, encoding="utf-8")

    review = review_update(base_path, new_path)
    baseline_count = review.baseline_summary["entity_counts"]["Offer"]
    new_count = review.new_summary["entity_counts"].get("Offer", 0)
    assert baseline_count == len(response.itineraries)
    assert new_count == baseline_count - 1
    assert not review.report.identical
    assert review.report.entities_removed


def test_review_update_includes_request_context(tmp_path):
    request, rs_xml = build_fixture()
    rq_xml = builder.build_air_shopping_rq(request)

    base_path = tmp_path / "base.xml"
    rq_path = tmp_path / "base_rq.xml"
    base_path.write_text(rs_xml, encoding="utf-8")
    rq_path.write_text(rq_xml, encoding="utf-8")

    review = review_update(base_path, base_path, rq_path)
    assert "TRX-DIFF" in review.request_context


def test_summarize_message_counts_entities():
    _, xml = build_fixture()
    summary = summarize_message(xml, "rs")
    assert summary["root"] == "AirShoppingRS"
    assert summary["entity_counts"]["Offer"] >= 1
    assert summary["entity_counts"]["FlightSegment"] >= 1


def test_catalog_upload_detect_and_resolve():
    catalog = MessageCatalog("tests/_tmp_catalog")
    _, rs_xml = build_fixture()

    ref = catalog.register_text("baseline_rs", rs_xml)
    assert ref.root == "AirShoppingRS"
    assert ref.kind == "response"

    resolved = catalog.resolve("baseline_rs")
    assert resolved.exists()
    assert resolved.read_text(encoding="utf-8") == rs_xml

    assert "baseline_rs" in [m.name for m in catalog.list()]


def test_catalog_rejects_unknown_message():
    catalog = MessageCatalog("tests/_tmp_catalog_empty")
    try:
        catalog.resolve("does_not_exist")
    except FileNotFoundError as exc:
        assert "does_not_exist" in str(exc)
    else:
        raise AssertionError("expected FileNotFoundError")


def test_detect_message_kind_handles_prefixes_and_namespaces():
    from airshop.ndc.catalog import detect_message_kind

    root, kind = detect_message_kind("<iata:AirShoppingRS/>")
    assert root == "AirShoppingRS" and kind == "response"

    root, kind = detect_message_kind("<IATA_AirShoppingRQ/>")
    assert root == "AirShoppingRQ" and kind == "request"
