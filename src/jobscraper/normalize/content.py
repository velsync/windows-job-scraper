"""Content normalization: HTML -> markdown/text, language, hashes.

Authority: module 01 section 34 (content normalization pipeline).
"""

from __future__ import annotations

import hashlib
import re
from html import unescape

_BLOCK_TAGS = {"script", "style", "noscript", "template", "head"}
_HEADING_TAGS = {"h1": "#", "h2": "##", "h3": "###", "h4": "####", "h5": "#####", "h6": "######"}


def html_to_text(html: str) -> str:
    """Deterministic visible-text extraction."""
    html = re.sub(r"<!--.*?-->", " ", html, flags=re.S)
    for tag in _BLOCK_TAGS:
        html = re.sub(rf"<{tag}\b.*?</{tag}>", " ", html, flags=re.S | re.I)
        html = re.sub(rf"<{tag}\b[^>]*/?>", " ", html, flags=re.I)
    html = re.sub(r"</(p|div|li|tr|h[1-6]|br)>", "\n", html, flags=re.I)
    html = re.sub(r"<br\s*/?>", "\n", html, flags=re.I)
    html = re.sub(r"<li\b[^>]*>", "\n- ", html, flags=re.I)
    html = re.sub(r"<[^>]+>", " ", html)
    text = unescape(html)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def html_to_markdown(html: str) -> str:
    """Markdown-lite conversion (display-only; rendered through escaping)."""
    html = re.sub(r"<!--.*?-->", " ", html, flags=re.S)
    for tag in _BLOCK_TAGS:
        html = re.sub(rf"<{tag}\b.*?</{tag}>", " ", html, flags=re.S | re.I)

    def heading(match: re.Match) -> str:
        tag = match.group(1).lower()
        marker = _HEADING_TAGS.get(tag, "#")
        return f"\n{marker} {match.group(2).strip()}\n"

    html = re.sub(r"<(h[1-6])[^>]*>(.*?)</\1>", heading, html, flags=re.S | re.I)
    html = re.sub(r"<(strong|b)[^>]*>(.*?)</\1>", r"**\2**", html, flags=re.S | re.I)
    html = re.sub(r"<(em|i)[^>]*>(.*?)</\1>", r"*\2*", html, flags=re.S | re.I)
    html = re.sub(r"<a [^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", r"[\2](\1)", html, flags=re.S | re.I)
    html = re.sub(r"<li\b[^>]*>", "\n- ", html, flags=re.I)
    html = re.sub(r"</(p|div|ul|ol|section|article)>", "\n", html, flags=re.I)
    html = re.sub(r"<br\s*/?>", "\n", html, flags=re.I)
    html = re.sub(r"<[^>]+>", " ", html)
    text = unescape(html)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_KANA_RE = re.compile(r"[\u3040-\u30ff]")
_HANGUL_RE = re.compile(r"[\uac00-\ud7af]")
_LATIN_RE = re.compile(r"[A-Za-z]")


def detect_language(text: str) -> tuple[str, float]:
    """Cheap deterministic language heuristic with confidence."""
    sample = text[:5000]
    if not sample.strip():
        return ("unknown", 0.0)
    if _KANA_RE.search(sample):
        return ("ja", 0.7)
    if _HANGUL_RE.search(sample):
        return ("ko", 0.7)
    cjk = len(_CJK_RE.findall(sample))
    if cjk:
        return ("zh", 0.6)
    if _LATIN_RE.search(sample):
        return ("en", 0.5)
    return ("unknown", 0.1)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def content_hash(title: str, company: str, description: str) -> str:
    return sha256_text(f"{title}\x1f{company}\x1f{html_to_text(description)[:10000]}")
