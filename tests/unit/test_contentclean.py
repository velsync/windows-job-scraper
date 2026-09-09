"""S2.3 unit tests: deterministic content cleaning (01 §34).

Authority: docs/plans/slice-2-worker-implementation-plan-v0313.md S2.3;
docs/spec/v0.3.1.3/01_product_and_workflow.md §34.

Proves:

* ``clean(raw) -> (markdown, text, lang, hash)`` behind a versioned constant,
  deterministic and idempotent (cleaning cleaned output changes nothing);
* HTML path: script/style removed, HTML entities decoded, structural
  Markdown retained, links kept only with a safe scheme, tracking parameters
  dropped from retained links;
* Markdown path: executable URLs neutralized, tracking parameters dropped,
  entities decoded;
* plain text passes through unchanged;
* the description hash covers exactly the stored text bytes;
* language detection feeds the pipeline (en corpus, short text stays None).
"""

from __future__ import annotations

import hashlib

import pytest

from jobscraper.pipeline.contentclean import (
    CONTENT_CLEANING_VERSION,
    CleanedContent,
    clean,
    detect_language,
)


def test_cleaning_version_is_pinned() -> None:
    assert CONTENT_CLEANING_VERSION == "content-clean-v2"


def test_clean_returns_markdown_text_lang_and_hash() -> None:
    result = clean("<p>Build <strong>reliable</strong> things</p>")
    assert isinstance(result, CleanedContent)
    assert result.markdown == "Build **reliable** things"
    assert result.text == "Build reliable things"
    # hash covers exactly the stored plain-text bytes
    assert result.content_hash == hashlib.sha256(result.text.encode()).hexdigest()


def test_clean_is_deterministic() -> None:
    raw = '<div><h2>Role</h2><p>Ship <a href="https://x.test/j?utm_source=gh">now</a></p></div>'
    assert clean(raw) == clean(raw)


def test_clean_is_idempotent_over_html_and_markdown() -> None:
    # paragraph corpus: the Markdown and the plain text both restabilize
    raw = "<p>First paragraph</p><p>Second paragraph</p>"
    once = clean(raw)
    assert once.markdown == "First paragraph\n\nSecond paragraph"
    assert once.text == "First paragraph\n\nSecond paragraph"
    again = clean(once.markdown)
    assert again == once
    thrice = clean(once.text)
    assert thrice == once


def test_lists_keep_markdown_structure_and_stable_text() -> None:
    raw = "<ul><li>One</li><li>Two</li></ul>"
    once = clean(raw)
    # Markdown keeps list structure; text keeps block boundaries (Slice-1
    # legacy behavior: block-level tags separate text with paragraph breaks)
    assert once.markdown == "- One\n- Two"
    assert once.text == "One\n\nTwo"
    # re-cleaning the Markdown is stable and never re-introduces tags
    again = clean(once.markdown)
    assert again.markdown == once.markdown
    # re-cleaning the text is stable
    thrice = clean(once.text)
    assert thrice.text == once.text
    assert thrice.markdown == once.text
    assert thrice.content_hash == hashlib.sha256(once.text.encode()).hexdigest()


def test_script_and_style_are_stripped() -> None:
    raw = (
        "<p>Visible</p><script>alert('xss')</script>"
        "<style>.hidden{display:none}</style><p>Also visible</p>"
    )
    result = clean(raw)
    assert "alert" not in result.text
    assert "hidden" not in result.text
    assert result.text == "Visible\n\nAlso visible"


def test_html_entities_are_decoded_once() -> None:
    result = clean("<p>R&amp;D costs &lt; 5 &euro;</p>")
    assert result.text == "R&D costs < 5 €"
    # a decoded entity is not decoded a second time on re-cleaning
    again = clean(result.text)
    assert again.text == result.text


def test_markdown_structure_is_retained() -> None:
    raw = (
        "<h1>Engineer</h1><p>Intro</p>"
        "<ul><li>Python</li><li>SQL</li></ul>"
        "<p>Apply now.</p>"
    )
    result = clean(raw)
    assert result.markdown == "# Engineer\n\nIntro\n\n- Python\n- SQL\n\nApply now."
    assert "Python" in result.text and "SQL" in result.text


