"""Custom OpenHands SDK tools that expose the NDC AirShopping layer to an agent.

Each tool wraps one deterministic capability (shop, parse, validate, reference
lookup) so the agent orchestrates them rather than hand-writing XML. All tools
share a :class:`TransactionStore` through the executor so artifacts from one
call are available to the next.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date as Date
from pathlib import Path
from typing import ClassVar

from pydantic import Field, field_validator

from lxml import etree

from openhands.sdk import Action, ImageContent, Observation, TextContent
from openhands.sdk.tool import (
    ToolAnnotations,
    ToolDefinition,
    ToolExecutor,
    register_tool,
)

from airshop.data.reference import AIRPORTS, CABINS, CARRIERS, PASSENGER_TYPES
from airshop.ndc import builder, engine, parser, validator
from airshop.ndc.catalog import MessageCatalog, review_update
from airshop.ndc.models import (
    AirShoppingRequest,
    OriginDest,
    Preferences,
    Traveler,
)
from airshop.store import TransactionStore

_READ_ONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
_WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
)


# --------------------------------------------------------------------------- #
# airshopping_query
# --------------------------------------------------------------------------- #


class AirShoppingQueryAction(Action):
    """Structured shopping request; the tool turns it into NDC XML."""

    transaction_id: str = Field(description="Transaction/correlation ID, e.g. TRX-001.")
    origin: str = Field(description="Origin IATA airport code, e.g. LHR.")
    destination: str = Field(description="Destination IATA airport code, e.g. JFK.")
    departure_date: str = Field(description="Departure date, ISO format YYYY-MM-DD.")
    travelers: dict[str, int] = Field(
        default_factory=lambda: {"ADT": 1},
        description="Passenger type code -> count, e.g. {'ADT': 2, 'CHD': 1}.",
    )
    cabin: str | None = Field(
        default=None,
        description="Cabin code: Y (economy), S (premium), C/J (business), F (first).",
    )
    currency: str = Field(default="USD", description="3-letter currency code.")
    max_connections: int = Field(default=2, ge=0, le=4)
    direct_only: bool = Field(default=False, description="Only non-stop itineraries.")
    carriers: list[str] = Field(
        default_factory=list, description="Restrict to these carrier codes."
    )
    max_price: float | None = Field(default=None, gt=0, description="Budget ceiling.")
    top_n: int = Field(default=10, ge=1, le=50, description="Offers to return.")

    @field_validator("cabin", mode="before")
    @classmethod
    def _normalize_cabin(cls, v):
        """Accept 'C/J', 'business', 'economy' and map to a valid cabin code."""
        if v is None or not isinstance(v, str):
            return v
        token = v.strip().upper()
        if token in {"Y", "S", "C", "J", "F"}:
            return token
        words = {"ECONOMY": "Y", "PREMIUM": "S", "BUSINESS": "C", "FIRST": "F"}
        for word, code in words.items():
            if word in token:
                return code
        return v


class AirShoppingQueryObservation(Observation):
    transaction_id: str
    response_id: str = ""
    offer_count: int = 0
    currency: str = ""
    warnings: list[str] = Field(default_factory=list)
    offers: list[dict] = Field(default_factory=list)
    rq_path: str = ""
    rs_path: str = ""
    summary_path: str = ""

    @property
    def to_llm_content(self) -> Sequence[TextContent | ImageContent]:
        lines = [
            f"Transaction {self.transaction_id} — {self.offer_count} offer(s) "
            f"in {self.currency}",
        ]
        if not self.offers:
            lines.append("No offers matched the criteria.")
        for offer in self.offers:
            stops = "non-stop" if offer["connections"] == 0 else f"{offer['connections']} stop(s)"
            lines.append(
                f"- {offer['itinerary_id']} | {offer['carrier']} | cabin {offer['cabin']} | "
                f"{offer['price']:.2f} {self.currency} | {stops} | "
                f"{offer['duration_minutes']} min | {offer['departure']} -> {offer['arrival']} | "
                f"fare {offer['fare_basis_code']} | "
                f"{'refundable' if offer['refundable'] else 'non-refundable'}"
            )
        for warning in self.warnings:
            lines.append(f"WARNING: {warning}")
        lines.append(f"Artifacts: RQ={self.rq_path} RS={self.rs_path} summary={self.summary_path}")
        return [TextContent(text="\n".join(lines))]


class AirShoppingQueryExecutor(ToolExecutor[AirShoppingQueryAction, AirShoppingQueryObservation]):
    def __init__(self, store: TransactionStore) -> None:
        self.store = store

    def __call__(
        self,
        action: AirShoppingQueryAction,
        conversation=None,  # noqa: ARG002
    ) -> AirShoppingQueryObservation:
        try:
            travelers = [
                Traveler(ptc=ptc, quantity=qty)
                for ptc, qty in action.travelers.items()
                if qty > 0
            ]
            if not travelers:
                travelers = [Traveler(ptc="ADT", quantity=1)]

            request = AirShoppingRequest(
                transaction_id=action.transaction_id,
                travelers=travelers,
                origin_dests=[
                    OriginDest(
                        origin=action.origin,
                        destination=action.destination,
                        departure_date=Date.fromisoformat(action.departure_date),
                        cabin=action.cabin,
                    )
                ],
                preferences=Preferences(
                    currency=action.currency.upper(),
                    max_connections=action.max_connections,
                    direct_only=action.direct_only,
                    carriers=[c.upper() for c in action.carriers],
                    max_price=action.max_price,
                ),
            )
        except ValueError as exc:
            return AirShoppingQueryObservation(
                transaction_id=action.transaction_id,
                warnings=[f"Invalid request: {exc}"],
            )

        response = engine.shop(request)
        rq_xml = builder.build_air_shopping_rq(request)
        rs_xml = builder.build_air_shopping_rs(response, request)
        record = self.store.save(request, response, rq_xml, rs_xml)

        offers = [
            {
                "itinerary_id": it.itinerary_id,
                "carrier": it.segments[0].marketing_carrier,
                "cabin": it.cabin,
                "price": it.price,
                "currency": it.currency,
                "connections": it.connections,
                "duration_minutes": it.total_duration_minutes,
                "departure": it.segments[0].departure,
                "arrival": it.segments[-1].arrival,
                "fare_basis_code": it.fare_basis_code,
                "refundable": it.refundable,
                "seats_available": it.seats_available,
            }
            for it in response.itineraries[: action.top_n]
        ]

        return AirShoppingQueryObservation(
            transaction_id=response.transaction_id,
            response_id=response.response_id,
            offer_count=response.offer_count,
            currency=response.currency,
            warnings=response.warnings,
            offers=offers,
            rq_path=str(record.rq_path),
            rs_path=str(record.rs_path),
            summary_path=str(record.summary_path),
        )


_AIRSHOPPING_QUERY_DESCRIPTION = """Run a complete NDC AirShopping transaction locally.

