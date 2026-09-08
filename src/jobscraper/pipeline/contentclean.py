"""Deterministic content cleaning (01 §34).

Pipeline:

    raw/structured description
    → deterministic cleaning → Markdown → plain text → language → hashes

This module is the single owner of the deterministic cleaner that turns a
raw job description (HTML, Markdown or plain text) into the Markdown and
plain-text representations recorded on the canonical row.  ``clean`` is
deterministic and idempotent for stable stored representations: re-processing
an observation never invents content or executable links.

Hardening rules (all versioned behind ``CONTENT_CLEANING_VERSION``):

* script/style blocks and their content are removed;
* HTML entities are decoded exactly once;
* structural Markdown is retained (headings, lists, emphasis, links);
* a link is retained only when its scheme is one of the non-executable
  ``http``/``https``/``mailto`` schemes; ``javascript:``, ``data:``,
  ``vbscript:`` and every other scheme (including schemeless and relative
  targets) is neutralized to the link's visible text — the URL is never
  stored;
* tracking parameters are dropped from retained links only;
* ordinary plain text passes through without Markdown interpretation unless it
  contains explicit Markdown structure.

The canonical row records which cleaning revision produced its description
(``jobs.content_cleaning_version``, migration v13) so a later cleaner revision
cannot silently rewrite provenance (RUN-21).
"""

from __future__ import annotations

import hashlib
import html as html_lib
import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

CONTENT_CLEANING_VERSION = "content-clean-v1"

TRACKING_PARAM_PREFIXES = ("utm_",)
TRACKING_PARAM_NAMES = frozenset(
    {
        "fbclid",
        "gclid",
        "msclkid",
        "twclid",
        "gbraid",
        "wbraid",
        "dclid",
        "yclid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "vero_id",
        "wickedid",
    }
)

SAFE_LINK_SCHEMES = frozenset({"http", "https", "mailto"})

_BLOCK_TAGS = re.compile(
    r"</?(p|div|section|article|header|footer|ul|ol|li|h[1-6]|br|tr|td|table)[^>]*>",
    re.IGNORECASE,
)
_SCRIPT_STYLE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_HTML_TAG = re.compile(r"<[^>]+>")
_HTML_LIKE = re.compile(r"</?[a-zA-Z][^>]*>")
_MD_LINK = re.compile(r"!?\[([^\]]*)\]\(((?:[^()]|\([^)]*\))*)\)")
_MD_HEADING = re.compile(r"^#{1,6}\s*", re.MULTILINE)
_MD_FENCE = re.compile(r"^```.*$", re.MULTILINE)
_MD_BULLET = re.compile(r"^\s*[-*+]\s+", re.MULTILINE)
_MD_NUMBERED = re.compile(r"^\s*\d+[.)]\s+", re.MULTILINE)
_MD_BLOCK_SIGNAL = re.compile(
    r"^(?:\s{0,3}#{1,6}\s+|\s*[-*+]\s+|\s*\d+[.)]\s+|\s*>\s?|\s*```)",
    re.MULTILINE,
)
_MD_INLINE_SIGNAL = re.compile(
    r"(?:\*\*[^*\n]+\*\*|__[^_\n]+__|`[^`\n]+`|(?<!\*)\*[^*\n]+\*(?!\*))"
)
_WHITESPACE = re.compile(r"[ \t]+")
_NEWLINES = re.compile(r"\n{3,}")
_SPACE_BEFORE_PUNCT = re.compile(r"[ \t]+([,.;:!?)\]])")
_SCHEME = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.\-]*):")


def _collapse(text: str) -> str:
    text = _WHITESPACE.sub(" ", text)
    lines = [line.strip() for line in text.split("\n")]
    return _NEWLINES.sub("\n\n", "\n".join(lines)).strip()


def _derived_text(raw_text: str) -> str:
    return _SPACE_BEFORE_PUNCT.sub(r"\1", _collapse(raw_text))


def _drop_tracking_params(url: str) -> str:
    parts = urlsplit(url)
    if not parts.query:
        return url
    kept = [
        (name, value)
        for name, value in parse_qsl(parts.query, keep_blank_values=True)
        if name not in TRACKING_PARAM_NAMES
        and not any(name.startswith(prefix) for prefix in TRACKING_PARAM_PREFIXES)
    ]
    query = urlencode(kept) if kept else ""
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def _neutralize_url(url: str) -> str | None:
    candidate = (url or "").strip()
    if not candidate:
        return None
    match = _SCHEME.match(candidate)
    if not match:
        return None
    scheme = match.group(1).lower()
    if scheme not in SAFE_LINK_SCHEMES:
        return None
    if scheme in ("http", "https"):
        candidate = _drop_tracking_params(candidate)
        if not candidate:
            return None
    return candidate


def _safe_link(url: str, label: str) -> str:
    """One HTML anchor → safe Markdown link or visible label only."""
    url = html_lib.unescape(url or "").strip()
    label = _collapse(_HTML_TAG.sub(" ", label or ""))
    retained = _neutralize_url(url)
    if retained is None:
        return label
    return f"[{label}]({retained})"


