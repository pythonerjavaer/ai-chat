#!/usr/bin/env python3
"""Build offline display boundaries and a separately sourced WGS84 gazetteer.

Build-only dependency: Shapely 2.1.2. No runtime map API or API key is used.
Cached upstream bytes are checked against the reviewed snapshot hashes below.
GeoNames publishes daily files: preserve this cache for exact reproduction;
--refresh-geonames explicitly opts into reviewing a newer gazetteer snapshot.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.request
import zipfile
from collections import Counter
from pathlib import Path

COMMIT = "5822c4c0a0bdfd73327f9454976c8661bfd6ad9f"
SNAPSHOT = "2026-10-04"
BOUNDARY_SOURCE = f"Supeset/China-GeoData@{COMMIT}"
GEONAMES_SOURCE = "GeoNames (CC BY 4.0; https://www.geonames.org/)"
MIT_NOTICE = """MIT License

Copyright (c) 2025 圈集

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:
The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.
THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""
BOUNDARY_NOTE = (
    "上游未声明 GeoJSON 坐标系；原始经纬度形态坐标仅作离线展示，"
    "不得宣称 WGS84/GCJ-02 或据此写入 SRID 4326。边界已简化，"
    "与 GeoNames WGS84 参考点叠合仅为地区示意，不用于导航、测绘或精确空间判定。"
)
POINT_NOTE = (
    "GeoNames 导出文档明确经纬度为 WGS84；坐标为行政地区/城市参考点，"
    "不是岗位地址地理编码、企业总部或行政机关门址。未匹配的边界中心标为 unknown-display；"
    "只有逐记录 crs=WGS84 的点可用于 SRID 4326。缺少市级上级的 ADM3 仍是区县参考记录，"
    "不是城市导航条目；已确认历史名称单独标记。数据非行政区划现势性认证。"
)
HISTORICAL_REFERENCES = {
    "geonames:1809079": {
        "administrative_status": "historical_reference",
        "administrative_status_source": "https://www.zhanjiang.gov.cn/fileserver/news/634265c8-aa19-4de5-8235-9047dc15bea4.pdf",
        "administrative_status_note": "湛江市地方志记载1994年4月海康县撤县建雷州市；保留原GeoNames记录，不据此猜测或改写其市级上级。",
    },
}


def source_credits():
    return [
        {"name": BOUNDARY_SOURCE, "license": "MIT", "url": "https://github.com/Supeset/China-GeoData",
         "license_url": f"https://github.com/Supeset/China-GeoData/blob/{COMMIT}/LICENSE",
         "license_notice": MIT_NOTICE, "crs": "unknown-display",
         "changes": "Deduplicated overlapping source files; simplified polygon rings; normalized public properties"},
        {"name": "GeoNames", "attribution": GEONAMES_SOURCE, "license": "CC-BY-4.0",
         "url": "https://www.geonames.org/", "license_url": "https://creativecommons.org/licenses/by/4.0/",
         "crs": "WGS84", "changes": "Selected administrative/city reference points, aliases and source hierarchy; joined to boundary names"},
    ]
