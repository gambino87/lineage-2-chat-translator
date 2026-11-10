"""In-memory link provenance and display styling; never infer links from brackets."""
from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
import re


LINK_COLORS = {"item_link": "#d3d3d3", "party_link": "#add8e6"}
# Shared with translation invariants: only ranges/counts from actual party links
# are protected; plain bracketed prose never gains link provenance.
PARTY_DETAIL = re.compile(r"\[[ \t]*[0-9]+[ \t]*[~\-–—][ \t]*[0-9]+[ \t]*\]|\([ \t]*[0-9]+[ \t]*/[ \t]*[0-9]+[ \t]*\)")


@dataclass(frozen=True)
class ChatLinkSpan:
    kind: str
    start: int
    end: int


def valid_spans(text: str, spans) -> tuple[ChatLinkSpan, ...]:
    """Treat invalid or overlapping provenance as plain text, never a render error."""
    if not isinstance(spans, (tuple, list)):
        return ()
    result = []
    end = 0
    for span in spans:
        if (not isinstance(span, ChatLinkSpan) or span.kind not in ("item", "party")
                or type(span.start) is not int or type(span.end) is not int
                or not end <= span.start < span.end <= len(text)):
            return ()
        result.append(span)
        end = span.end
    return tuple(result)


class LinkedText(str):
    """A string-compatible translation carrying exact, optional display spans."""
    def __new__(cls, text, spans=()):
        value = super().__new__(cls, text)
        value.link_spans = valid_spans(value, spans)
        return value


def normalize_display(value, *, spans=None, limit=None) -> LinkedText:
    """Collapse whitespace and clip text while mapping its source offsets."""
    text = str(value)
    spans = valid_spans(text, getattr(value, "link_spans", ()) if spans is None else spans)
    parts, positions = [], []
    for match in re.finditer(r"\S+", text):
        if parts:
            parts.append(" ")
            positions.append(match.start() - 1)
        parts.append(match.group())
        positions.extend(range(match.start(), match.end()))
    shown = "".join(parts)
    if limit is not None:
        shown = shown[:max(0, limit)]
        positions = positions[:len(shown)]
    mapped = []
    for span in spans:
        start = bisect_left(positions, span.start)
        end = bisect_left(positions, span.end)
        # Whitespace at link boundaries must not color surrounding prose.
        while start < end and shown[start].isspace():
            start += 1
        while end > start and shown[end - 1].isspace():
            end -= 1
        if start < end:
            mapped.append(ChatLinkSpan(span.kind, start, end))
    return LinkedText(shown, mapped)


def insert_linked_text(widget, value, base_tag):
    """Insert text with the base style and foreground overrides on known links."""
    text = str(value)
    spans = valid_spans(text, getattr(value, "link_spans", ()))
    cursor = 0
    for span in spans:
        if cursor < span.start:
            widget.insert("end", text[cursor:span.start], base_tag)
        widget.insert("end", text[span.start:span.end], (base_tag, span.kind + "_link"))
        cursor = span.end
    if cursor < len(text) or not spans:
        widget.insert("end", text[cursor:], base_tag)


@dataclass(frozen=True)
class LinkBoundary:
    span: ChatLinkSpan
    text: str
    opening: str
    closing: str


def without_boundary_tokens(text: str, protected: dict[str, str]) -> str:
    """Display markers are zero-width for hints; item/URL tokens stay opaque."""
    for token, label in protected.items():
        if len(label) == 1 and 0xe000 <= ord(label) <= 0xf8ff:
            text = text.replace(token, "")
    return text


def mark_link_boundaries(text: str, spans) -> tuple[str, tuple[LinkBoundary, ...]]:
    """Use collision-free private characters, protected by the existing translator."""
    spans = valid_spans(text, spans)
    used = set(text)
    available = [chr(code) for code in range(0xe000, 0xf900) if chr(code) not in used]
    if len(spans) * 2 > len(available):
        return text, ()
    available = iter(available)
    boundaries = []
    parts = []
    cursor = 0
    for span in spans:
        opening, closing = next(available), next(available)
        body = text[span.start:span.end]
        boundaries.append(LinkBoundary(span, body, opening, closing))
        parts.extend((text[cursor:span.start], opening, body, closing))
        cursor = span.end
    parts.append(text[cursor:])
    return "".join(parts), tuple(boundaries)


def restore_link_boundaries(value: str, boundaries: tuple[LinkBoundary, ...]) -> LinkedText:
    """Remove all private markers; color only complete, non-overlapping pairs."""
    if not boundaries:
        return LinkedText(value)
    markers = {marker for boundary in boundaries for marker in (boundary.opening, boundary.closing)}
    plain = value.translate({ord(marker): None for marker in markers})
    if any(value.count(marker) != 1 for marker in markers):
        return LinkedText(plain)
    by_marker = {marker: (index, opening) for index, boundary in enumerate(boundaries)
                 for marker, opening in ((boundary.opening, True), (boundary.closing, False))}
    spans = []
    active = None
    length = 0
    for char in value:
        event = by_marker.get(char)
        if event is None:
            length += 1
            continue
        index, opening = event
        if opening:
            if active is not None:
                return LinkedText(plain)
            active = (index, length)
        else:
            if active is None or active[0] != index:
                return LinkedText(plain)
            start = active[1]
            boundary = boundaries[index]
            body = plain[start:length]
            if not body.strip() or (boundary.span.kind == "item" and body.strip() != boundary.text):
                return LinkedText(plain)
            if boundary.span.kind == "party" and PARTY_DETAIL.findall(body) != PARTY_DETAIL.findall(boundary.text):
                return LinkedText(plain)
            spans.append(ChatLinkSpan(boundary.span.kind, start, length))
            active = None
    return LinkedText(plain, spans) if active is None else LinkedText(plain)