Give the structured trip parameters; the tool builds the AirShoppingRQ XML, runs the
shopping engine, renders the AirShoppingRS XML, and persists all artifacts. It returns
the ranked offers plus the file paths of the request, response and JSON summary.

Use this whenever the user asks to search or shop for flights. Offers come back sorted
by price. `cabin` accepts Y/S/C/J/F. `travelers` maps passenger type codes (ADT, CHD,
INF, YTH, SRC) to counts. Set `direct_only` for non-stop only.
"""


class AirShoppingTool(ToolDefinition[AirShoppingQueryAction, AirShoppingQueryObservation]):
    """Shop for flights and produce NDC AirShoppingRQ/RS artifacts."""

    name: ClassVar[str] = "airshopping"

    @classmethod
    def create(cls, conv_state=None, store: TransactionStore | None = None, **params):
        if store is None:
            root = Path(conv_state.workspace.working_dir) / "runs" if conv_state else Path("runs")
            store = TransactionStore(root)
        return [
            cls(
                description=_AIRSHOPPING_QUERY_DESCRIPTION,
                action_type=AirShoppingQueryAction,
                observation_type=AirShoppingQueryObservation,
                executor=AirShoppingQueryExecutor(store),
                annotations=_WRITE,
            )
        ]


# --------------------------------------------------------------------------- #
# parse_air_shopping_rq
# --------------------------------------------------------------------------- #


class ParseRqAction(Action):
    xml: str | None = Field(default=None, description="AirShoppingRQ XML content.")
    path: str | None = Field(default=None, description="Path to an AirShoppingRQ XML file.")


class ParseRqObservation(Observation):
    ok: bool = False
    error: str = ""
    request: dict = Field(default_factory=dict)
    validation: list[str] = Field(default_factory=list)

    @property
    def to_llm_content(self) -> Sequence[TextContent | ImageContent]:
        if not self.ok:
            return [TextContent(text=f"Failed to parse AirShoppingRQ: {self.error}")]
        return [
            TextContent(
                text="Parsed AirShoppingRQ:\n"
                + json.dumps(self.request, indent=2)
                + "\n\nValidation:\n"
                + "\n".join(self.validation)
            )
        ]


class ParseRqExecutor(ToolExecutor[ParseRqAction, ParseRqObservation]):
    def __call__(self, action: ParseRqAction, conversation=None) -> ParseRqObservation:  # noqa: ARG002
        xml = action.xml
        if xml is None and action.path:
            try:
                xml = Path(action.path).read_text(encoding="utf-8")
            except OSError as exc:
                return ParseRqObservation(ok=False, error=str(exc))
        if xml is None:
            return ParseRqObservation(ok=False, error="provide either `xml` or `path`")
        try:
            request = parser.parse_air_shopping_rq(xml)
        except ValueError as exc:
            return ParseRqObservation(ok=False, error=str(exc))
        report = validator.validate_rq(request)
        return ParseRqObservation(
            ok=True,
            request=request.model_dump(mode="json"),
            validation=[str(c) for c in report.checks],
        )


_PARSE_DESCRIPTION = """Parse a real NDC AirShoppingRQ XML document into a normalized request.

