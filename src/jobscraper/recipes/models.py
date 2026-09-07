"""ExtractionRecipe model and validation.

Authority: module 02 section 22 (ExtractionRecipe operates on already-valid
content; required-field failure never invents values).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

RECIPE_SCHEMA_VERSION = 1
MODES = ("HTML", "JSON_LD", "EMBEDDED_JSON", "API_JSON")

LOCATOR_KINDS = (
    "data_attr",
    "relative_css",
    "css",
    "role",
    "href_pattern",
    "text_anchor",
    "json_path",
    "json_ld_path",
)


class RecipeValidationError(ValueError):
    pass


@dataclass(frozen=True)
class Locator:
    kind: str
    value: str

    def validate(self) -> None:
        if self.kind not in LOCATOR_KINDS:
            raise RecipeValidationError(f"unknown locator kind {self.kind}")
        if not self.value or not isinstance(self.value, str):
            raise RecipeValidationError("locator value required")
        if len(self.value) > 500:
            raise RecipeValidationError("locator value too long")


@dataclass(frozen=True)
class FieldSpec:
    name: str
    required: bool = False
    locators: tuple[Locator, ...] = ()

    def validate(self) -> None:
        if not self.name or not isinstance(self.name, str):
            raise RecipeValidationError("field name required")
        for loc in self.locators:
            loc.validate()
        if self.required and not self.locators:
            raise RecipeValidationError(f"required field {self.name} needs locators")


@dataclass(frozen=True)
class ExtractionRecipe:
    """A versioned recipe. Multiple ordered locator candidates per field."""

    mode: str
    card_locators: tuple[Locator, ...] = ()
    fields: dict[str, FieldSpec] = field(default_factory=dict)
    schema_version: int = RECIPE_SCHEMA_VERSION

    def validate(self) -> None:
        if self.mode not in MODES:
            raise RecipeValidationError(f"unsupported mode {self.mode}")
        for loc in self.card_locators:
            loc.validate()
        for name, spec in self.fields.items():
            if name != spec.name:
                raise RecipeValidationError("field key/name mismatch")
            spec.validate()

    def to_json(self) -> str:
        return json.dumps(
            {
                "schema_version": self.schema_version,
                "mode": self.mode,
                "card_locators": [{"kind": l.kind, "value": l.value} for l in self.card_locators],
                "fields": {
                    name: {"required": f.required, "locators": [{"kind": l.kind, "value": l.value} for l in f.locators]}
                    for name, f in self.fields.items()
                },
            },
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, text: str | dict) -> "ExtractionRecipe":
        data = json.loads(text) if isinstance(text, str) else dict(text)
        version = int(data.get("schema_version", 1))
        if version != RECIPE_SCHEMA_VERSION:
            raise RecipeValidationError(f"unsupported recipe schema version {version}")
        mode = data.get("mode")
        if mode not in MODES:
            raise RecipeValidationError(f"unsupported mode {mode}")
        cards = tuple(Locator(**l) for l in data.get("card_locators", []))
        fields = {}
        for name, spec in (data.get("fields") or {}).items():
            fields[name] = FieldSpec(
                name=name,
                required=bool(spec.get("required", False)),
                locators=tuple(Locator(**l) for l in spec.get("locators", [])),
            )
        recipe = cls(mode=mode, card_locators=cards, fields=fields)
        recipe.validate()
        return recipe
