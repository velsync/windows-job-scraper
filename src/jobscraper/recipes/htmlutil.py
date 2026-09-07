"""Minimal DOM + selector utilities built on stdlib html.parser.

Supports the locator kinds the ExtractionRecipe model declares: data
attributes, simple CSS (tag/#id/.class/[attr]/[attr*=v], descendant +
child combinators), relative CSS (scoped to a card element), role/ARIA,
href patterns and text anchors. Deliberately NOT a full CSS engine; absolute
DOM paths are never the sole primary locator by design.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser

VOID_ELEMENTS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}

_BLOCK_TAGS = {"style", "noscript", "template"}
# NOTE: <script> content is RETAINED as element text — JSON-LD extraction
# (module 02 section 22) reads <script type="application/ld+json"> bodies.
# Consumers that must not see script text filter by tag.


@dataclass
class Element:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    parent: "Element | None" = None
    children: list = field(default_factory=list)
    text_parts: list[str] = field(default_factory=list)

    def add_child(self, child: "Element") -> None:
        self.children.append(child)
        child.parent = self

    def text(self, *, recursive: bool = True) -> str:
        out = " ".join(p for p in self.text_parts if p.strip())
        if recursive:
            for c in self.children:
                t = c.text(recursive=True)
                if t:
                    out += " " + t
        return re.sub(r"\s+", " ", unescape(out)).strip()

    @property
    def classes(self) -> list[str]:
        return (self.attrs.get("class") or "").split()

    def attr(self, name: str) -> str | None:
        return self.attrs.get(name)

    def iter(self):
        yield self
        for c in self.children:
            yield from c.iter()


class _DomBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Element("#root")
        self.stack: list[Element] = [self.root]
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if self._skip and tag not in _BLOCK_TAGS:
            return
        if tag in _BLOCK_TAGS:
            self._skip += 1
        element = Element(tag, {k: (v if v is not None else "") for k, v in attrs})
        self.stack[-1].add_child(element)
        if tag not in VOID_ELEMENTS:
            self.stack.append(element)

    def handle_startendtag(self, tag, attrs):
        element = Element(tag, {k: (v if v is not None else "") for k, v in attrs})
        self.stack[-1].add_child(element)

    def handle_endtag(self, tag):
        if tag in _BLOCK_TAGS and self._skip:
            self._skip -= 1
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        if not self._skip:
            self.stack[-1].text_parts.append(data)


def parse_html(html: str) -> Element:
    builder = _DomBuilder()
    builder.feed(html)
    builder.close()
    return builder.root


def find_all(root: Element, tag: str) -> list[Element]:
    return [e for e in root.iter() if e.tag == tag]


# ---------------------------------------------------------------- selectors
_ATTR_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9-]*)(#[-\w]+)?((?:\.[-\w]+)*)"
                      r"((?:\[[^\]]+\])*)$")
_COND_RE = re.compile(r"^\[([a-zA-Z-]+)(?:([*^$~]|)=(\"[^\"]*\"|'[^']*'|[^\]]*))?\]$")


def _match_single(element: Element, selector: str) -> bool:
    selector = selector.strip()
    if selector == "*":
        return True
    m = _ATTR_RE.match(selector)
    if not m:
        return False
    tag, id_part, class_part, attrs_part = m.groups()
    if tag and tag != "*" and element.tag != tag.lower():
        return False
    if id_part and element.attrs.get("id") != id_part[1:]:
        return False
    if class_part:
        wanted = class_part[1:].split(".")
        have = set(element.classes)
        if not all(w in have for w in wanted):
            return False
    if attrs_part:
        rest = attrs_part
        while rest:
            cm = re.match(r"^(\[[^\]]+\])", rest)
            if not cm:
                return False
            cond = _COND_RE.match(cm.group(1))
            if not cond:
                return False
            name, op, value = cond.groups()
            if value is not None:
                value = value.strip("'\"")
            actual = element.attrs.get(name)
            if op is None:
                if actual is None:
                    return False
            elif op == "=" and actual != value:
                return False
            elif op == "*" and (actual is None or value not in actual):
                return False
            elif op == "^" and (actual is None or not actual.startswith(value)):
                return False
            elif op == "$" and (actual is None or not actual.endswith(value)):
                return False
            elif op == "~" and (actual is None or value not in actual.split()):
                return False
            rest = rest[cm.end():]
    return True


def css_select(root: Element, selector: str) -> list[Element]:
    """Select elements by a simple CSS subset (descendant + child '>' + ',')."""
    results: list[Element] = []
    for group in selector.split(","):
        group = group.strip()
        if not group:
            continue
        # Split on '>' child combinator vs whitespace descendant.
        parts = re.split(r"\s*>\s*", group) if ">" in group else group.split()
        if not parts:
            continue
        current: list[Element] = [root]
        for i, part in enumerate(parts):
            child_mode = ">" in group and i > 0
            nxt: list[Element] = []
            for el in current:
                if child_mode:
                    nxt.extend(c for c in el.children if _match_single(c, part))
                else:
                    nxt.extend(e for e in el.iter() if e is not el and _match_single(e, part))
            current = nxt
            if not current:
                break
        results.extend(e for e in current if e is not root)
    # Preserve document order, dedupe.
    seen = set()
    ordered = []
    for e in root.iter():
        if id(e) in {id(x) for x in results} and id(e) not in seen:
            seen.add(id(e))
            ordered.append(e)
    return ordered


def select_by_data_attr(root: Element, expr: str) -> list[Element]:
    """data-testid=job-card style locator."""
    if "=" in expr:
        name, _, value = expr.partition("=")
        return [
            e for e in root.iter()
            if e.attrs.get(name.strip()) == value.strip()
        ]
    return [e for e in root.iter() if expr.strip() in e.attrs]


def select_by_role(root: Element, role: str) -> list[Element]:
    role = role.strip().lower()
    return [
        e for e in root.iter()
        if (e.attrs.get("role") or "").lower() == role
        or (role == "heading" and e.tag in {"h1", "h2", "h3", "h4", "h5", "h6"})
        or (role == "link" and e.tag == "a")
        or (role == "listitem" and e.tag == "li")
        or (role == "button" and e.tag == "button")
    ]


def select_by_href_pattern(root: Element, pattern: str) -> list[Element]:
    return [e for e in root.iter() if e.tag == "a" and pattern in (e.attrs.get("href") or "")]


def select_by_text_anchor(root: Element, text: str) -> list[Element]:
    text = text.strip().lower()
    return [e for e in root.iter() if text in e.text(recursive=False).lower()]


def absolute_url(base: str, href: str | None) -> str | None:
    if not href:
        return None
    from urllib.parse import urljoin, urlsplit

    if urlsplit(href).scheme:
        return href
    return urljoin(base, href)