def _html_to_markdown(markup: str) -> str:
    text = _HTML_COMMENT.sub(" ", markup or "")
    text = _SCRIPT_STYLE.sub(" ", text)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<li[^>]*>", "- ", text, flags=re.IGNORECASE)
    text = re.sub(r"</li>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(
        r"<h([1-6])[^>]*>",
        lambda m: "#" * int(m.group(1)) + " ",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"</h[1-6]>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(
        r"</(div|section|article|header|footer|ul|ol|table|tr)>",
        "\n",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"<(p|div|section|article|header|footer|ul|ol|table|tr)\b[^>]*>",
        "\n",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"<(strong|b)>(.*?)</\1>",
        r"**\2**",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    text = re.sub(
        r"<(em|i)>(.*?)</\1>",
        r"*\2*",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    text = re.sub(
        r"<a\b[^>]*href\s*=\s*[\"']([^\"']*)[\"'][^>]*>(.*?)</a>",
        lambda m: _safe_link(m.group(1), m.group(2)),
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    text = _HTML_TAG.sub(" ", text)
    return _collapse(html_lib.unescape(text))


def _html_to_text(markup: str) -> str:
    text = _HTML_COMMENT.sub(" ", markup or "")
    text = _SCRIPT_STYLE.sub(" ", text)
    text = _BLOCK_TAGS.sub("\n", text)
    text = _HTML_TAG.sub(" ", text)
    return _derived_text(html_lib.unescape(text))


def _clean_markdown(raw: str) -> str:
    """Preserve Markdown structure while sanitizing every retained link."""
    text = raw or ""

    def _md_link(match: re.Match) -> str:
        label, url = match.group(1), match.group(2)
        url = html_lib.unescape(url)
        safe = _neutralize_url(url)
        if safe is None:
            return label
        return f"[{label}]({safe})"

    text = _MD_LINK.sub(_md_link, text)
    return _collapse(html_lib.unescape(text))


def _markdown_to_text(markdown: str) -> str:
    """Markdown → plain text (labels only, no Markdown syntax residue)."""
    text = markdown or ""
    text = _MD_FENCE.sub("", text)
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = _MD_HEADING.sub("", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"__([^_]+)__", r"\1", text)
    text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"\1", text)
    text = re.sub(r"(?<!_)_([^_\n]+)_(?!_)", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = _MD_BULLET.sub("", text)
    text = _MD_NUMBERED.sub("", text)
    text = re.sub(r"^>\s?", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*([-*_])\s*(\1\s*){2,}$", "", text, flags=re.MULTILINE)
    return _derived_text(text)


def _looks_like_html(raw: str) -> bool:
    return bool(_HTML_LIKE.search(raw or ""))


def _looks_like_markdown(raw: str) -> bool:
    """Recognize explicit Markdown structure, not only Markdown links."""
    text = raw or ""
    return bool(
        _MD_LINK.search(text)
        or _MD_BLOCK_SIGNAL.search(text)
        or _MD_INLINE_SIGNAL.search(text)
    )


_EN_STOP = frozenset(
    "the and for with you your our are have this that will from not per job work".split()
)
_DE_STOP = frozenset(
    "der die das und für mit Sie Ihre sind werden nicht aus dem den job".split()
)
_RO_STOP = frozenset(
    "și de la în pentru care vor fi acest nostru dumneavoastră sunt".split()
)


def detect_language(text: str) -> str | None:
    words = re.findall(r"[a-zăâîșțäöüß]+", (text or "").lower())
    if len(words) < 12:
        return None
    scores = {
        "en": sum(1 for w in words if w in _EN_STOP),
        "de": sum(1 for w in words if w in _DE_STOP),
        "ro": sum(1 for w in words if w in _RO_STOP),
    }
    best = max(scores, key=lambda k: scores[k])
    if scores[best] < 3:
        return None
    return best


@dataclass(frozen=True)
class CleanedContent:
    markdown: str | None
    text: str | None
    lang: str | None
    content_hash: str | None


def clean(raw: str) -> CleanedContent:
    """Deterministically clean one raw description (01 §34)."""
    if raw is None:
        raw = ""
    raw = str(raw)
    if not raw.strip():
        return CleanedContent(markdown=None, text=None, lang=None, content_hash=None)

    if _looks_like_html(raw):
        markdown = _html_to_markdown(raw)
        text = _html_to_text(raw)
    elif _looks_like_markdown(raw):
        markdown = _clean_markdown(raw)
        text = _markdown_to_text(markdown)
    else:
        text = _collapse(raw)
        markdown = text

    content_hash = hashlib.sha256(text.encode()).hexdigest() if text else None
    return CleanedContent(
        markdown=markdown,
        text=text,
        lang=detect_language(text),
        content_hash=content_hash,
    )


__all__ = [
    "CONTENT_CLEANING_VERSION",
    "CleanedContent",
    "SAFE_LINK_SCHEMES",
    "TRACKING_PARAM_NAMES",
    "TRACKING_PARAM_PREFIXES",
    "clean",
    "detect_language",
]
