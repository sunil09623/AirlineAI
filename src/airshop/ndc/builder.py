"""Build IATA NDC ``AirShoppingRS`` XML from normalized response models.

Output follows the NDC message pair structure: PayloadAttributes / Response, with
DataLists holding flight segments and reference lists, and Offers holding priced
itineraries that reference those lists by ID. That reference-based layout is what
makes NDC a graph rather than a tree, and it is deliberately preserved here so
downstream consumers exercise real linking behaviour.
"""

from __future__ import annotations

from datetime import datetime, timezone

from lxml import etree

from airshop.data.reference import CABINS, CARRIERS, CURRENCIES
from airshop.ndc.models import (
    AirShoppingRequest,
    AirShoppingResponse,
    Itinerary,
)

IATA_NS = "http://www.iata.org/IATA/2015/00/2019.1"
COUNTRIES_NS = "http://www.iata.org/IATA/2015/00/2019.1/Country"
CURRENCY_NS = "http://www.iata.org/IATA/2015/00/2019.1/Currency"

NSMAP = {
    "iata": IATA_NS,
    "cns": COUNTRIES_NS,
    "cur": CURRENCY_NS,
}


def _sub(parent: etree._Element, tag: str, text: str | None = None) -> etree._Element:
    el = etree.SubElement(parent, tag)
    if text is not None:
        el.text = text
    return el


def _fmt_dt(value: str) -> str:
    """Normalize a datetime string to NDC's ``YYYY-MM-DDTHH:MM:SS`` form."""
    try:
        return datetime.fromisoformat(value).replace(tzinfo=None).isoformat(
            timespec="seconds"
        )
    except ValueError:
        return value


def build_air_shopping_rs(
    response: AirShoppingResponse,
    request: AirShoppingRequest | None = None,
) -> str:
    """Render an :class:`AirShoppingResponse` as AirShoppingRS XML."""
    root = etree.Element(f"{{{IATA_NS}}}AirShoppingRS", nsmap=NSMAP)
    root.set("Version", "21.3")

    _add_metadata(root, response, request)
    response_el = _sub(root, "Response")
    _add_data_lists(response_el, response, request)
    _add_offers(response_el, response)
    if response.warnings:
        _add_warnings(response_el, response)

    etree.indent(root, space="  ")
    xml = etree.tostring(root, pretty_print=True, xml_declaration=True, encoding="UTF-8")
    return xml.decode("utf-8")


