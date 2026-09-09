from __future__ import annotations

from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    file = Path(path)
    text = file.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"expected exactly one match in {path}, found {count}")
    file.write_text(text.replace(old, new, 1), encoding="utf-8")


# Host-owned provider authority: reviewed provider API hosts may be added only
# from the versioned ATS endpoint table. Binding config remains data, never
# network authority.
driver = "src/jobscraper/pipeline/driver.py"
replace_once(
    driver,
    "from jobscraper.acquisition.envelope import (\n",
    "from jobscraper.acquisition.atsendpoints import spec_for_provider\n"
    "from jobscraper.acquisition.envelope import (\n",
)
replace_once(
    driver,
    '''def source_policy(source_row: sqlite3.Row) -> DestinationPolicy:\n    """Host-owned destination policy for one source (04 §5.1)."""\n    try:\n        normalized = normalize_url(source_row["entry_url"])\n        host = normalized.host or ""\n    except Exception:\n        host = ""\n    grant = None\n    try:\n        literal = ipaddress.ip_address(host.strip("[]"))\n        is_loopback = literal.is_loopback\n    except ValueError:\n        is_loopback = False\n    if is_loopback:\n        # Narrow host rule: only a source whose own entry host is a loopback\n        # literal gets a loopback grant for that exact host. Scraped content\n        # and imported configuration can never construct a grant.\n        grant = InternalGrant(\n            purpose="loopback-source-entry", allowed_hosts=frozenset({host})\n        )\n    return DestinationPolicy(\n        allowed_hosts=frozenset({host}) if host else None,\n        internal_grant=grant,\n        max_redirects=3,\n        timeout_s=30.0,\n        max_bytes=2_000_000,\n    )\n''',
    '''def source_policy(\n    source_row: sqlite3.Row | dict, *, adapter_id: str | None = None\n) -> DestinationPolicy:\n    """Host-owned destination policy for one source (04 §5.1).\n\n    A provider adapter may add only hosts from the reviewed, versioned ATS\n    endpoint table, and only when the Source entry host itself is a reviewed\n    host for that provider. Adapter config is deliberately not an input, so\n    imported/scraped data cannot widen network authority.\n    """\n    try:\n        normalized = normalize_url(source_row["entry_url"])\n        host = normalized.host or ""\n    except Exception:\n        host = ""\n\n    allowed_hosts: set[str] = {host} if host else set()\n    if adapter_id == "greenhouse" and host:\n        spec = spec_for_provider("GREENHOUSE")\n        if host in {*spec.hosted_hosts, *spec.api_hosts}:\n            allowed_hosts.update(spec.api_hosts)\n\n    grant = None\n    try:\n        literal = ipaddress.ip_address(host.strip("[]"))\n        is_loopback = literal.is_loopback\n    except ValueError:\n        is_loopback = False\n    if is_loopback:\n        # Narrow host rule: only a source whose own entry host is a loopback\n        # literal gets a loopback grant for that exact host. Scraped content\n        # and imported configuration can never construct a grant.\n        grant = InternalGrant(\n            purpose="loopback-source-entry", allowed_hosts=frozenset({host})\n        )\n    return DestinationPolicy(\n        allowed_hosts=frozenset(allowed_hosts) if allowed_hosts else None,\n        internal_grant=grant,\n        max_redirects=3,\n        timeout_s=30.0,\n        max_bytes=2_000_000,\n    )\n''',
)
replace_once(
    driver,
    "    policy = source_policy(source)\n",
    "    policy = source_policy(source, adapter_id=plan_row[\"adapter_id\"])\n",
)

# Current Greenhouse public response contract: provider-supplied company name
# and first_published outrank optional binding fallbacks.
greenhouse = "src/jobscraper/adapters/greenhouse.py"
replace_once(
    greenhouse,
    '''        put("source_job_id", job_id, "id")\n        put("title", title, "title")\n        put("company", self.config.company_name, "company_name", kind="binding_config")\n        put("careers_url", self.config.careers_url, "careers_url", kind="binding_config")\n''',
    '''        put("source_job_id", job_id, "id")\n        put("title", title, "title")\n        provider_company = _text(item.get("company_name"))\n        if provider_company is not None:\n            put("company", provider_company, "company_name")\n        else:\n            put("company", self.config.company_name, "company_name", kind="binding_config")\n        put("careers_url", self.config.careers_url, "careers_url", kind="binding_config")\n''',
)
replace_once(
    greenhouse,
    '''    for locator in (\n        "first_published_at",\n        "job_post_information.date_published",\n    ):\n''',
    '''    for locator in (\n        "first_published",\n        "first_published_at",\n        "job_post_information.date_published",\n    ):\n''',
)
replace_once(
    greenhouse,
    '    return None, "updated_at"\n',
    '    return None, ""\n',
)

