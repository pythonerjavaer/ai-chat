"""Offline asset contract tests: no network, database, account or source cache."""
import json
import unittest
from collections import Counter
from pathlib import Path

import prepare_radar_geography as builder


ROOT = Path(__file__).resolve().parents[1]


class GeographyAssetsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.boundary_path = ROOT / "frontend/src/data/china-boundaries.json"
        cls.center_path = ROOT / "frontend/src/data/china-admin-centers.json"
        cls.boundaries = json.loads(cls.boundary_path.read_text(encoding="utf-8"))
        cls.catalog = json.loads((ROOT / "backend/future_radar/data/china_places.json").read_text(encoding="utf-8"))
        cls.centers = json.loads(cls.center_path.read_text(encoding="utf-8"))
        cls.features = {f["properties"]["id"]: f for f in cls.boundaries["features"]}
        cls.places = {p["id"]: p for p in cls.catalog["places"]}

    def test_polygon_coverage_and_non_place_auxiliary(self):
        self.assertEqual(len(self.features), 510)
        self.assertEqual(Counter(f["properties"]["level"] for f in self.features.values()),
                         {"province": 34, "city": 363, "district": 112, "auxiliary": 1})
        self.assertEqual(self.features["100000_JD"]["properties"]["level"], "auxiliary")
        self.assertNotIn("100000_JD", self.places)
        self.assertTrue(self.boundaries["coverage"]["south_china_sea_auxiliary_preserved"])
        for key in ("710000", "810000", "820000"):
            self.assertEqual(self.features[key]["properties"]["level"], "province")

    def test_polygon_rings_are_closed_and_source_crs_not_assumed(self):
        self.assertEqual(self.boundaries["crs"], "unknown-display")
        for feature in self.features.values():
            self.assertEqual(feature["properties"]["crs"], "unknown-display")
            geometry = feature["geometry"]
            self.assertIn(geometry["type"], {"Polygon", "MultiPolygon"})
            parts = geometry["coordinates"] if geometry["type"] == "MultiPolygon" else [geometry["coordinates"]]
            for part in parts:
                for ring in part:
                    self.assertGreaterEqual(len(ring), 4)
                    self.assertEqual(ring[0], ring[-1])
                    self.assertGreaterEqual(len(set(map(tuple, ring))), 3)
                    for longitude, latitude in ring:
                        self.assertTrue(-180 <= longitude <= 180 and -90 <= latitude <= 90)

    def test_required_cities_and_hong_kong_have_wgs84_provenance_and_aliases(self):
        required = {"110000": "Beijing", "310000": "Shanghai", "440300": "Shenzhen",
                    "440100": "Guangzhou", "330100": "Hangzhou", "510100": "Chengdu"}
        for identity, alias in required.items():
            place = self.places[identity]
            self.assertIn(alias, place["aliases"])
            self.assertEqual(place["crs"], "WGS84")
            self.assertEqual(place["geonames_id"], builder.CITY_REFERENCE_IDS[identity])
            self.assertEqual(place["coordinate_source"], f"https://www.geonames.org/{place['geonames_id']}/")
        self.assertIn("Hong Kong", self.places["810000"]["aliases"])
        for identity in builder.HK_CODES:
            place = self.places[identity]
            self.assertEqual(place["level"], "district")
            self.assertEqual(place["province_id"], "810000")
            self.assertEqual(place["parent_id"], "810000")
            self.assertEqual(place["crs"], "WGS84")
            self.assertEqual(place["geonames_feature_code"], "ADM1")
            self.assertTrue(any(alias.endswith("District") for alias in place["aliases"]))
            self.assertEqual(self.features[identity]["properties"]["level"], "district")

    def test_catalog_ids_hierarchy_and_per_record_coordinate_contract(self):
        self.assertEqual(len(self.places), len(self.catalog["places"]))
        self.assertEqual(len(self.places), 3752)
        counts = Counter(place["crs"] for place in self.places.values())
        self.assertEqual(counts, {"WGS84": 3739, "unknown-display": 13})
        self.assertEqual(self.catalog["crs"], "mixed-per-place")
        for place in self.places.values():
            self.assertEqual(place["center"], [place["longitude"], place["latitude"]])
            self.assertIn(place["province_id"], self.places)
            if place["level"] != "province":
                self.assertIn(place["parent_id"], self.places)
            if place["city_id"]:
                self.assertIn(place["city_id"], self.places)
            if place["crs"] == "WGS84":
                self.assertIn("GeoNames", place["source"])
                self.assertTrue(place["coordinate_source"].startswith("https://www.geonames.org/"))
            else:
                self.assertEqual(place["coordinate_precision"], "unverified_source_display_reference")
                self.assertNotIn("geonames_id", place)

    def test_minimal_frontend_directory_matches_backend_without_private_fields(self):
        self.assertEqual(self.centers, builder.build_public_centers(self.catalog))
        expected_fields = {"id", "name", "level", "parent_id", "province_id", "city_id", "center", "crs", "source"}
        for row in self.centers["places"]:
            optional_fields = {"hierarchy_status", "navigation_visible", "administrative_status"}
            self.assertTrue(expected_fields <= set(row) <= expected_fields | optional_fields)
            self.assertEqual(row["center"], self.places[row["id"]]["center"])
            for field in optional_fields:
                self.assertEqual(row.get(field), self.places[row["id"]].get(field))
        self.assertLess(self.center_path.stat().st_size, 1_000_000)
        self.assertLess(self.boundary_path.stat().st_size + self.center_path.stat().st_size, 2_000_000)

    def test_embedded_license_credits_and_snapshot_hashes(self):
        self.assertEqual(self.boundaries["license_notice"], builder.MIT_NOTICE)
        self.assertIn("Copyright (c) 2025 圈集", self.boundaries["license_notice"])
        for asset in (self.catalog, self.centers):
            boundary_source, point_source = asset["sources"]
            self.assertEqual(boundary_source["license_notice"], builder.MIT_NOTICE)
            self.assertEqual(point_source["license"], "CC-BY-4.0")
            self.assertEqual(point_source["url"], "https://www.geonames.org/")
            self.assertEqual(point_source["license_url"], "https://creativecommons.org/licenses/by/4.0/")
            self.assertTrue(point_source["changes"])
            for name, (url, digest) in builder.FILES.items():
                self.assertEqual(asset["source_files"][name], {"url": url, "sha256": digest})
        self.assertEqual(self.boundaries["coverage"]["district_boundary_coverage"], "partial")
        self.assertIn("not a complete/current authoritative", self.catalog["coverage"]["freshness"])

    def test_xiangyang_city_and_xiangzhou_district_are_not_suffix_conflated(self):
        city = self.places["420600"]
        self.assertEqual(city["name"], "襄阳市")
        self.assertEqual(city["level"], "city")
        self.assertEqual(city["geonames_id"], "1790585")
        self.assertEqual(city["geonames_feature_code"], "ADM2")
        self.assertIn("Xiangyang", city["aliases"])
        self.assertNotIn("襄州区", city["aliases"])
        self.assertNotIn("襄阳区", city["aliases"])
        self.assertNotIn("geonames:1790585", self.places)
        self.assertFalse(any(place["name"] == "襄樊市" and place["level"] == "city" for place in self.places.values()))
        district = self.places["geonames:1790456"]
        self.assertEqual(district["name"], "襄州区")
        self.assertEqual(district["level"], "district")
        self.assertEqual(district["geonames_feature_code"], "ADM3")
        self.assertEqual(district["parent_id"], "420600")
        self.assertEqual(district["city_id"], "420600")
        self.assertNotEqual(city["center"], district["center"])

    def test_orphan_haikang_remains_a_sourced_district_but_not_navigation_city(self):
        place = self.places["geonames:1809079"]
        self.assertEqual(place["name"], "海康县")
        self.assertEqual(place["level"], "district")
        self.assertEqual(place["geonames_feature_code"], "ADM3")
        self.assertEqual(place["parent_id"], "440000")
        self.assertIsNone(place["city_id"])
        self.assertEqual(place["coordinate_source"], "https://www.geonames.org/1809079/")
        self.assertEqual(place["source_modified"], "2010-08-09")
        self.assertEqual(place["hierarchy_status"], "province_only_unverified")
        self.assertIs(place["navigation_visible"], False)
        self.assertEqual(place["administrative_status"], "historical_reference")
        self.assertTrue(place["administrative_status_source"].startswith("https://www.zhanjiang.gov.cn/"))
        for district in self.places.values():
            if district["level"] == "district" and not district["city_id"]:
                self.assertIs(district["navigation_visible"], False)
                self.assertEqual(district["hierarchy_status"], "province_only_unverified")
        self.assertEqual(self.catalog["coverage"]["province_only_district_references"], 73)

    def test_adm_matching_checks_code_province_and_administrative_level(self):
        city = {"id": "420600", "name": "襄阳市", "level": "city", "parent_id": "420000", "province_id": "420000"}
        row = {"country": "CN", "code": "ADM2", "admin2": "4206"}
        self.assertIs(builder.admin2_boundary_target({city["id"]: city}, row, "420000"), city)
        self.assertIsNone(builder.admin2_boundary_target({city["id"]: city}, row, "440000"))
        self.assertIsNone(builder.admin2_boundary_target({city["id"]: city}, dict(row, admin2="1790585"), "420000"))
        self.assertIsNone(builder.admin2_boundary_target({city["id"]: city}, dict(row, code="ADM3"), "420000"))
        self.assertFalse(builder.adm3_boundary_compatible(city))
        self.assertTrue(builder.adm3_boundary_compatible(dict(city, id="420607", level="district")))
        self.assertTrue(builder.adm3_boundary_compatible({"id": "659002", "name": "阿拉尔市", "level": "city", "parent_id": "650000", "province_id": "650000"}))

    def test_real_parser_uses_separate_xiangyang_and_xiangzhou_references(self):
        from backend.future_radar.geography import ChinaPlaceCatalog

        catalog = ChinaPlaceCatalog(self.catalog)
        for text, identity, level in (
            ("襄阳市", "420600", "city"), ("Xiangyang", "420600", "city"),
            ("襄樊市", "420600", "city"),
            ("襄州区", "geonames:1790456", "district"),
            ("襄阳市襄州区", "geonames:1790456", "district"),
        ):
            places, status = catalog.resolve(text)
            self.assertEqual(status, "mapped", text)
            self.assertEqual([(place["id"], place["level"]) for place in places], [(identity, level)], text)

    def test_published_chinese_names_keep_english_aliases_and_coordinate_provenance(self):
        reviews = json.loads((ROOT / "scripts/data/china-name-reviews.json").read_text(encoding="utf-8"))
        for review in reviews["places"]:
            place = self.places["geonames:" + review["geonames_id"]]
            self.assertEqual(place["name"], review["name"])
            self.assertIn(review["source_name"], place["aliases"])
            self.assertEqual(place["name_source"], review["source"])
            self.assertEqual(place["province_id"], review["province_id"])
            self.assertEqual(place["parent_id"], review["parent_id"])
            self.assertEqual(place["coordinate_source"], f"https://www.geonames.org/{review['geonames_id']}/")
        self.assertEqual(self.places["geonames:12746883"]["center"], [116.5433, 29.89635])
        visible = [p for p in self.places.values() if p.get("navigation_visible") is not False]
        self.assertFalse([p["name"] for p in visible if not builder.CHINESE.fullmatch(p["name"])])

    def test_chinese_township_aliases_are_not_discarded_or_invented_counties(self):
        for name in ("北竿鄉", "金城鎮", "昭平镇", "冷湖行委", "江口镇"):
            self.assertEqual(builder.preferred_chinese({"name": "English reference", "aliases": [name]}), name)
        self.assertEqual(builder.preferred_chinese({"name": "Lishi District", "aliases": ["离石"]}), "离石")
        self.assertEqual(builder.preferred_chinese({"name": "Unverified County", "aliases": []}), "Unverified County")

    def test_reviewed_historical_and_conflicting_rows_do_not_locate_current_jobs(self):
        from backend.future_radar.geography import ChinaPlaceCatalog

        catalog = ChinaPlaceCatalog(self.catalog)
        for identity in ("1806069", "1788532", "1796133", "11288134"):
            place = self.places["geonames:" + identity]
            self.assertIs(place["navigation_visible"], False)
            self.assertIs(place["matching_enabled"], False)
            self.assertIsNotNone(catalog.place(place["id"]), "Source row remains auditable, not deleted")
            self.assertNotIn(place["id"], {p["id"] for p in catalog.resolve(place["name"])[0]})
        conflict = self.places["geonames:11288134"]
        self.assertEqual(conflict["parent_id"], "530900")
        self.assertEqual(conflict["hierarchy_status"], "published_parent_conflict")

    def test_pengze_chinese_english_and_parent_city_resolve_same_id(self):
        from backend.future_radar.geography import ChinaPlaceCatalog

        catalog = ChinaPlaceCatalog(self.catalog)
        for name in ("彭泽县", "江西省九江市彭泽县", "Pengze County"):
            places, status = catalog.resolve(name)
            self.assertEqual(status, "mapped", name)
            self.assertEqual([p["id"] for p in places], ["geonames:12746883"], name)
            self.assertEqual(places[0]["name"], "彭泽县")
        places, status = catalog.resolve("湖北省彭泽县")
        self.assertEqual(status, "ambiguous")
        self.assertFalse(places)


if __name__ == "__main__":
    unittest.main()
