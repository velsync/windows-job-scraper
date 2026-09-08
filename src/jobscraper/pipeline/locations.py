"""Deterministic multi-location normalization (01 §33.2, ROAD-03).

The canonical model deliberately keeps a *set* of locations with structured
fields instead of one `location_text`: a posting can be simultaneously
"Berlin + Paris", "country-level remote", or "two hubs with an offset span".
Presentation strings are derived from the set (see `location_summary`), never
stored as the model of record.

Versioned rules only, and always conservative:

* an unrecognized place is **kept** at low confidence, never dropped (data
  preservation beats a tidy row);
* no country is inferred from a bare city name, and no timezone is recorded
  unless the evidence carried a zone — `timezone_min/max` stay NULL otherwise;
* UTC offsets are resolved against the *recorded* observation instant
  (`reference_at`), so DST handling is reproducible rather than wall-clock
  dependent;
* the country-name table is explicit: an unknown country name stays unresolved
  in `raw_text` instead of being guessed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

#: Versioned rule set (ARC-10, RUN-21): recorded alongside every projection so
#: a later rule change is never mistaken for a data change.
LOCATION_RULES_VERSION = "location-rules-v1"

_REMOTE_TOKENS = ("remote", "distributed", "work from home", "anywhere", "wfh")

#: Explicit English/local country name → ISO 3166-1 alpha-2.  Only what is
#: actually listed here resolves; anything else stays unresolved on purpose.
COUNTRY_NAMES: dict[str, str] = {
    "argentina": "AR",
    "australia": "AU",
    "austria": "AT",
    "belgium": "BE",
    "brazil": "BR",
    "bulgaria": "BG",
    "canada": "CA",
    "chile": "CL",
    "colombia": "CO",
    "croatia": "HR",
    "cyprus": "CY",
    "czechia": "CZ",
    "czech republic": "CZ",
    "denmark": "DK",
    "estonia": "EE",
    "finland": "FI",
    "france": "FR",
    "deutschland": "DE",
    "germany": "DE",
    "greece": "GR",
    "hungary": "HU",
    "iceland": "IS",
    "india": "IN",
    "indonesia": "ID",
    "ireland": "IE",
    "israel": "IL",
    "italia": "IT",
    "italy": "IT",
    "japan": "JP",
    "kenya": "KE",
    "latvija": "LV",
    "latvia": "LV",
    "lithuania": "LT",
    "lietuva": "LT",
    "luxembourg": "LU",
    "mexico": "MX",
    "morocco": "MA",
    "netherlands": "NL",
    "holland": "NL",
    "new zealand": "NZ",
    "nigeria": "NG",
    "norway": "NO",
    "polska": "PL",
    "poland": "PL",
    "portugal": "PT",
    "romania": "RO",
    "romana": "RO",
    "serbia": "RS",
    "singapore": "SG",
    "slovakia": "SK",
    "slovenia": "SI",
    "south africa": "ZA",
    "korea": "KR",
    "south korea": "KR",
    "espana": "ES",
    "spain": "ES",
    "sverige": "SE",
    "sweden": "SE",
    "schweiz": "CH",
    "switzerland": "CH",
    "türkiye": "TR",
    "turkey": "TR",
    "ukraine": "UA",
    "emirates": "AE",
    "united arab emirates": "AE",
    "united kingdom": "GB",
    "great britain": "GB",
    "england": "GB",
    "united states": "US",
    "usa": "US",
    "united states of america": "US",
    "vietnam": "VN",
}

_ISO2 = {code for code in COUNTRY_NAMES.values()}

# A place name we can actually treat as a city label: short, no control or
# typographic noise, and not a URL fragment.
_NAME_OK = set(
    "abcdefghijklmnopqrstuvwxyzàáâãäåæçèéêëìíîïðñòóôõöøùúûüýþÿăâîșțşũĩ'&.,- "
)


@dataclass(frozen=True)
class LocationRecord:
    """One structured location of a job (01 §33.2)."""

    raw_text: str
    country: str | None = None
    region: str | None = None
    city: str | None = None
    remote: int = 0
    timezone_min: int | None = None
    timezone_max: int | None = None
    source_location_id: str | None = None
    confidence: float = 0.3
    #: explicit entry mode claimed by the source, when it claimed one
    entry_mode: str | None = None
    rules_version: str = LOCATION_RULES_VERSION

    def as_dict(self) -> dict:
        return {
            "raw_text": self.raw_text,
            "country": self.country,
            "region": self.region,
            "city": self.city,
            "remote": self.remote,
            "timezone_min": self.timezone_min,
            "timezone_max": self.timezone_max,
            "source_location_id": self.source_location_id,
            "confidence": self.confidence,
            "entry_mode": self.entry_mode,
            "rules_version": self.rules_version,
        }


def _looks_like_place_name(value: str) -> bool:
    text = value.strip()
    if not 1 < len(text) <= 48:
        return False
    if any(ch for ch in text.lower() if ch not in _NAME_OK):
        return False
    # a bare initialism or a sentence fragment is not a place name
    words = [w for w in text.replace(",", " ").split() if w]
    return len(words) <= 3


def _country_from(value: Any) -> str | None:
    """Map an explicit country name or alpha-2 code; never guess."""
    if isinstance(value, dict):
        value = value.get("name") or value.get("code")
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    lowered = text.lower().rstrip(".")
    if lowered in COUNTRY_NAMES:
        return COUNTRY_NAMES[lowered]
    upper = text.upper()
    if len(upper) == 2 and upper in _ISO2:
        return upper
    return None


def _text(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("name") or value.get("text")
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _zone_offsets(zones: Iterable[Any], reference_at: str | None) -> tuple[int | None, int | None]:
    """UTC offsets in minutes for the *recorded* instant, or (None, None)."""
    offsets: list[int] = []
    moments = [z for z in zones if isinstance(z, str) and z.strip()]
    if not moments or not reference_at:
        return None, None
    from datetime import datetime, timezone

    try:
        from zoneinfo import ZoneInfo

        stamp = reference_at.replace("Z", "+00:00")
        try:
            at = datetime.fromisoformat(stamp)
        except ValueError:  # pragma: no cover - defensive on malformed input
            at = datetime.now(timezone.utc)
        if at.tzinfo is None:  # pragma: no cover - defensive
            at = at.replace(tzinfo=timezone.utc)
    except Exception:  # pragma: no cover - zoneinfo/tzdata unavailable
        return None, None
    for zone in moments:
        try:
            offset = ZoneInfo(zone.strip()).utcoffset(at)
        except Exception:
            continue
        if offset is not None:
            offsets.append(int(offset.total_seconds() // 60))
    if not offsets:
        return None, None
    return min(offsets), max(offsets)


def _entry_mode(location_type: str | None, remote_flag: int) -> str | None:
    """Explicit mode only: REMOTE / HYBRID / ONSITE, never an inference."""
    text = (location_type or "").lower()
    if "hybrid" in text:
        return "HYBRID"
    if "onsite" in text.replace(" ", "") or "on-site" in text:
        return "ONSITE"
    if remote_flag or "remote" in text:
        return "REMOTE"
    return None


def remote_mode_of(records: Iterable[LocationRecord]) -> str:
    """One canonical mode derived from the location set (01 §33.2)."""
    rows = [r for r in records if r.entry_mode]
    if not rows:
        remote = [r for r in records if r.remote]
        return "REMOTE" if records and len(remote) == len(records) else "UNSPECIFIED"
    modes = {r.entry_mode for r in rows}
    if modes == {"REMOTE"}:
        return "REMOTE"
    if "HYBRID" in modes or ({"REMOTE", "ONSITE"} <= modes):
        return "HYBRID"
    if modes == {"ONSITE"}:
        return "ONSITE"
    return "UNSPECIFIED"  # pragma: no cover - all modes are listed above


def _from_string(part: str) -> LocationRecord:
    raw = part.strip()
    lowered = raw.lower()
    remote_flag = 1 if any(token in lowered for token in _REMOTE_TOKENS) else 0
    # "City, Country" / "City, Region, Country" / "Country"
    segments = [s.strip() for s in raw.split(",") if s.strip()]
    country = None
    if segments:
        country = _country_from(segments[-1]) if len(segments) > 1 else _country_from(segments[0])
    if country and len(segments) == 1 and not remote_flag:
        return LocationRecord(raw_text=raw, country=country, confidence=0.7)
    if country:
        remainder = segments[:-1] if len(segments) > 1 else []
        city = _text(remainder[-1]) if remainder else None
        region = _text(remainder[-2]) if len(remainder) > 1 else None
        if city and not _looks_like_place_name(city):
            city = None
        if city is None and not remote_flag:
            # a country-qualified fragment we cannot safely split further
            return LocationRecord(raw_text=raw, country=country, confidence=0.6)
        return LocationRecord(
            raw_text=raw,
            country=country,
            region=region,
            city=city,
            remote=remote_flag,
            confidence=0.85 if city or remote_flag else 0.7,
        )
    if remote_flag:
        return LocationRecord(
            raw_text=raw, remote=1, confidence=0.5, entry_mode="REMOTE"
        )
    if len(segments) == 1 and _looks_like_place_name(raw):
        return LocationRecord(raw_text=raw, city=raw, confidence=0.6)
    # preserved but unresolved: an operator can still read what the source said
    return LocationRecord(raw_text=raw, confidence=0.3)


def _from_dict(value: dict, *, reference_at: str | None) -> LocationRecord:
    city = _text(value.get("city"))
    if city and not _looks_like_place_name(city):
        city = None
    region = _text(value.get("region") or value.get("state") or value.get("province"))
    country = _country_from(value.get("country") or value.get("country_code"))
    location_type = _text(value.get("location_type") or value.get("type")) or ""
    remote_value = value.get("remote")
    if remote_value is None:
        remote_value = value.get("isRemote") or value.get("is_remote")
    remote_flag = 1 if (remote_value or "remote" in location_type.lower()) else 0
    entry_mode = _entry_mode(location_type, remote_flag)
    zones = list(value.get("alternate_timezones") or [])
    single_zone = value.get("timezone") or value.get("tz") or value.get("time_zone")
    if isinstance(single_zone, str) and single_zone.strip():
        zones.insert(0, single_zone)
    tz_min, tz_max = _zone_offsets(zones, reference_at)
    source_id = _text(value.get("id") or value.get("source_location_id"))
    parts = " / ".join(p for p in (city, region, country) if p) or (
        _text(value.get("name")) or _text(value.get("location")) or ""
    )
    raw = parts.strip() or location_type or "unspecified"
    if remote_flag and "remote" not in raw.lower():
        raw = f"{raw} (Remote)".strip() if raw != "unspecified" else "Remote"
    if city and (country or region):
        confidence = 0.95
    elif city or country or remote_flag:
        confidence = 0.85
    else:
        confidence = 0.4
    return LocationRecord(
        raw_text=raw,
        country=country,
        region=region,
        city=city,
        remote=remote_flag,
        timezone_min=tz_min,
        timezone_max=tz_max,
        source_location_id=source_id,
        confidence=confidence,
        entry_mode=entry_mode,
    )


def normalize_locations(
    values: Any,
    *,
    job_location_type: str | None = None,
    applicant_location_requirements: Iterable[Any] | None = None,
    reference_at: str | None = None,
) -> tuple[LocationRecord, ...]:
    """Normalize every location signal of one observation into a stable set.

    ``values`` accepts a string, a list of strings, or a list of structured
    location objects (ATS-native shapes).  ``job_location_type`` (Lever) and
    ``applicant_location_requirements`` (Greenhouse) are the documented
    side-channels and are folded into the same set, never preferred over
    explicit rows.
    """
    if values is None:
        values = []
    if isinstance(values, str):
        values = [values]

    records: list[LocationRecord] = []
    for item in values:
        if isinstance(item, dict):
            records.append(_from_dict(item, reference_at=reference_at))
            continue
        text = _text(item)
        if not text:
            continue
        for part in _split_multi(text):
            records.append(_from_string(part))

    for requirement in applicant_location_requirements or ():
        if isinstance(requirement, dict):
            records.append(_requirement(requirement))
        else:
            text = _text(requirement)
            if text:
                for part in _split_multi(text):
                    records.append(_from_string(part))

    remote_only = (job_location_type or "").strip().upper() in {
        "REMOTE",
        "FULLY_REMOTE",
        "ANYWHERE",
    }
    if remote_only:
        if not records:
            records.append(
                LocationRecord(
                    raw_text="Remote", remote=1, confidence=0.5, entry_mode="REMOTE"
                )
            )
        else:
            records = [
                LocationRecord(**{**record.__dict__, "remote": 1, "raw_text": record.raw_text})
                for record in records
            ]

    seen: set[str] = set()
    unique: list[LocationRecord] = []
    for record in records:
        key = record.raw_text.casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(record)
    return tuple(unique)


def _split_multi(text: str) -> list[str]:
    """Split the documented multi-location separators, preserving order."""
    parts: list[str] = []
    for chunk in text.split("/"):
        for piece in chunk.split(";"):
            trimmed = piece.strip()
            if trimmed:
                parts.append(trimmed)
    return parts


def _requirement(value: dict) -> LocationRecord:
    location_type = _text(value.get("location_type")) or ""
    remote_flag = 1 if "remote" in location_type.lower() else 0
    city = _text(value.get("city"))
    if city and not _looks_like_place_name(city):
        city = None
    region = _text(value.get("state") or value.get("province") or value.get("region"))
    country = _country_from(value.get("country"))
    raw = " / ".join(p for p in (city, region, country) if p) or location_type or "unspecified"
    return LocationRecord(
        raw_text=raw,
        country=country,
        region=region,
        city=city,
        remote=remote_flag,
        confidence=0.85,
        entry_mode=_entry_mode(location_type, remote_flag),
    )


def project_job_locations(conn, *, job_id: str, records: Iterable[LocationRecord]) -> int:
    """Replace the canonical job's location set with this evidence (01 §33.2).

    The set is a *projection* of the winning presence: rows are rewritten from
    the evidence, and the observation rows they came from stay immutable
    (RUN-21 governs canonical state, not the derived projection).
    """
    rows = list(records)
    conn.execute("DELETE FROM job_locations WHERE job_id = ?", (job_id,))
    if not rows:
        return 0
    from jobscraper.ids import new_id

    for record in rows:
        conn.execute(
            """
            INSERT INTO job_locations (id, job_id, raw_text, country, region,
                city, remote, timezone_min, timezone_max, source_location_id,
                confidence)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("jl"),
                job_id,
                record.raw_text,
                record.country,
                record.region,
                record.city,
                record.remote,
                record.timezone_min,
                record.timezone_max,
                record.source_location_id,
                record.confidence,
            ),
        )
    return len(rows)


