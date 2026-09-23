"""Parse real IATA NDC ``AirShoppingRQ`` XML into normalized models.

The parser is namespace-agnostic (some carriers use ``iata:`` prefixes, some use
a default namespace, some use none) and tolerates missing optional elements, so
it works against production-shaped payloads rather than only our samples.
"""

from __future__ import annotations

from datetime import date as Date

from lxml import etree

from airshop.ndc.models import (
    AirShoppingRequest,
    OriginDest,
    Preferences,
    Traveler,
)
from airshop.ndc.xmlutil import find as _find
from airshop.ndc.xmlutil import find_all as _find_all
from airshop.ndc.xmlutil import first as _first
from airshop.ndc.xmlutil import localname as _localname
from airshop.ndc.xmlutil import text as _text


def parse_air_shopping_rq(xml: str | bytes) -> AirShoppingRequest:
    """Parse an AirShoppingRQ document into an :class:`AirShoppingRequest`."""
    if isinstance(xml, str):
        xml = xml.encode("utf-8")
    try:
        root = etree.fromstring(xml)
    except etree.XMLSyntaxError as exc:  # pragma: no cover - surfaced to the tool
        raise ValueError(f"invalid AirShoppingRQ XML: {exc}") from exc

    if _localname(root.tag) not in {"AirShoppingRQ", "IATA_AirShoppingRQ"}:
        raise ValueError(
            f"expected an AirShoppingRQ document, got <{_localname(root.tag)}>"
        )

    trx_el = _first(_find(root, "TrxID"), _find(root, "CorrelationID"))
    transaction_id = _text(trx_el) or "AUTO"

    travelers = _parse_pax(root)
    origin_dests = _parse_origin_dests(root)
    preferences = _parse_preferences(root)

    if not origin_dests:
        raise ValueError("AirShoppingRQ contains no OriginDestination information")
    if not travelers:
        travelers = [Traveler(ptc="ADT", quantity=1)]

    return AirShoppingRequest(
        transaction_id=transaction_id,
        travelers=travelers,
        origin_dests=origin_dests,
        preferences=preferences,
    )


def _parse_pax(root: etree._Element) -> list[Traveler]:
    travelers: list[Traveler] = []

    # Form 1: <PaxList><Pax><PTC><PaxType>ADT</...></Pax>
    for pax in _find_all(root, "Pax"):
        ptc_el = _first(_find(pax, "PaxType"), _find(pax, "PTC"))
        ptc = _text(ptc_el)
        if ptc:
            travelers.append(Traveler(ptc=ptc))

    # Form 2: <Paxs><Pax><PaxID>ADT1</PaxID><PTC>ADT</PTC></Pax>
    if not travelers:
        for pax in _find_all(root, "Paxs"):
            for child in pax:
                if _localname(child.tag) != "Pax":
                    continue
                ptc = _text(_find(child, "PTC"))
                if ptc:
                    travelers.append(Traveler(ptc=ptc))

    # Collapse duplicates into quantities (ADT, ADT, CHD -> 2x ADT, 1x CHD).
    collapsed: dict[str, int] = {}
    for t in travelers:
        collapsed[t.ptc] = collapsed.get(t.ptc, 0) + 1
    return [Traveler(ptc=ptc, quantity=n) for ptc, n in collapsed.items()]


def _parse_origin_dests(root: etree._Element) -> list[OriginDest]:
    origin_dests: list[OriginDest] = []

    # Form 1 (compact): <OriginDest><OriginCode>..</><DestCode>..</><DepartureDate>..</>
    for od in _find_all(root, "OriginDest"):
        origin = _text(_find(od, "OriginCode"))
        dest = _text(_find(od, "DestCode"))
        dep_date = _text(_find(od, "DepartureDate")) or _text(
            _find(od, "DepartureDateTime")
        )
        cabin = _text(_find(od, "CabinTypeName")) or _text(_find(od, "CabinCode"))
        if origin and dest and dep_date:
            origin_dests.append(
                OriginDest(
                    origin=origin,
                    destination=dest,
                    departure_date=_parse_date(dep_date),
                    cabin=_normalize_cabin(cabin),
                )
            )

    # Form 2 (verbose IATA): <OriginDest><OriginCode>..</><DestCode>..</><PaxJourney...>
    # where the date lives in a FlightAssociations / Capabilities block.
    if not origin_dests:
        pairs: list[tuple[str, str]] = []
        for od in _find_all(root, "OriginDest"):
            origin = _text(_find(od, "OriginCode"))
            dest = _text(_find(od, "DestCode"))
            dep_date = _text(
                _find(root, "DepartureDateTime")
            ) or _text(_find(root, "EarliestDepartureDateTime"))
            if origin and dest and dep_date:
                pairs.append((origin, dest))
        for origin, dest in pairs:
            origin_dests.append(
                OriginDest(
                    origin=origin,
                    destination=dest,
                    departure_date=_parse_date(
                        _text(_find(root, "DepartureDateTime"))
                        or _text(_find(root, "EarliestDepartureDateTime"))
                    ),
                )
            )

    return origin_dests


def _parse_preferences(root: etree._Element) -> Preferences:
    prefs = Preferences()

    cur = _text(_find(root, "CurCode")) or _text(_find(root, "CurrencyCode"))
    if cur:
        prefs.currency = cur.upper()

    max_conn = _text(_find(root, "MaxConnections"))
    if max_conn and max_conn.isdigit():
        prefs.max_connections = int(max_conn)

    direct = _text(_find(root, "DirectOnly"))
    if direct and direct.lower() in {"true", "1", "yes"}:
        prefs.direct_only = True
        prefs.max_connections = 0

    max_price = _text(_find(root, "MaxPrice")) or _text(_find(root, "MaxFareAmount"))
    if max_price:
        try:
            prefs.max_price = float(max_price)
        except ValueError:
            pass

    carriers: list[str] = []
    for car in _find_all(root, "AirlineDesigCode"):
        code = _text(car)
        if code:
            carriers.append(code.upper())
    prefs.carriers = sorted(set(carriers))

    return prefs


def _normalize_cabin(cabin: str | None) -> str | None:
    if not cabin:
        return None
    cabin = cabin.strip().upper()
    named = {"ECONOMY": "Y", "PREMIUM": "S", "BUSINESS": "C", "FIRST": "F"}
    if cabin in named:
        return named[cabin]
    return cabin if cabin in {"Y", "S", "C", "J", "F"} else None


def _parse_date(value: str) -> Date:
    return Date.fromisoformat(value[:10])