Accepts either inline `xml` or a file `path`. Returns the transaction ID, passengers,
origin/destination pairs and preferences, plus structural validation results. Use this
to inspect a request the user provides before shopping it.
"""


class ParseAirShoppingRqTool(ToolDefinition[ParseRqAction, ParseRqObservation]):
    """Parse AirShoppingRQ XML into a normalized request."""

    name: ClassVar[str] = "parse_airshopping_rq"

    @classmethod
    def create(cls, conv_state=None, **params):  # noqa: ARG003
        if params:
            raise ValueError("ParseAirShoppingRqTool doesn't accept parameters")
        return [
            cls(
                description=_PARSE_DESCRIPTION,
                action_type=ParseRqAction,
                observation_type=ParseRqObservation,
                executor=ParseRqExecutor(),
                annotations=_READ_ONLY,
            )
        ]


# --------------------------------------------------------------------------- #
# validate_air_shopping
# --------------------------------------------------------------------------- #


class ValidateAction(Action):
    transaction_id: str = Field(
        description="Transaction ID previously produced by airshopping."
    )


class ValidateObservation(Observation):
    ok: bool = False
    error: str = ""
    score: float = 0.0
    passed: bool = False
    checks: list[str] = Field(default_factory=list)

    @property
    def to_llm_content(self) -> Sequence[TextContent | ImageContent]:
        if not self.ok:
            return [TextContent(text=f"Validation failed: {self.error}")]
        header = (
            f"Validation score {self.score:.2f} — "
            f"{'ALL CHECKS PASSED' if self.passed else 'SOME CHECKS FAILED'}"
        )
        return [TextContent(text=header + "\n" + "\n".join(self.checks))]


class ValidateExecutor(ToolExecutor[ValidateAction, ValidateObservation]):
    def __init__(self, store: TransactionStore) -> None:
        self.store = store

    def __call__(self, action: ValidateAction, conversation=None) -> ValidateObservation:  # noqa: ARG002
        record = self.store.record_for(action.transaction_id)
        if record is None:
            return ValidateObservation(
                ok=False,
                error=f"unknown transaction {action.transaction_id!r}; run airshopping first",
            )

        request = parser.parse_air_shopping_rq(record.rq_path.read_text(encoding="utf-8"))
        response = self.store.load_response(record)
        rs_xml = record.rs_path.read_text(encoding="utf-8")

        rq_report = validator.validate_rq(request)
        xml_report = validator.validate_rs_xml(rs_xml)
        conformance = validator.validate_against_request(request, response)

        all_checks = rq_report.checks + xml_report.checks + conformance.checks
        total = len(all_checks)
        passed_count = sum(1 for c in all_checks if c.passed)
        return ValidateObservation(
            ok=True,
            score=passed_count / total if total else 1.0,
            passed=all(c.passed for c in all_checks),
            checks=[str(c) for c in all_checks],
        )


_VALIDATE_DESCRIPTION = """Validate a stored AirShopping transaction.