def _add_metadata(
    root: etree._Element,
    response: AirShoppingResponse,
    request: AirShoppingRequest | None,
) -> None:
    pa = _sub(root, "PayloadAttributes")
    _sub(pa, "TrxID", response.transaction_id)
    _sub(pa, "CorrelationID", response.transaction_id)
    _sub(pa, "Version", "21.3")
    _sub(pa, "Timestamp", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    if request is not None:
        sender = _sub(pa, "Sender")
        system = _sub(sender, "EnabledSystem")
        _sub(system, "SystemID", "airshop-local")


def _add_data_lists(
    response_el: etree._Element,
    response: AirShoppingResponse,
    request: AirShoppingRequest | None,
) -> None:
    dl = _sub(response_el, "DataLists")

    # PaxList — PTCs come from the request; fall back to a single adult.
    pax_list = _sub(dl, "PaxList")
    pax_types = _request_pax_types(request)
    for i, ptc in enumerate(pax_types, start=1):
        pax = _sub(pax_list, "Pax")
        _sub(pax, "PaxID", f"{ptc}{i}")
        _sub(pax, "PTC", ptc)

    # OriginDestList.
    seen_od: set[tuple[str, str]] = set()
    od_list = _sub(dl, "OriginDestList")
    for it in response.itineraries:
        key = (it.origin, it.destination)
        if key in seen_od:
            continue
        seen_od.add(key)
        od = _sub(od_list, "OriginDest")
        _sub(od, "OriginCode", it.origin)
        _sub(od, "DestCode", it.destination)
        _sub(od, "OriginDestID", f"OD_{it.origin}{it.destination}")
        journey_ref = _sub(od, "PaxJourneyRefID")
        journey_ref.text = f"PJ_{it.origin}{it.destination}_{it.departure_date}"

    # FlightSegmentList.
    seg_list = _sub(dl, "FlightSegmentList")
    emitted: set[str] = set()
    for it in response.itineraries:
        for seg in it.segments:
            if seg.segment_ref in emitted:
                continue
            emitted.add(seg.segment_ref)
            fs = _sub(seg_list, "FlightSegment")
            _sub(fs, "SegmentID", seg.segment_ref)
            dep = _sub(fs, "Departure")
            _sub(dep, "AirportCode", seg.origin)
            _sub(dep, "Date", seg.departure[:10])
            _sub(dep, "Time", seg.departure[11:19] or "00:00:00")
            arr = _sub(fs, "Arrival")
            _sub(arr, "AirportCode", seg.destination)
            _sub(arr, "Date", seg.arrival[:10])
            _sub(arr, "Time", seg.arrival[11:19] or "00:00:00")
            _sub(fs, "MarketingCarrierAirlineID", seg.marketing_carrier)
            marketing = CARRIERS.get(seg.marketing_carrier, (seg.marketing_carrier, ()))
            _sub(fs, "MarketingCarrierName", marketing[0])
            _sub(fs, "MarketingFlightNumber", seg.flight_number)
            _sub(fs, "OperatingCarrierAirlineID", seg.operating_carrier)
            _sub(fs, "AircraftTypeCode", seg.aircraft)
            _sub(fs, "CabinTypeCode", seg.cabin)
            _sub(fs, "CabinTypeName", CABINS.get(seg.cabin, seg.cabin))
            _sub(fs, "BookingClassCode", seg.booking_class)
            _sub(fs, "Duration", f"PT{seg.duration_minutes}M")

    # FareList — fare basis + refundability flags.
    fare_list = _sub(dl, "FareList")
    seen_fare: set[str] = set()
    for it in response.itineraries:
        if it.fare_basis_code in seen_fare:
            continue
        seen_fare.add(it.fare_basis_code)
        fare = _sub(fare_list, "Fare")
        _sub(fare, "FareCode", it.fare_basis_code)
        _sub(fare, "FareBasisCode", it.fare_basis_code)
        fare_type = _sub(fare, "FareTypeCode")
        fare_type.text = "PUB"
        fare_detail = _sub(fare, "FareDetail")
        _sub(fare_detail, "Refundable", "true" if it.refundable else "false")
        _sub(fare_detail, "Changeable", "true" if it.changeable else "false")

    _add_currency_metadata(dl, response.currency)


def _request_pax_types(request: AirShoppingRequest | None) -> list[str]:
    if request is None:
        return ["ADT"]
    types: list[str] = []
    for traveler in request.travelers:
        types.extend([traveler.ptc] * traveler.quantity)
    return types or ["ADT"]


def _add_currency_metadata(dl: etree._Element, currency: str) -> None:
    decimals = CURRENCIES.get(currency, 2)
    cur_el = etree.SubElement(dl, f"{{{CURRENCY_NS}}}CurParameter")
    code = etree.SubElement(cur_el, f"{{{CURRENCY_NS}}}CurCode")
    code.text = currency
    dec = etree.SubElement(cur_el, f"{{{CURRENCY_NS}}}DecimalsAllowedNumber")
    dec.text = str(decimals)


def _add_offers(response_el: etree._Element, response: AirShoppingResponse) -> None:
    offers = _sub(response_el, "Offers")
    for it in response.itineraries:
        offer = _sub(offers, "Offer")
        _sub(offer, "OfferID", it.itinerary_id)
        _sub(offer, "OwnerCode", it.segments[0].marketing_carrier)
        _sub(offer, "ValidatingCarrierCode", it.segments[0].marketing_carrier)

        # Associations — the graph edges back to DataLists.
        assoc = _sub(offer, "Associations")
        flight_refs = _sub(assoc, "FlightRefs")
        for seg in it.segments:
            _sub(flight_refs, "FlightSegmentReference").set("ref", seg.segment_ref)
        _sub(assoc, "OriginDestRef", f"PJ_{it.origin}{it.destination}_{it.departure_date}")
        _sub(assoc, "FareRef", it.fare_basis_code)

        offer_item = _sub(offer, "OfferItem")
        _sub(offer_item, "OfferItemID", f"{it.itinerary_id}-OI1")
        _sub(offer_item, "CabinTypeCode", it.cabin)
        _sub(offer_item, "SeatsAvailable", str(it.seats_available))
        _sub(offer_item, "ItineraryDuration", f"PT{it.total_duration_minutes}M")
        _sub(offer_item, "Connections", str(it.connections))

        price = _sub(offer_item, "Price")
        total = _sub(price, "TotalAmount")
        total.set("CurCode", it.currency)
        total.text = f"{it.price:.{CURRENCIES.get(it.currency, 2)}f}"
        base = _sub(price, "BaseAmount")
        base.set("CurCode", it.currency)
        base.text = f"{it.price * 0.8:.{CURRENCIES.get(it.currency, 2)}f}"
        taxes = _sub(price, "TaxAmount")
        taxes.set("CurCode", it.currency)
        taxes.text = f"{it.price * 0.2:.{CURRENCIES.get(it.currency, 2)}f}"


def _add_warnings(response_el: etree._Element, response: AirShoppingResponse) -> None:
    warnings = _sub(response_el, "Warnings")
    for text in response.warnings:
        warn = _sub(warnings, "Warning")
        _sub(warn, "Code", "LOCAL-0001")
        _sub(warn, "MessageText", text)


def build_air_shopping_rq(request: AirShoppingRequest) -> str:
    """Render a normalized request back to AirShoppingRQ XML (used for fixtures)."""
    root = etree.Element(f"{{{IATA_NS}}}AirShoppingRQ", nsmap=NSMAP)
    root.set("Version", "21.3")

    pa = _sub(root, "PayloadAttributes")
    _sub(pa, "TrxID", request.transaction_id)
    _sub(pa, "CorrelationID", request.transaction_id)
    _sub(pa, "Version", "21.3")

    req = _sub(root, "Request")
    flight_req = _sub(req, "FlightRequest")

    for od in request.origin_dests:
        od_el = _sub(flight_req, "OriginDest")
        _sub(od_el, "OriginCode", od.origin)
        _sub(od_el, "DestCode", od.destination)
        _sub(od_el, "DepartureDate", od.departure_date.isoformat())
        if od.cabin:
            _sub(od_el, "CabinTypeName", CABINS.get(od.cabin, od.cabin))

    pax_list = _sub(req, "PaxList")
    idx = 1
    for traveler in request.travelers:
        for _ in range(traveler.quantity):
            pax = _sub(pax_list, "Pax")
            _sub(pax, "PaxID", f"{traveler.ptc}{idx}")
            _sub(pax, "PTC", traveler.ptc)
            idx += 1

    prefs = request.preferences
    criteria = _sub(req, "ShoppingCriteria")
    cur = _sub(criteria, "CurParameter")
    _sub(cur, "CurCode", prefs.currency)
    _sub(criteria, "MaxConnections", str(prefs.max_connections))
    if prefs.direct_only:
        _sub(criteria, "DirectOnly", "true")
    if prefs.max_price is not None:
        _sub(criteria, "MaxPrice", f"{prefs.max_price:.2f}")
    if prefs.carriers:
        carrier_criteria = _sub(criteria, "CarrierCriteria")
        for code in prefs.carriers:
            carrier = _sub(carrier_criteria, "Carrier")
            _sub(carrier, "AirlineDesigCode", code)

    etree.indent(root, space="  ")
    xml = etree.tostring(root, pretty_print=True, xml_declaration=True, encoding="UTF-8")
    return xml.decode("utf-8")