def location_rows_equal(before: list, after: Iterable[LocationRecord]) -> bool:
    """True when the projected set is unchanged (drives LOCATIONS_CHANGED)."""
    current = sorted(
        (
            row["raw_text"],
            row["country"],
            row["region"],
            row["city"],
            int(row["remote"] or 0),
        )
        for row in before
    )
    proposed = sorted(
        (r.raw_text, r.country, r.region, r.city, int(r.remote)) for r in after
    )
    return current == proposed


def location_sets_conflict(rows_a: Iterable, rows_b: Iterable) -> bool:
    """§38 stage 5: *meaningful* disagreement only.

    Missing information is never a disagreement.  A conflict requires both
    sides to have stated the same kind of thing (cities, or else countries) and
    those statements to be disjoint — "Berlin" versus "Berlin, Germany" is
    compatible, "Berlin" versus "Amsterdam, Netherlands" is not.
    """
    def parts(rows):
        cities, countries = set(), set()
        for row in rows:
            if hasattr(row, "keys"):
                city, country = row["city"], row["country"]
            else:
                city, country = row.city, row.country
            if city:
                cities.add(str(city).strip().lower())
            if country:
                countries.add(str(country).strip().upper())
        return cities, countries

    cities_a, countries_a = parts(rows_a)
    cities_b, countries_b = parts(rows_b)
    if cities_a and cities_b and not (cities_a & cities_b):
        return True
    if countries_a and countries_b and not (countries_a & countries_b):
        return True
    return False