Checks three layers: the request is coherent, the response XML has no dangling
segment/fare references, and the offers actually honour the requested cabin,
connection limit, carrier and price filters. Returns a pass/fail score per check.

Use this to verify your own work or to audit an existing transaction.
"""


class ValidateAirShoppingTool(ToolDefinition[ValidateAction, ValidateObservation]):
    """Validate stored AirShopping transactions."""

    name: ClassVar[str] = "validate_airshopping"

    @classmethod
    def create(cls, conv_state=None, store: TransactionStore | None = None, **params):
        if store is None:
            root = Path(conv_state.workspace.working_dir) / "runs" if conv_state else Path("runs")
            store = TransactionStore(root)
        return [
            cls(
                description=_VALIDATE_DESCRIPTION,
                action_type=ValidateAction,
                observation_type=ValidateObservation,
                executor=ValidateExecutor(store),
                annotations=_READ_ONLY,
            )
        ]


# --------------------------------------------------------------------------- #
# ndc_reference
# --------------------------------------------------------------------------- #


class ReferenceAction(Action):
    query: str = Field(
        default="all",
        description="What to look up: 'airports', 'carriers', 'cabins', 'passengers' or 'all'.",
    )


class ReferenceObservation(Observation):
    airports: dict = Field(default_factory=dict)
    carriers: dict = Field(default_factory=dict)
    cabins: dict = Field(default_factory=dict)
    passengers: dict = Field(default_factory=dict)

    @property
    def to_llm_content(self) -> Sequence[TextContent | ImageContent]:
        parts = []
        if self.airports:
            parts.append(
                "Airports (code -> city, country, region):\n"
                + ", ".join(
                    f"{code}={city}/{country}" for code, (city, country, _r) in self.airports.items()
                )
            )
        if self.carriers:
            parts.append(
                "Carriers (code -> name):\n"
                + ", ".join(f"{code}={name}" for code, (name, _c) in self.carriers.items())
            )
        if self.cabins:
            parts.append(
                "Cabins (code -> name):\n"
                + ", ".join(f"{code}={name}" for code, name in self.cabins.items())
            )
        if self.passengers:
            parts.append(
                "Passenger types (code -> name):\n"
                + ", ".join(f"{code}={name}" for code, name in self.passengers.items())
            )
        return [TextContent(text="\n\n".join(parts) or "No reference data requested.")]


class ReferenceExecutor(ToolExecutor[ReferenceAction, ReferenceObservation]):
    def __call__(self, action: ReferenceAction, conversation=None) -> ReferenceObservation:  # noqa: ARG002
        q = action.query.strip().lower()
        want_all = q in {"all", ""}
        return ReferenceObservation(
            airports=dict(AIRPORTS) if want_all or q == "airports" else {},
            carriers=dict(CARRIERS) if want_all or q == "carriers" else {},
            cabins=dict(CABINS) if want_all or q == "cabins" else {},
            passengers=dict(PASSENGER_TYPES) if want_all or q == "passengers" else {},
        )


_REFERENCE_DESCRIPTION = """Look up the valid NDC codes this local engine understands.

