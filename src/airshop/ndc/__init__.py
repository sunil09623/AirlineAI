"""NDC AirShoppingRQ/RS domain layer: models, XML parser/builder, engine, validator."""

from airshop.ndc.builder import build_air_shopping_rq, build_air_shopping_rs
from airshop.ndc.engine import shop
from airshop.ndc.models import (
    AirShoppingRequest,
    AirShoppingResponse,
    Itinerary,
    OriginDest,
    Preferences,
    Segment,
    Traveler,
)
from airshop.ndc.parser import parse_air_shopping_rq
from airshop.ndc.validator import (
    ValidationReport,
    validate_against_request,
    validate_rq,
    validate_rs_xml,
)

__all__ = [
    "AirShoppingRequest",
    "AirShoppingResponse",
    "Itinerary",
    "OriginDest",
    "Preferences",
    "Segment",
    "Traveler",
    "ValidationReport",
    "build_air_shopping_rq",
    "build_air_shopping_rs",
    "parse_air_shopping_rq",
    "shop",
    "validate_against_request",
    "validate_rq",
    "validate_rs_xml",
]