FILES = {
    "china_province_full.geojson": (
        f"https://raw.githubusercontent.com/Supeset/China-GeoData/{COMMIT}/geojson/china_province_full.geojson",
        "99adfeded5223848bbe37a0a12f8023e11ee12161c7800521c27db42fdeac275",
    ),
    "china_province_city_full.geojson": (
        f"https://raw.githubusercontent.com/Supeset/China-GeoData/{COMMIT}/geojson/china_province_city_full.geojson",
        "a268d74d3fba89bcc35f9febd8843a9cb77792a3690044dc8b26f21e8acf1bcd",
    ),
    "CN.zip": ("https://download.geonames.org/export/dump/CN.zip", "6e3bcd716a3c5a7e6e312e356fd717e841cba37ade0ed618333bdb6cdd41e6d6"),
    "HK.zip": ("https://download.geonames.org/export/dump/HK.zip", "ffd4389c2a280e960fc204de0cf5258fc97db2bf62031ce2cae0e4b11fe90efd"),
    "MO.zip": ("https://download.geonames.org/export/dump/MO.zip", "8f2cac1233bdb6af0e958e42aa19c5c24ea751bfe8a4a3bf313848de758cca58"),
    "TW.zip": ("https://download.geonames.org/export/dump/TW.zip", "c6e9544a3f0e73859d1c1266ac90a8240b73428a68cede9c0586458323dd39c8"),
}
# Source adcodes and GeoNames' HK ADM1 codes, matched by their published names.
HK_CODES = {
    "810001": "HCW", "810002": "HWC", "810003": "HEA", "810004": "HSO",
    "810005": "KYT", "810006": "KSS", "810007": "KKC", "810008": "KWT",
    "810009": "KKT", "810010": "NTW", "810011": "NTM", "810012": "NYL",
    "810013": "NNO", "810014": "NTP", "810015": "NSK", "810016": "NST",
    "810017": "NKT", "810018": "NIS",
}
COMMON_NAMES = {
    "110000": ["北京", "Beijing", "Beijing City", "Peking"],
    "120000": ["天津", "Tianjin"], "310000": ["上海", "Shanghai", "Shanghai City"],
    "500000": ["重庆", "Chongqing"], "440100": ["广州", "Guangzhou", "Canton"],
    "440300": ["深圳", "Shenzhen"], "330100": ["杭州", "Hangzhou", "Hangchow"],
    "510100": ["成都", "Chengdu"],
    "810000": ["香港", "香港特別行政區", "Hong Kong", "Hong Kong SAR", "HK"],
    "820000": ["澳门", "澳門", "澳門特別行政區", "Macau", "Macao"],
    "710000": ["台湾", "臺灣", "台灣", "Taiwan"],
}
# Published populated-place reference points for the six required cities.
CITY_REFERENCE_IDS = {
    "110000": "1816670", "310000": "1796236", "440100": "1809858",
    "440300": "1795565", "330100": "1808926", "510100": "1815286",
}
CN_SUFFIX = re.compile(r"(?:特别行政区|特別行政區|壮族自治区|維吾爾自治區|维吾尔自治区|回族自治区|自治区|自治區|省|市|区|區|县|縣|旗)$")
CHINESE = re.compile(r"^[\u3400-\u9fff·]+$")
ADMIN_END = re.compile(r"(?:自治区|自治區|自治州|地区|地區|省|市|区|區|县|縣|旗)$")


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"


def short_name(name):
    return CN_SUFFIX.sub("", name).casefold()


def aliases_for(row):
    values = [row["name"], row["ascii"]]
    # Retain published Chinese spellings and the primary English/transliterated
    # name, not every multilingual/historic alias from the convenience column.
    values += [v for v in row["aliases"] if len(v) > 1 and CHINESE.fullmatch(v)]
    if row["country"] == "HK":
        values += [v for v in row["aliases"] if v.endswith(" District")]
    return list(dict.fromkeys(v for v in values if v))


def read_sources(cache, refresh):
    cache.mkdir(parents=True, exist_ok=True)
    manifest = {}
    for name, (url, expected) in FILES.items():
        target = cache / name
        if not target.exists():
            print(f"Downloading {name}", flush=True)
            request = urllib.request.Request(url, headers={"User-Agent": "FrostFire-offline-geography-builder/1"})
            with urllib.request.urlopen(request, timeout=90) as response:
                target.write_bytes(response.read())
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        if digest != expected and not (refresh and name.endswith(".zip")):
            raise ValueError(f"Unreviewed {name} SHA256 {digest}; preserve the original source cache or explicitly review --refresh-geonames")
        manifest[name] = {"url": url, "sha256": digest}
    return manifest


def geonames_rows(cache):
    result = []
    for country in ("CN", "HK", "MO", "TW"):
        with zipfile.ZipFile(cache / f"{country}.zip") as archive:
            # Stream the large source; no raw 32 MB dump is shipped to clients.
            with archive.open(f"{country}.txt") as stream:
                for raw in stream:
                    fields = raw.decode("utf-8").rstrip("\n\r").split("\t")
                    if len(fields) != 19 or fields[7] not in {"ADM1", "ADM2", "ADM3", "PCLI", "PCLS", "PPLA", "PPLA2", "PPLC"}:
                        continue
                    result.append({"id": fields[0], "name": fields[1], "ascii": fields[2],
                                   "aliases": fields[3].split(","), "latitude": float(fields[4]),
                                   "longitude": float(fields[5]), "code": fields[7], "country": fields[8],
                                   "admin1": fields[10], "admin2": fields[11], "admin3": fields[12],
                                   "modified": fields[18]})
    return result


