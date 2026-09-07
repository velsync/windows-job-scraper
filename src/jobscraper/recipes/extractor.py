"""Recipe-driven extraction from validated content.

Authority: module 02 section 22. Recipes run only on valid page classes;
required-field failure produces structured review evidence, never invented
values. Locator telemetry is recorded for repairability.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from jobscraper.recipes import htmlutil
from jobscraper.recipes.models import ExtractionRecipe, Locator


@dataclass
class ExtractionResult:
    records: list[dict] = field(default_factory=list)
    telemetry: list[dict] = field(default_factory=list)
    missing_required: list[dict] = field(default_factory=list)


def _apply_locator(element: htmlutil.Element, locator: Locator, base_url: str) -> str | None:
    kind = locator.kind
    if kind == "relative_css":
        for sel in locator.value.split(","):
            found = htmlutil.css_select(element, sel.strip())
            if found:
                return found[0].text()
        return None
    if kind == "css":
        for sel in locator.value.split(","):
            found = htmlutil.css_select(element, sel.strip())
            if found:
                return found[0].text()
        return None
    if kind == "data_attr":
        found = htmlutil.select_by_data_attr(element, locator.value)
        return found[0].text() if found else None
    if kind == "role":
        found = htmlutil.select_by_role(element, locator.value)
        return found[0].text() if found else None
    if kind == "href_pattern":
        found = htmlutil.select_by_href_pattern(element, locator.value)
        return htmlutil.absolute_url(base_url, found[0].attr("href")) if found else None
    if kind == "text_anchor":
        found = htmlutil.select_by_text_anchor(element, locator.value)
        return found[0].text() if found else None
    if kind in ("json_path", "json_ld_path"):
        # For structured modes, element is a dict.
        if isinstance(element, dict):
            return _json_path(element, locator.value)
        return None
    return None


def _json_path(data: dict, path: str) -> str | None:
    current = data
    for part in path.split("."):
        if part.startswith("["):
            continue
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    if isinstance(current, (str, int, float, bool)):
        return str(current)
    return json.dumps(current, sort_keys=True) if current is not None else None


def _find_cards(root: htmlutil.Element, recipe: ExtractionRecipe) -> list:
    if not recipe.card_locators:
        return [root]
    for locator in recipe.card_locators:
        try:
            if locator.kind == "data_attr":
                cards = htmlutil.select_by_data_attr(root, locator.value)
            elif locator.kind == "role":
                cards = htmlutil.select_by_role(root, locator.value)
            elif locator.kind == "css" or locator.kind == "relative_css":
                cards = htmlutil.css_select(root, locator.value)
            else:
                cards = []
        except Exception:
            cards = []
        if cards:
            return cards
    return []


def extract_records(recipe: ExtractionRecipe, html: str, base_url: str) -> list[dict]:
    """Extract job records from valid HTML using ordered locator fallback."""
    result = _extract(recipe, html, base_url)
    return result.records


def extract_with_telemetry(recipe: ExtractionRecipe, html: str, base_url: str) -> ExtractionResult:
    return _extract(recipe, html, base_url)


def _extract(recipe: ExtractionRecipe, html: str, base_url: str) -> ExtractionResult:
    result = ExtractionResult()
    if recipe.mode in ("HTML", "JSON_LD"):
        root = htmlutil.parse_html(html)
        cards = _find_cards(root, recipe)
        for card in cards:
            record: dict = {}
            card_missing: list[str] = []  # required fields missing on THIS card
            for name, spec in recipe.fields.items():
                value = None
                for locator in spec.locators:
                    value = _apply_locator(card, locator, base_url)
                    if value:
                        result.telemetry.append(
                            {"field": name, "locator_kind": locator.kind, "hit": True}
                        )
                        break
                    result.telemetry.append(
                        {"field": name, "locator_kind": locator.kind, "hit": False}
                    )
                if value is None and spec.required:
                    card_missing.append(name)
                    result.missing_required.append(
                        {"field": name, "card": card.text()[:120]}
                    )
                if value is not None:
                    record[name] = value
            # A card missing its own required fields is dropped with evidence;
            # other cards are unaffected. Fields absent from the recipe (e.g.
            # no job_url locator) stay absent — no invented fallback URLs.
            if record and not card_missing:
                result.records.append(record)
    elif recipe.mode in ("EMBEDDED_JSON", "API_JSON"):
        try:
            data = json.loads(html)
        except ValueError:
            return result
        items = data if isinstance(data, list) else _find_array(data)
        for item in items or []:
            if not isinstance(item, dict):
                continue
            record = {}
            for name, spec in recipe.fields.items():
                value = None
                for locator in spec.locators:
                    if locator.kind in ("json_path", "json_ld_path"):
                        value = _json_path(item, locator.value)
                    if value:
                        result.telemetry.append({"field": name, "locator_kind": locator.kind, "hit": True})
                        break
                    result.telemetry.append({"field": name, "locator_kind": locator.kind, "hit": False})
                if value is not None:
                    record[name] = value
                elif spec.required:
                    result.missing_required.append({"field": name})
            if record:
                result.records.append(record)
    return result


def _find_array(data) -> list | None:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("jobs", "postings", "data", "results", "items", "positions"):
            if key in data and isinstance(data[key], list):
                return data[key]
        for value in data.values():
            found = _find_array(value)
            if found:
                return found
    return None