# Greenhouse descriptions may encode the complete HTML markup as entities.
# Decode that representation exactly once, sanitize the now-visible HTML, and
# version the changed canonicalization semantics.
content = "src/jobscraper/pipeline/contentclean.py"
replace_once(
    content,
    'CONTENT_CLEANING_VERSION = "content-clean-v1"\n',
    'CONTENT_CLEANING_VERSION = "content-clean-v2"\n',
)
replace_once(
    content,
    '''def _safe_link(url: str, label: str) -> str:\n    """One HTML anchor → safe Markdown link or visible label only."""\n    url = html_lib.unescape(url or "").strip()\n    label = _collapse(_HTML_TAG.sub(" ", label or ""))\n''',
    '''def _safe_link(\n    url: str, label: str, *, decode_entities: bool = True\n) -> str:\n    """One HTML anchor → safe Markdown link or visible label only."""\n    url = (html_lib.unescape(url or "") if decode_entities else (url or "")).strip()\n    label = _collapse(_HTML_TAG.sub(" ", label or ""))\n''',
)
replace_once(
    content,
    'def _html_to_markdown(markup: str) -> str:\n',
    'def _html_to_markdown(markup: str, *, decode_entities: bool = True) -> str:\n',
)
replace_once(
    content,
    '        lambda m: _safe_link(m.group(1), m.group(2)),\n',
    '        lambda m: _safe_link(m.group(1), m.group(2), decode_entities=decode_entities),\n',
)
replace_once(
    content,
    '    text = _HTML_TAG.sub(" ", text)\n    return _collapse(html_lib.unescape(text))\n\n\ndef _html_to_text(markup: str) -> str:\n',
    '    text = _HTML_TAG.sub(" ", text)\n    collapsed = _collapse(text)\n    return html_lib.unescape(collapsed) if decode_entities else collapsed\n\n\ndef _html_to_text(markup: str, *, decode_entities: bool = True) -> str:\n',
)
replace_once(
    content,
    '    text = _HTML_TAG.sub(" ", text)\n    return _derived_text(html_lib.unescape(text))\n\n\ndef _clean_markdown(raw: str) -> str:\n',
    '    text = _HTML_TAG.sub(" ", text)\n    derived = _derived_text(text)\n    return html_lib.unescape(derived) if decode_entities else derived\n\n\ndef _clean_markdown(raw: str) -> str:\n',
)
replace_once(
    content,
    '''    if _looks_like_html(raw):\n        markdown = _html_to_markdown(raw)\n        text = _html_to_text(raw)\n    elif _looks_like_markdown(raw):\n        markdown = _clean_markdown(raw)\n        text = _markdown_to_text(markdown)\n    else:\n        text = _collapse(raw)\n        markdown = text\n''',
    '''    if _looks_like_html(raw):\n        markdown = _html_to_markdown(raw)\n        text = _html_to_text(raw)\n    else:\n        decoded_once = html_lib.unescape(raw)\n        if decoded_once != raw and _looks_like_html(decoded_once):\n            # Provider payloads such as Greenhouse encode the markup itself.\n            # The outer entity layer is decoded exactly once here; the HTML\n            # sanitizer must not decode nested text entities a second time.\n            markdown = _html_to_markdown(decoded_once, decode_entities=False)\n            text = _html_to_text(decoded_once, decode_entities=False)\n        elif _looks_like_markdown(raw):\n            markdown = _clean_markdown(raw)\n            text = _markdown_to_text(markdown)\n        else:\n            text = _collapse(raw)\n            markdown = text\n''',
)

# One-time applicator removes itself and its workflow from the resulting tree.
Path(".github/workflows/_repair_s25_greenhouse.yml").unlink(missing_ok=True)
Path(__file__).unlink(missing_ok=True)