def source_features(cache):
    features = {}
    for name in ("china_province_full.geojson", "china_province_city_full.geojson"):
        for feature in json.loads((cache / name).read_text(encoding="utf-8"))["features"]:
            key = str(feature["properties"]["adcode"])
            # Taiwan and the South China Sea auxiliary are present in both.
            features.setdefault(key, feature)
    return features


def boundary_properties(features):
    provinces = {key: value["properties"]["name"] for key, value in features.items()
                 if value["properties"].get("level") == "province"}
    result = {}
    municipalities = {"110000", "120000", "310000", "500000", "810000", "820000"}
    for key, feature in features.items():
        source = feature["properties"]
        level = source.get("level") or "auxiliary"
        province_id = key if level == "province" else key[:2] + "0000"
        if level == "auxiliary":
            province_id = None
        parent = str(source.get("parent", {}).get("adcode", "100000"))
        city_id = key if level == "city" else province_id if province_id in municipalities else None
        if level == "district" and parent in features and features[parent]["properties"].get("level") == "city":
            city_id = parent
        result[key] = {"id": key, "name": source.get("name", ""), "level": level,
                       "parent_id": parent, "province_id": province_id,
                       "province_name": provinces.get(province_id), "city_id": city_id,
                       "city_name": features[city_id]["properties"]["name"] if city_id else None,
                       "center": source.get("center"), "source": BOUNDARY_SOURCE,
                       "crs": "unknown-display", "coordinate_precision": "source_display_reference"}
    return result


def simplify_geometry(geometry, tolerance):
    from shapely.geometry import Polygon, mapping
    if geometry["type"] not in {"Polygon", "MultiPolygon"}:
        raise ValueError("Only source Polygon/MultiPolygon boundaries are supported")
    parts = geometry["coordinates"] if geometry["type"] == "MultiPolygon" else [geometry["coordinates"]]
    output = []
    for part in parts:
        # Simplify components independently: never merge/drop offshore islands
        # or remove a source hole, even when an upstream MultiPolygon overlaps.
        polygon = Polygon(part[0], part[1:])
        candidate = mapping(polygon.simplify(tolerance, preserve_topology=True))
        rings = candidate["coordinates"] if candidate["type"] == "Polygon" else part
        if len(rings) != len(part):
            rings = part
        rounded = [[[round(float(x), 6), round(float(y), 6)] for x, y in ring] for ring in rings]
        if any(len(set(map(tuple, ring))) < 3 for ring in rounded):
            rounded = part
        output.append(rounded)
    return {"type": geometry["type"], "coordinates": output if geometry["type"] == "MultiPolygon" else output[0]}


def build_boundaries(features, properties, manifest):
    from shapely.geometry import shape
    output = []
    for key, feature in features.items():
        props = dict(properties[key])
        # Preserve the auxiliary South China Sea source geometry byte-for-value.
        geometry = feature["geometry"] if props["level"] == "auxiliary" else simplify_geometry(
            feature["geometry"], 0.0003 if key.startswith(("81", "82")) else 0.03)
        if props["center"] is None:
            bounds = shape(feature["geometry"]).bounds
            props["center"] = [round((bounds[0] + bounds[2]) / 2, 6), round((bounds[1] + bounds[3]) / 2, 6)]
            props["coordinate_precision"] = "source_geometry_bbox_midpoint_display_only"
        output.append({"type": "Feature", "properties": props, "geometry": geometry})
    return {"type": "FeatureCollection", "schema_version": 1, "source": BOUNDARY_SOURCE,
            "crs": "unknown-display", "coordinate_note": BOUNDARY_NOTE,
            "license": "MIT", "license_url": f"https://github.com/Supeset/China-GeoData/blob/{COMMIT}/LICENSE",
            "license_notice": MIT_NOTICE, "source_snapshot": SNAPSHOT,
            "source_files": {name: item for name, item in manifest.items() if name.endswith(".geojson")},
            "coverage": {"polygon_counts": dict(Counter(p["properties"]["level"] for p in output)),
                         "district_boundary_coverage": "partial", "hong_kong_district_polygons": 18,
                         "mainland_district_polygons": "Direct municipalities only; ordinary provincial districts require point-only drilldown",
                         "source_level_note": "Source city level includes prefectures and some directly administered county-level units",
                         "south_china_sea_auxiliary_preserved": True}, "features": output}


