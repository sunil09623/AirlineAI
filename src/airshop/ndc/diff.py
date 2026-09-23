"""Structural diff between two NDC XML messages (e.g. two AirShoppingRS).

Answers two questions about a newly uploaded response, both offline and
deterministically:

* **What is missing?** nodes/values present in the baseline but not in the new one.
* **What is extra?** nodes/values present in the new one but not in the baseline.

Two complementary passes:

1. *Structural pass* — flattens each document into a multiset of
   ``(tag-path, value)`` records and diffs the multisets. It is order-independent
   so a reordered response reports no spurious differences, and it is generic so it
   works on any NDC message, not just AirShopping.

2. *Entity pass* — recognises NDC's ID-bearing collections (Offer/OfferID,
   FlightSegment/SegmentID, Fare/FareCode, Pax/PaxID, OriginDest/OriginDestID) and
   matches them across the two documents by ID. This distinguishes "offer IT3 was
   dropped" from "IT3's price changed", which a flat multiset diff would blur.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from lxml import etree

from airshop.ndc.xmlutil import localname

# ID-bearing NDC collections: element name -> child element holding its key.
ENTITY_KEYS: dict[str, str] = {
    "Offer": "OfferID",
    "OfferItem": "OfferItemID",
    "FlightSegment": "SegmentID",
    "Fare": "FareCode",
    "Pax": "PaxID",
    "OriginDest": "OriginDestID",
    "Order": "OrderID",
    "PaxJourney": "PaxJourneyID",
    "PriceClass": "PriceClassID",
    "BaggageAllowance": "BaggageAllowanceID",
}


@dataclass(frozen=True)
class NodeRecord:
    """One comparable node: an element with text, or an attribute."""

    struct: str  # tag path, sibling index removed — order-independent
    path: str  # tag path with sibling index — human-readable location
    kind: str  # "element" | "attribute" | "text"
    value: str

    @property
    def signature(self) -> tuple[str, str]:
        return (self.struct, self.value)


def _indexed_children(element: etree._Element) -> list[tuple[str, int, etree._Element]]:
    """Children of ``element`` paired with their 1-based index among same-tag peers."""
    counts: Counter[str] = Counter()
    out: list[tuple[str, int, etree._Element]] = []
    for child in element:
        if not isinstance(child.tag, str):  # skip comments / processing instructions
            continue
        tag = localname(child.tag)
        counts[tag] += 1
        out.append((tag, counts[tag], child))
    return out


def flatten(root: etree._Element) -> list[NodeRecord]:
    """Flatten a document into comparable node records.

    Element text and attributes each become their own record, so a changed price
    surfaces as a changed value rather than a changed node.
    """
    records: list[NodeRecord] = []

    def walk(element: etree._Element, struct: str, path: str) -> None:
        for tag, idx, child in _indexed_children(element):
            child_struct = f"{struct}/{tag}" if struct else tag
            child_path = f"{path}/{tag}[{idx}]" if path else f"{tag}[{idx}]"

            for attr_name, attr_value in sorted(child.attrib.items()):
                records.append(
                    NodeRecord(
                        struct=f"{child_struct}/@{localname(attr_name)}",
                        path=f"{child_path}/@{localname(attr_name)}",
                        kind="attribute",
                        value=attr_value.strip(),
                    )
                )

            text = (child.text or "").strip()
            has_children = len(_indexed_children(child)) > 0
            if text and not has_children:
                records.append(
                    NodeRecord(
                        struct=child_struct,
                        path=child_path,
                        kind="text",
                        value=text,
                    )
                )

            walk(child, child_struct, child_path)

    tag = localname(root.tag)
    root_text = (root.text or "").strip()
    if root_text and not _indexed_children(root):
        records.append(
            NodeRecord(struct=tag, path=f"{tag}[1]", kind="text", value=root_text)
        )
    for attr_name, attr_value in sorted(root.attrib.items()):
        records.append(
            NodeRecord(
                struct=f"{tag}/@{localname(attr_name)}",
                path=f"{tag}[1]/@{localname(attr_name)}",
                kind="attribute",
                value=attr_value.strip(),
            )
        )
    walk(root, tag, f"{tag}[1]")
    return records


@dataclass
class ValueDiff:
    """A node path whose value set changed."""

    path: str
    struct: str
    missing_values: list[str] = field(default_factory=list)
    extra_values: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "struct": self.struct,
            "missing_values": self.missing_values,
            "extra_values": self.extra_values,
        }


@dataclass
class EntityDiff:
    """A diff scoped to one ID-bearing entity (an Offer, a Segment, ...)."""

    entity: str
    key: str
    change: str  # "removed" | "added" | "modified"
    missing: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity": self.entity,
            "key": self.key,
            "change": self.change,
            "missing": self.missing,
            "extra": self.extra,
        }


@dataclass
class DiffReport:
    """Full comparison of a baseline and an updated NDC message."""

    base_label: str
    new_label: str
    counts_missing: dict[str, int] = field(default_factory=dict)
    counts_extra: dict[str, int] = field(default_factory=dict)
    value_diffs: list[ValueDiff] = field(default_factory=list)
    entities_removed: list[EntityDiff] = field(default_factory=list)
    entities_added: list[EntityDiff] = field(default_factory=list)
    entities_modified: list[EntityDiff] = field(default_factory=list)

    @property
    def identical(self) -> bool:
        return not (
            self.counts_missing
            or self.counts_extra
            or self.value_diffs
            or self.entities_removed
            or self.entities_added
            or self.entities_modified
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "base": self.base_label,
            "new": self.new_label,
            "identical": self.identical,
            "missing_nodes": self.counts_missing,
            "extra_nodes": self.counts_extra,
            "value_diffs": [v.to_dict() for v in self.value_diffs],
            "entities_removed": [e.to_dict() for e in self.entities_removed],
            "entities_added": [e.to_dict() for e in self.entities_added],
            "entities_modified": [e.to_dict() for e in self.entities_modified],
        }

    def render(self, max_items: int = 40) -> str:
        """Human/LLM-readable summary of the differences."""
        if self.identical:
            return (
                f"No differences: {self.new_label} is structurally identical to "
                f"{self.base_label}."
            )

        lines = [f"Comparing {self.base_label} (baseline) vs {self.new_label} (new):"]

        if self.entities_removed:
            lines.append("\nMISSING entities (in baseline, absent from new):")
            for e in self.entities_removed[:max_items]:
                lines.append(f"  - {e.entity} {e.key} was removed")

        if self.entities_added:
            lines.append("\nEXTRA entities (new, not in baseline):")
            for e in self.entities_added[:max_items]:
                lines.append(f"  - {e.entity} {e.key} was added")

        if self.entities_modified:
            lines.append("\nMODIFIED entities (same ID, different content):")
            for e in self.entities_modified[:max_items]:
                lines.append(f"  - {e.entity} {e.key}:")
                for m in e.missing[:max_items]:
                    lines.append(f"      missing: {m}")
                for x in e.extra[:max_items]:
                    lines.append(f"      extra:   {x}")

        if self.counts_missing:
            lines.append("\nMISSING node paths (tag -> count):")
            for path, count in sorted(self.counts_missing.items())[:max_items]:
                lines.append(f"  - {path} x{count}")

        if self.counts_extra:
            lines.append("\nEXTRA node paths (tag -> count):")
            for path, count in sorted(self.counts_extra.items())[:max_items]:
                lines.append(f"  + {path} x{count}")

        if self.value_diffs:
            lines.append("\nVALUE changes (same path, different values):")
            for v in self.value_diffs[:max_items]:
                for mv in v.missing_values[:10]:
                    lines.append(f"  - {v.path}: {mv!r} no longer present")
                for xv in v.extra_values[:10]:
                    lines.append(f"  + {v.path}: {xv!r} newly present")

        return "\n".join(lines)


def _parse(xml: str | bytes) -> etree._Element:
    if isinstance(xml, str):
        xml = xml.encode("utf-8")
    return etree.fromstring(xml)


def _flatten_entity(entity_el: etree._Element) -> list[NodeRecord]:
    """Flatten one entity's own fields, not descending into nested entities.

    Nested ID-bearing elements (an Offer's OfferItems, say) are compared under
    their own keys, so recreating them here would report the same change twice.
    """
    tag = localname(entity_el.tag)
    records: list[NodeRecord] = []

    def walk(element: etree._Element, struct: str, path: str) -> None:
        for child_tag, idx, child in _indexed_children(element):
            child_struct = f"{struct}/{child_tag}"
            child_path = f"{path}/{child_tag}[{idx}]"

            # Do not recreate nested entities; they are diffed on their own key.
            if child_tag in ENTITY_KEYS and child_struct != f"{tag}/{child_tag}":
                continue

            for attr_name, attr_value in sorted(child.attrib.items()):
                name = localname(attr_name)
                records.append(
                    NodeRecord(
                        struct=f"{child_struct}/@{name}",
                        path=f"{child_path}/@{name}",
                        kind="attribute",
                        value=attr_value.strip(),
                    )
                )
            text = (child.text or "").strip()
            if text and not _indexed_children(child):
                records.append(
                    NodeRecord(
                        struct=child_struct, path=child_path, kind="text", value=text
                    )
                )
            walk(child, child_struct, child_path)

    walk(entity_el, tag, f"{tag}[1]")
    return records


def _entities(root: etree._Element) -> dict[tuple[str, str], list[NodeRecord]]:
    """Collect ID-bearing entities keyed by (element name, id value)."""
    entities: dict[tuple[str, str], list[NodeRecord]] = {}
    for element in root.iter():
        if not isinstance(element.tag, str):
            continue
        tag = localname(element.tag)
        id_child = ENTITY_KEYS.get(tag)
        if id_child is None:
            continue
        key: str | None = None
        for child in element:
            if isinstance(child.tag, str) and localname(child.tag) == id_child:
                key = (child.text or "").strip() or None
                break
        if not key:
            continue
        # Keep every record describing the entity except the ID itself, so two
        # instances with the same ID compare field-by-field without the ID
        # showing up as a difference.
        id_struct = f"{tag}/{id_child}"
        scoped = [
            record
            for record in _flatten_entity(element)
            if not (record.struct == id_struct and record.value == key)
        ]
        entities.setdefault((tag, key), []).extend(scoped)
    return entities


def _entity_value_map(records: list[NodeRecord]) -> Counter[tuple[str, str]]:
    return Counter(r.signature for r in records)


def diff_xml(
    base_xml: str | bytes,
    new_xml: str | bytes,
    base_label: str = "baseline",
    new_label: str = "new",
) -> DiffReport:
    """Compute the structural and entity-level diff between two NDC messages."""
    base_root = _parse(base_xml)
    new_root = _parse(new_xml)

    report = DiffReport(base_label=base_label, new_label=new_label)

    # --- structural pass ---------------------------------------------------- #
    base_records = flatten(base_root)
    new_records = flatten(new_root)

    base_struct = Counter(r.struct for r in base_records)
    new_struct = Counter(r.struct for r in new_records)
    report.counts_missing = {
        path: base_struct[path] - new_struct.get(path, 0)
        for path in base_struct
        if base_struct[path] > new_struct.get(path, 0)
    }
    report.counts_extra = {
        path: new_struct[path] - base_struct.get(path, 0)
        for path in new_struct
        if new_struct[path] > base_struct.get(path, 0)
    }

    # Value-level differences at a path that exists in both documents.
    structures = sorted(set(base_struct) | set(new_struct))
    for struct in structures:
        if struct not in base_struct or struct not in new_struct:
            continue
        base_vals = Counter(r.value for r in base_records if r.struct == struct)
        new_vals = Counter(r.value for r in new_records if r.struct == struct)
        missing_vals = sorted((base_vals - new_vals).elements())
        extra_vals = sorted((new_vals - base_vals).elements())
        if missing_vals or extra_vals:
            sample_path = next(
                (r.path for r in base_records if r.struct == struct),
                struct,
            )
            report.value_diffs.append(
                ValueDiff(
                    path=sample_path,
                    struct=struct,
                    missing_values=missing_vals,
                    extra_values=extra_vals,
                )
            )

    # --- entity pass -------------------------------------------------------- #
    base_entities = _entities(base_root)
    new_entities = _entities(new_root)

    # Entity tags whose instance set changed. Value diffs nested inside these are
    # already reported per-entity, so suppressing them here keeps the report free
    # of duplicate noise when an offer is added or dropped.
    churned_entity_tags: set[str] = set()

    for (entity, key) in sorted(set(base_entities) | set(new_entities)):
        in_base = (entity, key) in base_entities
        in_new = (entity, key) in new_entities
        if in_base and not in_new:
            report.entities_removed.append(
                EntityDiff(entity=entity, key=key, change="removed")
            )
            churned_entity_tags.add(entity)
        elif in_new and not in_base:
            report.entities_added.append(
                EntityDiff(entity=entity, key=key, change="added")
            )
            churned_entity_tags.add(entity)
        else:
            base_map = _entity_value_map(base_entities[(entity, key)])
            new_map = _entity_value_map(new_entities[(entity, key)])
            missing = [
                f"{struct} = {value!r}"
                for (struct, value), n in (base_map - new_map).items()
                for _ in range(n)
            ]
            extra = [
                f"{struct} = {value!r}"
                for (struct, value), n in (new_map - base_map).items()
                for _ in range(n)
            ]
            if missing or extra:
                report.entities_modified.append(
                    EntityDiff(
                        entity=entity,
                        key=key,
                        change="modified",
                        missing=sorted(missing),
                        extra=sorted(extra),
                    )
                )

    if churned_entity_tags:
        report.value_diffs = [
            v
            for v in report.value_diffs
            if not any(f"/{tag}/" in f"{v.struct}/" for tag in churned_entity_tags)
        ]

    return report


def summarize_message(xml: str | bytes, label: str = "message") -> dict[str, Any]:
    """Inventory of an NDC message: root, offer/segment counts and IDs."""
    root = _parse(xml)
    entities: dict[str, list[str]] = {}
    for element in root.iter():
        if not isinstance(element.tag, str):
            continue
        tag = localname(element.tag)
        id_child = ENTITY_KEYS.get(tag)
        if id_child is None:
            continue
        for child in element:
            if isinstance(child.tag, str) and localname(child.tag) == id_child:
                value = (child.text or "").strip()
                if value:
                    entities.setdefault(tag, []).append(value)

    return {
        "label": label,
        "root": localname(root.tag),
        "version": root.get("Version"),
        "node_count": sum(1 for _ in root.iter()),
        "entity_ids": entities,
        "entity_counts": {k: len(v) for k, v in entities.items()},
    }