Returns airport codes (with city/country), carrier codes, cabin codes and passenger
type codes. Call this before shopping so you do not guess an airport or carrier code
that the engine will reject.
"""


class NdcReferenceTool(ToolDefinition[ReferenceAction, ReferenceObservation]):
    """Reference lookup for valid airport/carrier/cabin/passenger codes."""

    name: ClassVar[str] = "ndc_reference"

    @classmethod
    def create(cls, conv_state=None, **params):  # noqa: ARG003
        if params:
            raise ValueError("NdcReferenceTool doesn't accept parameters")
        return [
            cls(
                description=_REFERENCE_DESCRIPTION,
                action_type=ReferenceAction,
                observation_type=ReferenceObservation,
                executor=ReferenceExecutor(),
                annotations=_READ_ONLY,
            )
        ]


# --------------------------------------------------------------------------- #
# upload_ndc_message
# --------------------------------------------------------------------------- #


class UploadMessageAction(Action):
    path: str | None = Field(
        default=None, description="Path to a local NDC XML file to import."
    )
    xml: str | None = Field(
        default=None, description="Inline NDC XML content to import instead of a path."
    )
    name: str = Field(
        description="Short name to register the message under, e.g. 'baseline_rs'."
    )


class UploadMessageObservation(Observation):
    ok: bool = False
    error: str = ""
    name: str = ""
    root: str = ""
    message_kind: str = ""
    path: str = ""
    registered: list[str] = Field(default_factory=list)

    @property
    def to_llm_content(self) -> Sequence[TextContent | ImageContent]:
        if not self.ok:
            return [TextContent(text=f"Upload failed: {self.error}")]
        lines = [
            f"Registered '{self.name}' as a {self.message_kind} message "
            f"(root <{self.root}>).",
            f"Stored at {self.path}",
            f"Catalog now holds: {', '.join(self.registered)}",
        ]
        return [TextContent(text="\n".join(lines))]


class UploadMessageExecutor(ToolExecutor[UploadMessageAction, UploadMessageObservation]):
    def __init__(self, catalog: MessageCatalog) -> None:
        self.catalog = catalog

    def __call__(  # noqa: ARG002
        self, action: UploadMessageAction, conversation=None
    ) -> UploadMessageObservation:
        try:
            if action.xml:
                ref = self.catalog.register_text(action.name, action.xml)
            elif action.path:
                ref = self.catalog.register(action.name, action.path)
            else:
                return UploadMessageObservation(
                    ok=False, error="provide either `path` or `xml`"
                )
        except (OSError, ValueError) as exc:
            return UploadMessageObservation(ok=False, error=str(exc))

        return UploadMessageObservation(
            ok=True,
            name=ref.name,
            root=ref.root,
            message_kind=ref.kind,
            path=str(ref.path),
            registered=[m.name for m in self.catalog.list()],
        )


_UPLOAD_DESCRIPTION = """Register an uploaded NDC XML message so other tools can use it.