def apply_point(place, row):
    place.update({"longitude": row["longitude"], "latitude": row["latitude"],
                  "center": [row["longitude"], row["latitude"]], "crs": "WGS84",
                  "source": GEONAMES_SOURCE, "coordinate_source": f"https://www.geonames.org/{row['id']}/",
                  "coordinate_precision": "administrative_reference_point", "geonames_id": row["id"],
                  "geonames_feature_code": row["code"], "source_modified": row["modified"]})
    place["aliases"] = list(dict.fromkeys(place["aliases"] + aliases_for(row)))


def names_match(place, row):
    targets = {short_name(place["name"])}
    return bool(targets & {short_name(v) for v in [row["name"], *row["aliases"]] if len(short_name(v)) > 1})


def preferred_chinese(row):
    names = [v for v in row["aliases"] if CHINESE.fullmatch(v) and ADMIN_END.search(v)]
    return next((v for v in names if v.endswith(("市", "县", "区", "縣", "區", "自治州"))), names[0] if names else row["name"])


def admin2_boundary_target(places, row, province_id):
    """Join a published mainland ADM2 code only to an existing same-province city.

    The reviewed CN snapshot uses four-digit administrative codes for ordinary
    prefectures. Names can contain historic aliases (Xiangfan / Xiangyang), so
    the exact code is safer than selecting the first Chinese convenience alias.
    Municipalities/direct-admin units and other countries retain their existing
    explicit name matching; arbitrary GeoNames IDs are never treated as adcodes.
    """
    code = row["admin2"]
    if row["country"] != "CN" or row["code"] != "ADM2" or not re.fullmatch(r"[0-9]{4}", code):
        return None
    place = places.get(code + "00")
    if place and place["level"] == "city" and place["province_id"] == province_id:
        return place
    return None


def adm3_boundary_compatible(place):
    # ADM3 can describe a source district or a province-direct county-level
    # unit that the polygon source puts at its city navigation level. It must
    # never supply a prefecture city's point/aliases merely because 市/区/县
    # suffix stripping made two names equal (e.g. 襄阳市 versus old 襄阳区).
    identity = place["id"]
    direct_county = (place["level"] == "city" and re.fullmatch(r"[0-9]{6}", identity)
                     and not identity.endswith("00") and place["parent_id"] == place["province_id"])
    return place["level"] == "district" or bool(direct_county)


