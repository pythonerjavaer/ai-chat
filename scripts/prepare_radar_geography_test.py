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
            self.assertEqual(set(row), expected_fields)
            self.assertEqual(row["center"], self.places[row["id"]]["center"])
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


if __name__ == "__main__":
    unittest.main()
