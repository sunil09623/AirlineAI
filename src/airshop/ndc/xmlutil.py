"""Namespace-agnostic helpers for walking NDC XML documents."""

from __future__ import annotations

import re

from lxml import etree

_NS_RE = re.compile(r"\{[^}]+\}")


def localname(tag: str) -> str:
    """Strip the ``{namespace}`` prefix from an lxml tag."""
    return _NS_RE.sub("", tag)


def find(element: etree._Element, name: str) -> etree._Element | None:
    """First descendant (excluding ``element``) whose local name matches."""
    for child in element.iter():
        if child is not element and localname(child.tag) == name:
            return child
    return None


def find_all(element: etree._Element, name: str) -> list[etree._Element]:
    """All descendants (excluding ``element``) whose local name matches."""
    return [
        c for c in element.iter() if c is not element and localname(c.tag) == name
    ]


def first(*elements: etree._Element | None) -> etree._Element | None:
    """First non-None element; lxml elements must not be truth-tested."""
    for element in elements:
        if element is not None:
            return element
    return None


def text(element: etree._Element | None) -> str | None:
    """Stripped element text, or None when absent/empty."""
    if element is None or element.text is None:
        return None
    value = element.text.strip()
    return value or None
