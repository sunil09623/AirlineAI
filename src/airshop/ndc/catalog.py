"""Catalog of uploaded NDC messages plus offline comparison.

Holds user-supplied AirShoppingRQ/RS documents on disk so the agent can refer to
them by short name across tool calls, and provides the high-level "what changed"
review that combines message inventory with the structural diff.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from lxml import etree

from airshop.ndc.diff import DiffReport, diff_xml, summarize_message
from airshop.ndc.xmlutil import localname

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")

# Recognised message kinds, keyed by how their root element ends.
MESSAGE_KINDS: dict[str, str] = {
    "AirShoppingRQ": "request",
    "AirShoppingRS": "response",
    "OfferPriceRQ": "request",
    "OfferPriceRS": "response",
    "OrderCreateRQ": "request",
    "OrderCreateRS": "response",
    "OrderReshopRQ": "request",
    "OrderReshopRS": "response",
}


@dataclass(frozen=True)
class MessageRef:
    name: str
    path: Path
    root: str
    kind: str
    saved_at: str


def detect_message_kind(xml: str | bytes) -> tuple[str, str]:
    """Return ``(root_localname, kind)`` for an NDC document."""
    if isinstance(xml, bytes):
        xml = xml.decode("utf-8", errors="replace")
    match = re.search(r"<([A-Za-z_][\w.\-]*:)?([A-Za-z_][\w.\-]*)[\s/>]", xml)
    root = match.group(2) if match else "unknown"
    if root.startswith("IATA_"):
        root = root[len("IATA_") :]
    return root, MESSAGE_KINDS.get(root, "unknown")


class MessageCatalog:
    """Stores uploaded NDC messages under ``root`` for later reference."""

    def __init__(self, root: Path | str = "ndc_messages") -> None:
        self.root = Path(root)

    def _safe(self, name: str) -> str:
        cleaned = _SAFE_NAME.sub("_", name).strip("._")
        return cleaned or "message"

    def register(self, name: str, source: Path | str) -> MessageRef:
        """Copy a document into the catalog under ``name``."""
        source_path = Path(source)
        if not source_path.exists():
            raise FileNotFoundError(f"no such file: {source_path}")
        content = source_path.read_text(encoding="utf-8")

        safe = self._safe(name)
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root / f"{safe}.xml"
        if source_path.resolve() != target.resolve():
            shutil.copyfile(source_path, target)

        root, kind = detect_message_kind(content)
        return MessageRef(
            name=safe,
            path=target,
            root=root,
            kind=kind,
            saved_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )

    def register_text(self, name: str, xml: str) -> MessageRef:
        """Store an inline document under ``name``."""
        safe = self._safe(name)
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root / f"{safe}.xml"
        target.write_text(xml, encoding="utf-8")
        root, kind = detect_message_kind(xml)
        return MessageRef(
            name=safe,
            path=target,
            root=root,
            kind=kind,
            saved_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )

    def resolve(self, name_or_path: str) -> Path:
        """Resolve a catalog name or a filesystem path to an existing file."""
        candidate = Path(name_or_path)
        if candidate.exists() and candidate.is_file():
            return candidate
        named = self.root / f"{self._safe(name_or_path)}.xml"
        if named.exists():
            return named
        # Fall back to a case-insensitive catalog match.
        lowered = self._safe(name_or_path).lower()
        for existing in sorted(self.root.glob("*.xml")):
            if existing.stem.lower() == lowered:
                return existing
        raise FileNotFoundError(
            f"{name_or_path!r} is neither a file nor a registered message; "
            f"registered: {[p.stem for p in sorted(self.root.glob('*.xml'))]}"
        )

    def list(self) -> list[MessageRef]:
        refs: list[MessageRef] = []
        for path in sorted(self.root.glob("*.xml")):
            try:
                content = path.read_text(encoding="utf-8")
            except OSError:
                continue
            root, kind = detect_message_kind(content)
            refs.append(
                MessageRef(
                    name=path.stem,
                    path=path,
                    root=root,
                    kind=kind,
                    saved_at="",
                )
            )
        return refs


@dataclass
class UpdateReview:
    """Result of reviewing a new RS against a baseline RQ/RS pair."""

    baseline_label: str
    new_label: str
    baseline_summary: dict
    new_summary: dict
    report: DiffReport
    request_context: str = ""

    def render(self) -> str:
        parts = [self.report.render()]
        parts.append("\nBaseline inventory:")
        for key, value in self.baseline_summary["entity_counts"].items():
            parts.append(f"  {key}: {value}")
        parts.append("New inventory:")
        for key, value in self.new_summary["entity_counts"].items():
            parts.append(f"  {key}: {value}")
        if self.request_context:
            parts.append(f"\nRequest context:\n{self.request_context}")
        return "\n".join(parts)


def review_update(
    baseline_rs: Path | str,
    new_rs: Path | str,
    baseline_rq: Path | str | None = None,
) -> UpdateReview:
    """Compare a new AirShoppingRS against a baseline, optionally with the RQ."""
    baseline_path = Path(baseline_rs)
    new_path = Path(new_rs)
    baseline_xml = baseline_path.read_text(encoding="utf-8")
    new_xml = new_path.read_text(encoding="utf-8")

    report = diff_xml(
        baseline_xml,
        new_xml,
        base_label=baseline_path.name,
        new_label=new_path.name,
    )

    request_context = ""
    if baseline_rq is not None:
        rq_path = Path(baseline_rq)
        rq_xml = rq_path.read_text(encoding="utf-8")
        root = etree.fromstring(rq_xml.encode("utf-8") if isinstance(rq_xml, str) else rq_xml)
        trx = ""
        for element in root.iter():
            if isinstance(element.tag, str) and localname(element.tag) in {
                "TrxID",
                "CorrelationID",
            }:
                trx = (element.text or "").strip()
                break
        request_context = (
            f"Baseline request {rq_path.name} (TrxID={trx or 'n/a'}) supplies the "
            f"shopping criteria the responses are answers to."
        )

    return UpdateReview(
        baseline_label=baseline_path.name,
        new_label=new_path.name,
        baseline_summary=summarize_message(baseline_xml, baseline_path.name),
        new_summary=summarize_message(new_xml, new_path.name),
        report=report,
        request_context=request_context,
    )