def location_summary(records: Iterable[LocationRecord]) -> str:
    """Presentation string derived from the set (never a stored column)."""
    parts = [record.raw_text for record in records if record.raw_text]
    return "; ".join(dict.fromkeys(parts))


#: Explicit worldwide wording.  A bare "Remote" is *not* evidence of
#: unrestricted scope: it may well be a country-restricted remote role whose
#: restriction the source simply did not repeat.  Slice 1's eligibility
#: semantics depend on that distinction ("no country proven" must stay an
#: honest LIKELY, never ELIGIBLE), so no inference happens here.
_WORLDWIDE_TOKENS = ("worldwide", "anywhere", "everywhere", "global", "no location restriction")


def remote_worldwide(records: Iterable[LocationRecord]) -> bool:
    """True only when the evidence says remote *and* unrestricted."""
    rows = list(records)
    if not rows:
        return False
    return all(
        record.remote == 1
        and record.country is None
        and record.city is None
        and any(token in record.raw_text.lower() for token in _WORLDWIDE_TOKENS)
        for record in rows
    )


__all__ = [
    "COUNTRY_NAMES",
    "location_rows_equal",
    "location_sets_conflict",
    "project_job_locations",
    "LOCATION_RULES_VERSION",
    "LocationRecord",
    "location_summary",
    "normalize_locations",
    "remote_mode_of",
    "remote_worldwide",
]