def build_places(properties, rows, manifest):
    places = {}
    for key, props in properties.items():
        if props["level"] == "auxiliary":
            continue
        place = dict(props)
        place["aliases"] = list(dict.fromkeys([props["name"], CN_SUFFIX.sub("", props["name"]), *COMMON_NAMES.get(key, [])]))
        center = props["center"] or [None, None]
        place.update({"longitude": center[0], "latitude": center[1], "coordinate_source": BOUNDARY_SOURCE,
                      "coordinate_precision": "unverified_source_display_reference"})
        places[key] = place
    used = {}
    cn_provinces = {}
    # The 31 mainland ADM1 rows have unambiguous published Chinese names.
    for row in rows:
        if row["country"] == "CN" and row["code"] == "ADM1":
            matches = [p for p in places.values() if p["level"] == "province" and names_match(p, row)]
            if len(matches) != 1:
                raise ValueError(f"Ambiguous province {row['name']}")
            place = matches[0]
            apply_point(place, row)
            used[row["id"]] = place["id"]
            cn_provinces[row["admin1"]] = place["id"]
    for country, key, feature in (("HK", "810000", "PCLS"), ("MO", "820000", "PCLS"), ("TW", "710000", "PCLI")):
        row = next(r for r in rows if r["country"] == country and r["code"] == feature)
        apply_point(places[key], row)
        used[row["id"]] = key
    for key, code in HK_CODES.items():
        row = next(r for r in rows if r["country"] == "HK" and r["code"] == "ADM1" and r["admin1"] == code)
        apply_point(places[key], row)
        used[row["id"]] = key
        # Keep the published English district suffix even for short source names.
        if row["name"].endswith("District") is False:
            places[key]["aliases"].append(row["name"] + " District")
    admin2 = {}
    admin_rows = [r for r in rows if r["country"] in {"CN", "TW"} and r["code"] in {"ADM2", "ADM3"}]
    for code in ("ADM2", "ADM3"):
        for row in (r for r in admin_rows if r["code"] == code):
            province_id = cn_provinces.get(row["admin1"]) if row["country"] == "CN" else "710000"
            if not province_id:
                continue
            parent_id = province_id if code == "ADM2" else admin2.get((row["country"], row["admin1"], row["admin2"]), province_id)
            candidates = [p for p in places.values() if p["province_id"] == province_id
                          and p["crs"] != "WGS84" and names_match(p, row)
                          and (adm3_boundary_compatible(p) if code == "ADM3" else p["level"] in {"city", "province"})]
            code_target = admin2_boundary_target(places, row, province_id)
            if code_target:
                candidates = [code_target]
            # Municipality ADM2 describes the same named place already represented
            # by its province code; avoid a second Beijing/Shanghai alias entity.
            if code == "ADM2" and places[province_id].get("city_id") == province_id and names_match(places[province_id], row):
                candidates = [places[province_id]]
            if len(candidates) == 1:
                place = candidates[0]
            else:
                key = "geonames:" + row["id"]
                city_id = key if code == "ADM2" else parent_id if places[parent_id]["level"] == "city" or places[parent_id].get("city_id") == parent_id else None
                place = {"id": key, "name": preferred_chinese(row), "level": "city" if code == "ADM2" else "district",
                         "parent_id": parent_id, "province_id": province_id, "province_name": places[province_id]["name"],
                         "city_id": city_id, "city_name": places[parent_id]["name"] if code == "ADM3" and city_id else None,
                         "aliases": []}
                if code == "ADM2":
                    place["city_name"] = place["name"]
                places[key] = place
            apply_point(place, row)
            used[row["id"]] = place["id"]
            if code == "ADM2":
                admin2[(row["country"], row["admin1"], row["admin2"])] = place["id"]
    for row in (r for r in rows if r["country"] == "MO" and r["code"] == "ADM1"):
        candidates = [p for p in places.values() if p["province_id"] == "820000" and p["level"] == "district" and names_match(p, row)]
        if len(candidates) == 1:
            apply_point(candidates[0], row)
    by_id = {row["id"]: row for row in rows}
    for key, geonames_id in CITY_REFERENCE_IDS.items():
        apply_point(places[key], by_id[geonames_id])
        places[key]["coordinate_precision"] = "city_reference_point"
    for place in places.values():
        place["aliases"] = list(dict.fromkeys([place["name"], *place["aliases"], *COMMON_NAMES.get(place["id"], [])]))
        if place["level"] == "district" and not place["city_id"]:
            # A known province does not establish a missing intermediate city.
            # Keep the source row for audit/resolution, but do not advertise it
            # as a current city/district navigation entry at the province tier.
            place.update(hierarchy_status="province_only_unverified", navigation_visible=False)
        if place["id"] in HISTORICAL_REFERENCES:
            place.update(HISTORICAL_REFERENCES[place["id"]])
    values = sorted(places.values(), key=lambda p: (p["province_id"], {"province": 0, "city": 1, "district": 2}[p["level"]], p["id"]))
    return {"schema_version": 1, "source": "Offline boundary names plus separately verified GeoNames reference points",
            "sources": source_credits(),
            "crs": "mixed-per-place", "coordinate_note": POINT_NOTE, "source_snapshot": SNAPSHOT,
            "source_files": manifest, "coverage": {"place_counts": dict(Counter(p["level"] for p in values)),
                "coordinate_crs_counts": dict(Counter(p["crs"] for p in values)),
                "mainland_geonames_admin2_rows": 360, "mainland_geonames_admin3_rows": 2938,
                "hong_kong_districts": 18, "district_boundaries": "partial; reference points do not imply polygons",
                "province_only_district_references": sum(p.get("navigation_visible") is False for p in values),
                "reviewed_historical_references": len(HISTORICAL_REFERENCES),
                "freshness": "Source snapshots, not a complete/current authoritative administrative-register certification"},
            "places": values}