Give the file `path` (or inline `xml`) plus a short `name`. The message is copied
into the local catalog and its type (AirShoppingRQ, AirShoppingRS, ...) is detected.
Always upload before comparing or parsing, and reuse the same names afterwards.
This works entirely offline against local files.
"""


class UploadNdcMessageTool(ToolDefinition[UploadMessageAction, UploadMessageObservation]):
    """Import an NDC XML file into the local catalog."""

    name: ClassVar[str] = "upload_ndc_message"

    @classmethod
    def create(cls, conv_state=None, catalog: MessageCatalog | None = None, **params):
        if catalog is None:
            root = (
                Path(conv_state.workspace.working_dir) / "ndc_messages"
                if conv_state
                else Path("ndc_messages")
            )
            catalog = MessageCatalog(root)
        return [
            cls(
                description=_UPLOAD_DESCRIPTION,
                action_type=UploadMessageAction,
                observation_type=UploadMessageObservation,
                executor=UploadMessageExecutor(catalog),
                annotations=_WRITE,
            )
        ]


# --------------------------------------------------------------------------- #
# compare_ndc_messages
# --------------------------------------------------------------------------- #


class CompareAction(Action):
    baseline: str = Field(
        description="Name (or path) of the baseline message, typically the first RS."
    )
    new: str = Field(
        description="Name (or path) of the newly uploaded message to compare."
    )
    baseline_rq: str | None = Field(
        default=None,
        description="Optional name of the baseline AirShoppingRQ for context.",
    )


class CompareObservation(Observation):
    ok: bool = False
    error: str = ""
    identical: bool = False
    missing_nodes: dict = Field(default_factory=dict)
    extra_nodes: dict = Field(default_factory=dict)
    entities_removed: list = Field(default_factory=list)
    entities_added: list = Field(default_factory=list)
    entities_modified: list = Field(default_factory=list)
    value_diffs: list = Field(default_factory=list)
    message: str = ""
    report_path: str = ""

    @property
    def to_llm_content(self) -> Sequence[TextContent | ImageContent]:
        if not self.ok:
            return [TextContent(text=f"Comparison failed: {self.error}")]
        text = self.message
        if self.report_path:
            text += f"\n\nFull report written to {self.report_path}"
        return [TextContent(text=text)]


class CompareExecutor(ToolExecutor[CompareAction, CompareObservation]):
    def __init__(self, catalog: MessageCatalog, reports_dir: Path) -> None:
        self.catalog = catalog
        self.reports_dir = reports_dir

    def __call__(  # noqa: ARG002
        self, action: CompareAction, conversation=None
    ) -> CompareObservation:
        try:
            baseline_path = self.catalog.resolve(action.baseline)
            new_path = self.catalog.resolve(action.new)
            baseline_rq = (
                self.catalog.resolve(action.baseline_rq) if action.baseline_rq else None
            )
        except FileNotFoundError as exc:
            return CompareObservation(ok=False, error=str(exc))

        try:
            review = review_update(baseline_path, new_path, baseline_rq)
        except (OSError, etree.XMLSyntaxError) as exc:
            return CompareObservation(ok=False, error=str(exc))

        report = review.report
        payload = report.to_dict()
        payload["baseline_inventory"] = review.baseline_summary["entity_counts"]
        payload["new_inventory"] = review.new_summary["entity_counts"]

        self.reports_dir.mkdir(parents=True, exist_ok=True)
        report_name = f"diff_{baseline_path.stem}__vs__{new_path.stem}.json"
        report_path = self.reports_dir / report_name
        report_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

        return CompareObservation(
            ok=True,
            identical=report.identical,
            missing_nodes=report.counts_missing,
            extra_nodes=report.counts_extra,
            entities_removed=[e.to_dict() for e in report.entities_removed],
            entities_added=[e.to_dict() for e in report.entities_added],
            entities_modified=[e.to_dict() for e in report.entities_modified],
            value_diffs=[v.to_dict() for v in report.value_diffs],
            message=review.render(),
            report_path=str(report_path),
        )


_COMPARE_DESCRIPTION = """Compare two NDC messages and report exactly what changed.

Answers what is MISSING from the new message and what is EXTRA in it, relative to
the baseline. Two levels of detail:

* Entities — ID-bearing nodes matched across both documents, so an Offer/Segment/
  Fare that disappeared is named precisely, and one that kept its ID but changed
  content is reported field by field.
* Node paths — tag paths whose count changed or whose values changed.

Use this whenever the user uploads a second AirShoppingRS and asks what is
different, missing or newly added. It is order-independent, so a reordered response
reports no false differences, and it works offline on any NDC message.
"""


class CompareNdcMessagesTool(ToolDefinition[CompareAction, CompareObservation]):
    """Structurally diff two NDC messages."""

    name: ClassVar[str] = "compare_ndc_messages"

    @classmethod
    def create(cls, conv_state=None, catalog: MessageCatalog | None = None, **params):
        base = Path(conv_state.workspace.working_dir) if conv_state else Path(".")
        if catalog is None:
            catalog = MessageCatalog(base / "ndc_messages")
        return [
            cls(
                description=_COMPARE_DESCRIPTION,
                action_type=CompareAction,
                observation_type=CompareObservation,
                executor=CompareExecutor(catalog, base / "reports"),
                annotations=_READ_ONLY,
            )
        ]


# --------------------------------------------------------------------------- #
# list_ndc_messages
# --------------------------------------------------------------------------- #


class ListMessagesAction(Action):
    pass


class ListMessagesObservation(Observation):
    messages: list = Field(default_factory=list)

    @property
    def to_llm_content(self) -> Sequence[TextContent | ImageContent]:
        if not self.messages:
            return [TextContent(text="No NDC messages have been uploaded yet.")]
        lines = ["Registered NDC messages:"]
        for m in self.messages:
            lines.append(f"- {m['name']} ({m['kind']}, root <{m['root']}>) at {m['path']}")
        return [TextContent(text="\n".join(lines))]


class ListMessagesExecutor(ToolExecutor[ListMessagesAction, ListMessagesObservation]):
    def __init__(self, catalog: MessageCatalog) -> None:
        self.catalog = catalog

    def __call__(  # noqa: ARG002
        self, action: ListMessagesAction, conversation=None
    ) -> ListMessagesObservation:
        return ListMessagesObservation(
            messages=[
                {
                    "name": m.name,
                    "kind": m.kind,
                    "root": m.root,
                    "path": str(m.path),
                }
                for m in self.catalog.list()
            ]
        )


_LIST_DESCRIPTION = """List the NDC messages currently in the local catalog.

