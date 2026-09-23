"""Filesystem store for AirShopping transactions.

Every transaction persists its request XML, response XML and a JSON summary.
That gives the agent stable paths to hand back to the user, and gives the
training-data generator a durable corpus to build trajectories from.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from airshop.ndc.models import AirShoppingRequest, AirShoppingResponse


@dataclass(frozen=True)
class TransactionRecord:
    transaction_id: str
    directory: Path
    rq_path: Path
    rs_path: Path
    summary_path: Path
    response_path: Path


class TransactionStore:
    """Persists NDC transactions under ``root`` (one directory each)."""

    def __init__(self, root: Path | str = "runs") -> None:
        self.root = Path(root)

    def _dir_for(self, transaction_id: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in transaction_id)
        return self.root / safe

    def save(
        self,
        request: AirShoppingRequest,
        response: AirShoppingResponse,
        rq_xml: str,
        rs_xml: str,
    ) -> TransactionRecord:
        directory = self._dir_for(request.transaction_id)
        directory.mkdir(parents=True, exist_ok=True)

        rq_path = directory / "airshopping_rq.xml"
        rs_path = directory / "airshopping_rs.xml"
        summary_path = directory / "summary.json"
        response_path = directory / "response.json"

        rq_path.write_text(rq_xml, encoding="utf-8")
        rs_path.write_text(rs_xml, encoding="utf-8")
        response_path.write_text(
            response.model_dump_json(indent=2), encoding="utf-8"
        )

        summary = {
            "transaction_id": request.transaction_id,
            "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "request": request.model_dump(mode="json"),
            "offer_count": response.offer_count,
            "currency": response.currency,
            "warnings": response.warnings,
            "offers": [
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
                    "segments": [s.model_dump(mode="json") for s in it.segments],
                }
                for it in response.itineraries
            ],
        }
        summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

        return TransactionRecord(
            transaction_id=request.transaction_id,
            directory=directory,
            rq_path=rq_path,
            rs_path=rs_path,
            summary_path=summary_path,
            response_path=response_path,
        )

    def record_for(self, transaction_id: str) -> TransactionRecord | None:
        """Return the persisted record for a transaction, or None if absent."""
        directory = self._dir_for(transaction_id)
        rq_path = directory / "airshopping_rq.xml"
        rs_path = directory / "airshopping_rs.xml"
        summary_path = directory / "summary.json"
        response_path = directory / "response.json"
        if not all(p.exists() for p in (rq_path, rs_path, summary_path, response_path)):
            return None
        return TransactionRecord(
            transaction_id=transaction_id,
            directory=directory,
            rq_path=rq_path,
            rs_path=rs_path,
            summary_path=summary_path,
            response_path=response_path,
        )

    def load_response(self, record: TransactionRecord) -> AirShoppingResponse:
        """Rebuild the normalized response from a record's persisted JSON."""
        return AirShoppingResponse.model_validate_json(
            record.response_path.read_text(encoding="utf-8")
        )