def build_public_centers(catalog):
    """Lazy-loadable public navigation index, with no job/account data or aliases."""
    fields = ("id", "name", "level", "parent_id", "province_id", "city_id", "center", "crs")
    places = []
    for place in catalog["places"]:
        row = {field: place[field] for field in fields}
        row["source"] = "GeoNames" if place["crs"] == "WGS84" else BOUNDARY_SOURCE
        for field in ("hierarchy_status", "navigation_visible", "administrative_status"):
            if field in place:
                row[field] = place[field]
        places.append(row)
    return {key: catalog[key] for key in ("schema_version", "source", "sources", "crs", "coordinate_note",
                                         "source_snapshot", "source_files", "coverage")} | {"places": places}


def validate(boundaries, catalog, originals):
    features = {f["properties"]["id"]: f for f in boundaries["features"]}
    places = {p["id"]: p for p in catalog["places"]}
    assert len(features) == 510 and len(places) == len(catalog["places"])
    assert boundaries["coverage"]["polygon_counts"] == {"province": 34, "city": 363, "district": 112, "auxiliary": 1}
    for key in ("710000", "810000", "820000", "100000_JD"):
        assert key in features
    assert features["100000_JD"]["geometry"] == originals["100000_JD"]["geometry"]
    for key, feature in features.items():
        geometry = feature["geometry"]
        source = originals[key]["geometry"]
        parts = geometry["coordinates"] if geometry["type"] == "MultiPolygon" else [geometry["coordinates"]]
        original_parts = source["coordinates"] if source["type"] == "MultiPolygon" else [source["coordinates"]]
        assert len(parts) == len(original_parts) and [len(p) for p in parts] == [len(p) for p in original_parts]
        for part in parts:
            for ring in part:
                assert len(ring) >= 4 and ring[0] == ring[-1]
                assert all(-180 <= point[0] <= 180 and -90 <= point[1] <= 90 for point in ring)
    for key in [*CITY_REFERENCE_IDS, *HK_CODES]:
        assert places[key]["crs"] == "WGS84" and places[key]["coordinate_source"].startswith("https://www.geonames.org/")
    for place in places.values():
        assert place["level"] == "province" or place["parent_id"] in places
        assert place["province_id"] in places
        if place["city_id"]:
            assert place["city_id"] in places
        assert place["longitude"] is not None and place["latitude"] is not None
        assert -180 <= place["longitude"] <= 180 and -90 <= place["latitude"] <= 90
        assert place["source"] and place["aliases"]
    assert places["420600"]["geonames_id"] == "1790585" and places["420600"]["geonames_feature_code"] == "ADM2"
    assert "geonames:1790585" not in places
    assert places["geonames:1790456"]["level"] == "district" and places["geonames:1790456"]["parent_id"] == "420600"
    assert places["geonames:1809079"]["level"] == "district" and places["geonames:1809079"]["city_id"] is None
    assert places["geonames:1809079"]["navigation_visible"] is False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-cache", type=Path, required=True)
    parser.add_argument("--shapely-lib", type=Path, help="Optional build-only pip --target installation")
    parser.add_argument("--refresh-geonames", action="store_true")
    parser.add_argument("--check", action="store_true", help="Compare generated bytes without rewriting assets")
    args = parser.parse_args()
    if args.shapely_lib:
        sys.path.insert(0, str(args.shapely_lib))
    import shapely
    if shapely.__version__ != "2.1.2":
        raise ValueError("Use build-only Shapely 2.1.2 to reproduce the reviewed polygon simplification")
    manifest = read_sources(args.source_cache, args.refresh_geonames)
    originals = source_features(args.source_cache)
    properties = boundary_properties(originals)
    boundaries = build_boundaries(originals, properties, manifest)
    catalog = build_places(properties, geonames_rows(args.source_cache), manifest)
    validate(boundaries, catalog, originals)
    root = Path(__file__).resolve().parents[1]
    outputs = {root / "frontend/src/data/china-boundaries.json": boundaries,
               root / "backend/future_radar/data/china_places.json": catalog,
               root / "frontend/src/data/china-admin-centers.json": build_public_centers(catalog)}
    for path, payload in outputs.items():
        data = compact(payload).encode("utf-8")
        if args.check:
            assert path.read_bytes() == data, f"{path} is not byte-for-byte reproducible"
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        print(f"{path.relative_to(root)}: {len(data):,} bytes", flush=True)
    print(json.dumps({"boundaries": boundaries["coverage"], "catalog": catalog["coverage"]}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