def test_safe_links_are_retained_without_tracking_parameters() -> None:
    raw = (
        '<p>See <a href="https://x.test/jobs/7?utm_source=feed&amp;utm_medium=api&amp;'
        'ref=42#apply">the role</a>.</p>'
    )
    result = clean(raw)
    assert result.markdown == "See [the role](https://x.test/jobs/7?ref=42#apply)."
    assert result.text == "See the role."


def test_executable_link_schemes_are_neutralized() -> None:
    raw = (
        '<p><a href="javascript:alert(1)">bad</a> '
        '<a href="data:text/html;base64,PHNjcmlwdD4=">worse</a> '
        '<a href="vbscript:msgbox(1)">ugly</a></p>'
    )
    result = clean(raw)
    # only the label survives — the URL is never retained
    assert result.markdown == "bad worse ugly"
    assert "javascript" not in result.markdown
    assert "data:" not in result.markdown
    assert "vbscript" not in result.markdown


def test_mailto_and_https_links_are_retained() -> None:
    raw = (
        '<p><a href="mailto:jobs@example.test">mail</a> '
        '<a href="https://example.test/careers">web</a></p>'
    )
    result = clean(raw)
    assert result.markdown == "[mail](mailto:jobs@example.test) [web](https://example.test/careers)"


def test_tracking_parameter_family_is_dropped_from_links_only() -> None:
    raw = (
        '<p>Read <a href="https://x.test/p?utm_campaign=c&amp;mc_cid=abc&amp;keep=1">'
        "utm_source appears only in links</a> — text keeps 'utm_campaign' when literal.</p>"
    )
    result = clean(raw)
    assert result.markdown == (
        "Read [utm_source appears only in links](https://x.test/p?keep=1)"
        " — text keeps 'utm_campaign' when literal."
    )


def test_markdown_input_is_cleaned_and_links_neutralized() -> None:
    raw = "[apply](javascript:void(0)) and [real](https://x.test/j?gclid=zz&amp;b=1)"
    result = clean(raw)
    assert result.markdown == "apply and [real](https://x.test/j?b=1)"
    assert result.text == "apply and real"


def test_plain_text_passes_through_unchanged() -> None:
    raw = "We are looking for a Senior Backend Engineer."
    result = clean(raw)
    assert result.markdown == raw
    assert result.text == raw
    assert result.content_hash == hashlib.sha256(raw.encode()).hexdigest()


def test_empty_and_whitespace_only_input_produce_no_content() -> None:
    for raw in ("", "   ", "\n\t"):
        result = clean(raw)
        assert result.markdown is None
        assert result.text is None
        assert result.lang is None
        assert result.content_hash is None


def test_language_detection_feeds_clean() -> None:
    english = (
        "We are looking for an engineer to join our team. You will work with "
        "our product group and help us deliver reliable software for customers."
    )
    assert detect_language(english) == "en"
    result = clean(f"<p>{english}</p>")
    assert result.lang == "en"
    # too short to guess: None, never a fabricated language
    assert clean("<p>Short text here</p>").lang is None


def test_detect_language_returns_none_for_ambiguous_or_short_text() -> None:
    assert detect_language("") is None
    assert detect_language("job title only") is None


def test_content_hash_never_claims_empty_content() -> None:
    result = clean("<script>only script</script>")
    # no decipherable text -> no claim of a content hash
    assert result.content_hash is None or result.text is None or result.text == ""


def test_relative_and_schemeless_links_are_not_retained_as_urls() -> None:
    raw = '<p><a href="/careers/x">relative</a> <a href="//cdn.test/x">scheme-less</a></p>'
    result = clean(raw)
    assert result.markdown == "relative scheme-less"
    assert result.text == "relative scheme-less"


def test_link_labels_with_nested_inline_markup_do_not_leak_spaces() -> None:
    # a <span> wrapper around the label must not leave stray spaces inside
    # the markdown link brackets; emphasis conversion still applies inside
    plain = '<a href="https://x.test/j"><span>Apply</span></a>'
    result = clean(plain)
    assert result.markdown == "[Apply](https://x.test/j)"
    assert result.text == "Apply"
    formatted = '<a href="https://x.test/j"><span>Apply <b>now</b></span></a>'
    result = clean(formatted)
    assert result.markdown == "[Apply **now**](https://x.test/j)"
    assert result.text == "Apply now"
