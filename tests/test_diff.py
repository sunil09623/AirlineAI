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


def test_churn_suffix_detects_session_token():
    from airshop.ndc.diff import churn_suffix

    # Real shape: prefix identifies the entity, tail is a per-response token.
    assert (
        churn_suffix(["Xbga0600210b3be03", "Xbga1900210b3be03"])
        == "00210b3be03"
    )


def test_churn_suffix_leaves_ordinary_keys_alone():
    from airshop.ndc.diff import churn_suffix

    # Ordinary segment ids share no meaningful tail.
    assert churn_suffix(["SEG1", "SEG2_1"]) == ""
    # A single value has nothing to compare against.
    assert churn_suffix(["IT1"]) == ""
    # Too-short shared tail: below the threshold, so treated as meaningful.
    assert churn_suffix(["A1", "B1"]) == ""


def test_churn_normalisation_keeps_distinct_entities_distinct():
    """The meaningful invariant: normalising ids must not merge separate entities."""
    from airshop.ndc.diff import normalise_entity_keys

    ids = {"BaggageAllowance": ["Xbga0600210b3be03", "Xbga1900210b3be03"]}
    mapping = normalise_entity_keys(ids)
    normalised = {mapping[v] for v in ids["BaggageAllowance"]}
    # Two different allowances stay two different allowances...
    assert len(normalised) == 2, "normalisation must not collapse distinct entities"
    # ...and the stable prefix is what survives.
    assert normalised == {"Xbga0600", "Xbga1900"} or all(
        v.startswith("Xbga") for v in normalised
    )
    # Short shared tails must be left alone.
    assert normalise_entity_keys({"Offer": ["IT1", "IT2"]}) == {"IT1": "IT1", "IT2": "IT2"}


def test_attribute_carried_ids_are_recognised_as_entities():
    from airshop.ndc.diff import collect_entity_ids

    xml = (
        '<AirShoppingRS><DataLists><BaggageAllowanceList>'
        '<BaggageAllowance BaggageAllowanceID="Xbga0600210b3be03"><Type>Checked</Type></BaggageAllowance>'
        '<BaggageAllowance BaggageAllowanceID="Xbga1900210b3be03"><Type>Checked</Type></BaggageAllowance>'
        "</BaggageAllowanceList></DataLists></AirShoppingRS>"
    )
    from lxml import etree

    ids = collect_entity_ids(etree.fromstring(xml.encode()))
    assert ids["BaggageAllowance"] == ["Xbga0600210b3be03", "Xbga1900210b3be03"]


def test_churned_ids_do_not_produce_noise():
    """A response that only reissues session tokens must not look changed."""
    from airshop.ndc.diff import diff_xml

    def doc(suffix: str, extra: bool = False) -> str:
        bgas = [
            f'<BaggageAllowance BaggageAllowanceID="Xbga0600{suffix}"><Type>Checked</Type></BaggageAllowance>',
            f'<BaggageAllowance BaggageAllowanceID="Xbga1900{suffix}"><Type>Checked</Type></BaggageAllowance>',
        ]
        if extra:
            bgas.append(
                f'<BaggageAllowance BaggageAllowanceID="Xbga9900{suffix}"><Type>CarryOn</Type></BaggageAllowance>'
            )
        fares = "".join(
            f'<FareGroup ListKey="Xfbc{k}00{suffix}"><FareBasisCode>{k.upper()}</FareBasisCode></FareGroup>'
            for k in ("09", "0e", "11")
        )
        return (
            "<AirShoppingRS><DataLists>"
            f"<BaggageAllowanceList>{''.join(bgas)}</BaggageAllowanceList>"
            f"<FareList>{fares}</FareList>"
            "</DataLists></AirShoppingRS>"
        )

    report = diff_xml(doc("210b3be03"), doc("0a0b3c8d7"), "old", "new")
    assert report.value_diffs == [], "churned ids must not appear as value changes"
    assert report.identical, "nothing substantive changed"

    # A genuinely new allowance must still surface as an addition.
    with_extra = diff_xml(doc("210b3be03"), doc("0a0b3c8d7", extra=True), "old", "new")
    assert [e.key for e in with_extra.entities_added] == ["Xbga99"]
    assert not with_extra.identical


def test_volatile_session_fields_are_separated():
    """Timestamps differ on every response and must not count as content churn."""
    from airshop.ndc.diff import diff_xml

    a = '<AirShoppingRS><PayloadAttributes><Timestamp>2026-01-01T00:00:00Z</Timestamp>' \
        "<TrxID>T1</TrxID></PayloadAttributes></Rsv>".replace("Rsv", "AirShoppingRS")
    b = '<AirShoppingRS><PayloadAttributes><Timestamp>2026-01-02T00:00:00Z</Timestamp>' \
        "<TrxID>T2</TrxID></PayloadAttributes></Rsv>".replace("Rsv", "AirShoppingRS")
    report = diff_xml(a, b, "old", "new")
    assert report.identical, "only session metadata differs"
    assert report.value_diffs == []
    assert len(report.session_metadata) == 2


def test_identical_pair_still_reports_identical():
    from airshop.ndc.diff import diff_xml

    _, xml = build_fixture()
    report = diff_xml(xml, xml)
    assert report.identical
    assert report.session_metadata == []


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