Use this to recall which baselines and new uploads are available before comparing.
"""


class ListNdcMessagesTool(ToolDefinition[ListMessagesAction, ListMessagesObservation]):
    """List locally registered NDC messages."""

    name: ClassVar[str] = "list_ndc_messages"

    @classmethod
    def create(cls, conv_state=None, catalog: MessageCatalog | None = None, **params):
        if catalog is None:
            root = (
                Path(conv_state.workspace.working_dir) / "ndc_messages"
                if conv_state
                else Path("ndc_messages")
            )
            catalog = MessageCatalog(root)
        return [
            cls(
                description=_LIST_DESCRIPTION,
                action_type=ListMessagesAction,
                observation_type=ListMessagesObservation,
                executor=ListMessagesExecutor(catalog),
                annotations=_READ_ONLY,
            )
        ]


# --------------------------------------------------------------------------- #
# toolset registration
# --------------------------------------------------------------------------- #


def build_ndc_tools(conv_state=None, store: TransactionStore | None = None):
    """Create all NDC tools sharing a single transaction store and catalog."""
    base = Path(conv_state.workspace.working_dir) if conv_state else Path(".")
    if store is None:
        store = TransactionStore(base / "runs")
    catalog = MessageCatalog(base / "ndc_messages")
    return [
        NdcReferenceTool.create(conv_state)[0],
        AirShoppingTool.create(conv_state, store=store)[0],
        ParseAirShoppingRqTool.create(conv_state)[0],
        ValidateAirShoppingTool.create(conv_state, store=store)[0],
        UploadNdcMessageTool.create(conv_state, catalog=catalog)[0],
        CompareNdcMessagesTool.create(conv_state, catalog=catalog)[0],
        ListNdcMessagesTool.create(conv_state, catalog=catalog)[0],
    ]


# Names are explicit so they stay stable regardless of class naming.
NDC_TOOL_CLASSES: dict[str, type[ToolDefinition]] = {
    "ndc_reference": NdcReferenceTool,
    "airshopping": AirShoppingTool,
    "parse_airshopping_rq": ParseAirShoppingRqTool,
    "validate_airshopping": ValidateAirShoppingTool,
    "upload_ndc_message": UploadNdcMessageTool,
    "compare_ndc_messages": CompareNdcMessagesTool,
    "list_ndc_messages": ListNdcMessagesTool,
}

for _name, _cls in NDC_TOOL_CLASSES.items():
    register_tool(_name, _cls)


__all__ = [
    "AirShoppingQueryAction",
    "AirShoppingQueryObservation",
    "AirShoppingTool",
    "CompareAction",
    "CompareNdcMessagesTool",
    "CompareObservation",
    "ListMessagesAction",
    "ListNdcMessagesTool",
    "ListMessagesObservation",
    "NDC_TOOL_CLASSES",
    "NdcReferenceTool",
    "ParseAirShoppingRqTool",
    "ParseRqAction",
    "ReferenceAction",
    "UploadMessageAction",
    "UploadMessageObservation",
    "UploadNdcMessageTool",
    "ValidateAction",
    "ValidateAirShoppingTool",
    "build_ndc_tools",
    "_READ_ONLY",
    "_WRITE",
]
