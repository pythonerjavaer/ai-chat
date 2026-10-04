"""Offline administrative references for explicitly stated public job places.

This is not address geocoding. Only ``city``/``location`` fields are resolved;
employers, job prose and account data never establish a location. Coordinates
describe catalog reference centers, not offices, exact addresses or GPS fixes.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any


_LEVELS = frozenset({"province", "city", "district"})
_WGS84 = frozenset({"wgs84", "wgs-84", "epsg:4326", "4326"})
_NON_PLACE = re.compile(
    r"全国|全球|不限|待定|待确认|未确定|远程|线上|居家|"
    r"\b(?:remote|nationwide|worldwide|anywhere|unspecified|tbd)\b", re.I)
_SPLIT = re.compile(r"[,，;；、/|&\n]+")
_LABEL = re.compile(r"^(?:(?:工作|办公|招聘|岗位)(?:地点|地址|城市)|地点|地址|城市|location)\s*[:：]\s*", re.I)
_ADDRESS_END = re.compile(r"(?:路|街|道|巷|号|大厦|园区|楼|室|镇|乡)")
_NARRATIVE = re.compile(r"客户|服务|支持|业务|面向|负责|覆盖|总部位于|headquarter|customer|support", re.I)
_COORDINATE_NOTE = "标点为公开行政参考中心，并非企业总部、岗位办公地址或精密 GPS；仅已确认 WGS84 的参考点输出 GeoJSON。"


def _normalized(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value)).strip().casefold()


class ChinaPlaceCatalog:
    """Code-owned catalog/index; no job or account text is cached here."""

    def __init__(self, data: dict[str, Any]):
        self.coordinate_note = str(data.get("coordinate_note") or _COORDINATE_NOTE)
        self._places: dict[str, dict[str, Any]] = {}
        aliases: dict[str, set[str]] = {}
        for row in data.get("places") or []:
            if not isinstance(row, dict) or row.get("level") not in _LEVELS:
                continue
            identity, name = str(row.get("id") or ""), str(row.get("name") or "")
            if not re.fullmatch(r"[A-Za-z0-9_:-]{1,64}", identity) or not name:
                continue
            place = {key: row.get(key) for key in (
                "id", "name", "level", "parent_id", "province_id", "province_name",
                "city_id", "city_name", "longitude", "latitude", "crs", "coordinate_source",
            )}
            place["id"], place["name"] = identity, name
            if place["level"] == "province":
                place["province_id"], place["province_name"] = identity, name
            center = row.get("center")
            if isinstance(center, list) and len(center) == 2:
                for key, value in zip(("longitude", "latitude"), center):
                    if place[key] is None:
                        place[key] = value
            for key, bound in (("longitude", 180), ("latitude", 90)):
                value = place[key]
                place[key] = float(value) if (type(value) in (int, float)
                                             and math.isfinite(value) and abs(value) <= bound) else None
            place["crs"] = "WGS84" if str(place.get("crs") or "").casefold() in _WGS84 else "unknown-display"
            place["catalog_source"] = str(row.get("source") or data.get("source") or "offline_catalog")
            self._places[identity] = place
            # Individually reviewed historical/conflicting source rows remain
            # auditable by ID, but must not locate current public jobs.
            if row.get("matching_enabled") is False:
                continue
            row_aliases = row.get("aliases") or []
            names = [name, *(row_aliases if isinstance(row_aliases, list) else [])]
            for alias in names:
                if isinstance(alias, str) and (key := _normalized(alias)):
                    aliases.setdefault(key, set()).add(identity)
        self._aliases = {key: tuple(sorted(ids)) for key, ids in aliases.items()}
        expressions = []
        for alias in sorted(self._aliases, key=lambda value: (-len(value), value)):
            expression = re.escape(alias).replace(r"\ ", r"\s+")
            if alias[0].isascii() and alias[0].isalnum():
                expression = r"(?<![a-z0-9])" + expression
            if alias[-1].isascii() and alias[-1].isalnum():
                expression += r"(?![a-z0-9])"
            expressions.append(expression)
        self._pattern = re.compile("|".join(expressions)) if expressions else None

    def place(self, identity: str, *, source: str = "catalog_parent") -> dict[str, Any] | None:
        row = self._places.get(identity)
        if row is None:
            return None
        # Exact output allowlist: aliases, source payloads and raw input are
        # never reflected, and every call returns independently mutable data.
        result = {key: value for key, value in row.items() if key != "parent_id"}
        result.update(accuracy="administrative_center", source=source if source in {
            "city", "location", "catalog_parent"} else "city")
        return result

    def _ancestors(self, identity: str) -> list[str]:
        ancestors, seen = [], {identity}
        while identity in self._places and len(ancestors) < 2:
            parent = self._places[identity].get("parent_id")
            if not parent or parent in seen or parent not in self._places:
                break
            ancestors.append(parent)
            seen.add(parent)
            identity = parent
        return ancestors

    def hierarchy(self, places: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        output, links = {}, {}
        for place in places:
            identity = str(place.get("id") or "")
            leaf = self.place(identity, source=str(place.get("source") or "city"))
            if leaf is None:
                continue
            output.setdefault(identity, leaf)
            child = identity
            for parent in self._ancestors(identity):
                output.setdefault(parent, self.place(parent))
                links[(child, parent)] = {"child": child, "parent": parent}
                child = parent
        return list(output.values()), list(links.values())

    def _specific(self, identities: tuple[str, ...] | list[str]) -> list[str]:
        return [identity for identity in identities if not any(
            identity in self._ancestors(other) for other in identities if identity != other)]

    def _alias_candidates(self, alias: str) -> list[str]:
        identities = self._aliases[alias]
        cities = {identity: _normalized(self._places[identity]["name"])
                  for identity in identities if self._places[identity]["level"] == "city"}
        # Historical ADM3 references can carry exactly their parent city's
        # name and aliases. That shared text establishes the city, not finer
        # district precision. Keep distinct district aliases available and
        # leave other shared aliases ambiguous instead of silently choosing
        # a child. Province/city duplicates (municipalities) may still choose
        # a more specific city when their canonical names are identical.
        identities = [identity for identity in identities if not (
            self._places[identity]["level"] == "district" and any(
                cities.get(parent) == _normalized(self._places[identity]["name"])
                for parent in self._ancestors(identity) if parent in cities))]
        return [identity for identity in identities if not (
            self._places[identity]["level"] == "province" and any(
                identity in self._ancestors(city) and name == _normalized(self._places[identity]["name"])
                for city, name in cities.items()))]

    def _resolve_part(self, text: str) -> tuple[list[str], bool]:
        if not text or _NON_PLACE.search(text) or self._pattern is None:
            return [], False
        matches = list(self._pattern.finditer(text))
        if not matches:
            return [], False
        groups = [self._alias_candidates(_normalized(match.group())) for match in matches]
        # Reject arbitrary prose in a purported location field. Full explicit
        # administrative paths may have a trailing street/building address;
        # bare street names such as 南京西路 cannot become 南京市.
        remainder = self._pattern.sub(" ", text)
        remainder = re.sub(r"中国大陆|中国|\b(?:china|prc|and)\b|[\s:：.·()（）\[\]{}-]+|(?:以及|和|及|或)", "", remainder)
        explicit_path = len(matches) > 1 or any(match.group().endswith(("省", "市", "区", "县", "旗")) for match in matches)
        if remainder and not (explicit_path and _ADDRESS_END.search(remainder) and not _NARRATIVE.search(remainder)):
            return [], False
        provinces = {self._places[ids[0]]["province_id"] for ids in groups if len(ids) == 1
                     and self._places[ids[0]]["level"] == "province"}
        cities = {self._places[ids[0]].get("city_id") or ids[0] for ids in groups if len(ids) == 1
                  and self._places[ids[0]]["level"] == "city"}
        resolved, ambiguous = [], False
        for ids in groups:
            if provinces:
                ids = [identity for identity in ids if self._places[identity]["province_id"] in provinces]
                if not ids:
                    return [], True
            if cities and (len(ids) > 1 or all(self._places[identity]["level"] == "district" for identity in ids)):
                ids = [identity for identity in ids if (self._places[identity].get("city_id") or identity) in cities]
                if not ids:
                    return [], True
            if len(ids) != 1:
                ambiguous = True
            else:
                resolved.append(ids[0])
        # A contradictory explicit province/city must not leave a misleading
        # province-only pin after its more specific place failed validation.
        if provinces and any(self._places[identity]["province_id"] not in provinces
                             for ids in groups for identity in ids if len(ids) == 1):
            return [], True
        return self._specific(list(dict.fromkeys(resolved))), ambiguous

    def resolve(self, value: Any, *, source: str = "city") -> tuple[list[dict[str, Any]], str]:
        values = [value] if isinstance(value, str) else value if isinstance(value, list) else []
        identities, ambiguous = [], False
        for value in values[:30]:
            if not isinstance(value, str) or len(value) > 2_000:
                continue
            text = _LABEL.sub("", _normalized(value))
            for part in _SPLIT.split(text):
                # Preserve administrative names such as Central and Western;
                # otherwise English "and" is an explicit multi-place list.
                parts = [part] if _normalized(part) in self._aliases else re.split(r"\s+and\s+", part)
                for part in parts:
                    found, unclear = self._resolve_part(part.strip())
                    identities.extend(found)
                    ambiguous = ambiguous or unclear
        identities = self._specific(list(dict.fromkeys(identities)))[:30]
        places = [self.place(identity, source=source) for identity in identities]
        return places, "ambiguous" if ambiguous else "mapped" if places else "unmapped"


@lru_cache(maxsize=1)
def china_place_catalog() -> ChinaPlaceCatalog:
    try:
        data = json.loads((Path(__file__).parent / "data" / "china_places.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # Missing optional assets must not break the authoritative job pool.
        data = {"places": [], "coordinate_note": _COORDINATE_NOTE}
    return ChinaPlaceCatalog(data if isinstance(data, dict) else {})


def resolve_opportunity_places(job: dict[str, Any], *, catalog: ChinaPlaceCatalog | None = None) -> tuple[list[dict[str, Any]], str]:
    catalog = catalog or china_place_catalog()
    field = "location" if job.get("location") else "city"
    return catalog.resolve(job.get(field), source=field)


def geography_projection(items: list[dict[str, Any]], *, catalog: ChinaPlaceCatalog | None = None) -> dict[str, Any]:
    """Aggregate only current public IDs; parent pins are explicitly parents."""
    catalog = catalog or china_place_catalog()
    locations, opportunities, direct = {}, {}, {}
    matched = 0
    for item in items:
        identity = str(item.get("id") or "")
        if not identity:
            continue
        places = [place for place in item.get("places") or []
                  if isinstance(place, dict) and catalog.place(str(place.get("id") or ""))]
        matched += bool(places)
        for place in places:
            direct.setdefault(place["id"], set()).add(identity)
            hierarchy, _links = catalog.hierarchy([place])
            for location in hierarchy:
                key = location["id"]
                locations.setdefault(key, location)
                opportunities.setdefault(key, set()).add(identity)
    features = []
    for identity, place in locations.items():
        if place["crs"] != "WGS84" or place["longitude"] is None or place["latitude"] is None:
            continue
        ids = sorted(opportunities[identity])
        features.append({"type": "Feature", "id": identity,
                         "geometry": {"type": "Point", "coordinates": [place["longitude"], place["latitude"]]},
                         "properties": {**place, "opportunity_ids": ids, "opportunity_count": len(ids),
                                        "direct_opportunity_ids": sorted(direct.get(identity, set()))}})
    return {"features": {"type": "FeatureCollection", "features": features},
            "matched_opportunities": matched, "unmapped_opportunities": len(items) - matched,
            "ambiguous_opportunities": sum(item.get("location_status") == "ambiguous" for item in items),
            "unplottable_places": len(locations) - len(features), "scope": "current_public_projection",
            "coordinate_note": f"{catalog.coordinate_note} {_COORDINATE_NOTE}"}
