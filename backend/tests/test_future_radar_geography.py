"""Synthetic administrative catalog only; no geocoder, account or cloud DB."""

import pytest

from backend.future_radar.geography import (
    ChinaPlaceCatalog, china_place_catalog, geography_projection, resolve_opportunity_places,
)


def synthetic_catalog():
    rows = []

    def add(identity, name, level, *, province=None, city=None, parent=None, aliases=(), crs="WGS84"):
        rows.append({"id": identity, "name": name, "level": level, "parent_id": parent,
                     "province_id": province or identity, "province_name": "公开省级名称",
                     "city_id": city or (identity if level == "city" else None), "city_name": "公开市级名称",
                     "longitude": 114.0, "latitude": 23.0, "crs": crs, "aliases": list(aliases),
                     "source": "synthetic-public-catalog", "coordinate_source": "synthetic-reference"})

    add("110000", "北京市", "province", aliases=("北京", "Beijing"))
    add("110100", "北京市", "city", province="110000", parent="110000", aliases=("北京", "Beijing"))
    add("110105", "朝阳区", "district", province="110000", city="110100", parent="110100")
    add("310000", "上海市", "province", aliases=("上海", "Shanghai"))
    add("310100", "上海市", "city", province="310000", parent="310000", aliases=("上海", "Shanghai"))
    add("440000", "广东省", "province", aliases=("广东", "Guangdong"))
    add("440300", "深圳市", "city", province="440000", parent="440000", aliases=("深圳", "Shenzhen"))
    add("440305", "南山区", "district", province="440000", city="440300", parent="440300", aliases=("Nanshan District",))
    add("440100", "广州市", "city", province="440000", parent="440000", aliases=("广州", "Guangzhou"))
    add("440106", "天河区", "district", province="440000", city="440100", parent="440100")
    add("330000", "浙江省", "province", aliases=("浙江",))
    add("330100", "杭州市", "city", province="330000", parent="330000", aliases=("杭州", "Hangzhou"))
    add("220000", "吉林省", "province", aliases=("吉林",))
    add("220100", "长春市", "city", province="220000", parent="220000", aliases=("长春",))
    add("220104", "朝阳区", "district", province="220000", city="220100", parent="220100")
    add("810000", "香港特别行政区", "province", aliases=("香港", "Hong Kong", "HK"))
    add("810001", "中西区", "district", province="810000", parent="810000", aliases=("Central and Western",))
    add("440399", "公开未知坐标区", "district", province="440000", city="440300", parent="440300", crs="unknown-display")
    return ChinaPlaceCatalog({"places": rows, "coordinate_note": "合成行政参考点，非办公地址。"})


@pytest.mark.parametrize("value, expected", [
    ("北京", ["110100"]), ("北京市", ["110100"]), ("Ｂｅｉｊｉｎｇ", ["110100"]),
    ("Shanghai", ["310100"]), ("香港", ["810000"]), ("Hong Kong", ["810000"]),
    ("广东省", ["440000"]), ("广东省深圳市南山区", ["440305"]),
    ("深圳市南山区科技路1号", ["440305"]), ("北京市朝阳区", ["110105"]),
    ("吉林省长春市朝阳区", ["220104"]), ("工作地点：广东省深圳市", ["440300"]),
    ("北京/上海/Hong Kong", ["110100", "310100", "810000"]),
    ("Beijing and Shanghai", ["110100", "310100"]),
    (["深圳", "深圳市", "广州"], ["440300", "440100"]),
    ("Central and Western, Hong Kong", ["810001"]),
])
def test_explicit_chinese_english_hierarchical_and_multi_place_values(value, expected):
    places, status = synthetic_catalog().resolve(value)
    assert [place["id"] for place in places] == expected
    assert status == "mapped"
    assert all(place["accuracy"] == "administrative_center" and place["source"] == "city" for place in places)


@pytest.mark.parametrize("value, status", [
    ("朝阳区", "ambiguous"), ("广东省杭州市", "ambiguous"),
    ("深圳市天河区", "ambiguous"), ("全国", "unmapped"), ("远程", "unmapped"),
    ("Remote / nationwide", "unmapped"), ("香港（远程）", "unmapped"),
    ("长三角", "unmapped"), ("华南", "unmapped"), ("不限城市", "unmapped"),
    ("待确认", "unmapped"), ("支持上海客户的深圳业务", "unmapped"),
    ("南京西路", "unmapped"), ("Beijinger", "unmapped"),
    ({"private": "北京"}, "unmapped"), (None, "unmapped"), (123, "unmapped"),
])
def test_ambiguous_remote_vague_and_non_location_values_never_get_guessed(value, status):
    places, actual = synthetic_catalog().resolve(value)
    assert places == []
    assert actual == status


def test_only_public_location_fields_supply_places_and_location_takes_precedence():
    catalog = synthetic_catalog()
    places, status = resolve_opportunity_places({
        "company": "北京总部", "description": "面向上海客户，支持香港项目。",
        "requirements": "深圳团队", "private_profile": {"city": "杭州"},
    }, catalog=catalog)
    assert places == [] and status == "unmapped"
    places, status = resolve_opportunity_places({"city": "上海", "location": "Hong Kong"}, catalog=catalog)
    assert places[0]["id"] == "810000" and places[0]["source"] == "location" and status == "mapped"
    assert resolve_opportunity_places({"city": "北京", "location": "remote"}, catalog=catalog) == ([], "unmapped")


def test_places_and_hierarchy_are_independent_catalog_allowlisted_copies():
    catalog = synthetic_catalog()
    places, _status = catalog.resolve("深圳市南山区")
    locations, links = catalog.hierarchy(places)
    assert [place["id"] for place in locations] == ["440305", "440300", "440000"]
    assert links == [{"child": "440305", "parent": "440300"}, {"child": "440300", "parent": "440000"}]
    places[0]["name"] = "client mutation"
    places[0]["longitude"] = 0
    places[0]["private"] = "PRIVATE_SENTINEL"
    again, _status = catalog.resolve("深圳市南山区")
    assert again[0]["name"] == "南山区" and again[0]["longitude"] == 114.0
    fresh, _links = catalog.hierarchy(places)
    assert "PRIVATE_SENTINEL" not in repr(fresh)
    assert fresh[0]["name"] == "南山区"


def test_geojson_aggregates_unique_current_jobs_at_leaf_and_parent_without_fake_district_points():
    catalog = synthetic_catalog()
    items = []
    for identity, city in (("one", "深圳市南山区"), ("two", "深圳"),
                           ("three", "公开未知坐标区"), ("remote", "全国")):
        places, status = catalog.resolve(city)
        items.append({"id": identity, "places": places, "location_status": status,
                      "private_profile": "PRIVATE_SENTINEL"})
    result = geography_projection(items, catalog=catalog)
    assert result["matched_opportunities"] == 3 and result["unmapped_opportunities"] == 1
    assert result["unplottable_places"] == 1 and result["scope"] == "current_public_projection"
    features = {feature["id"]: feature for feature in result["features"]["features"]}
    assert set(features) == {"440305", "440300", "440000"}
    assert features["440305"]["geometry"] == {"type": "Point", "coordinates": [114.0, 23.0]}
    assert features["440305"]["properties"]["opportunity_ids"] == ["one"]
    assert features["440300"]["properties"]["opportunity_ids"] == ["one", "three", "two"]
    assert features["440300"]["properties"]["direct_opportunity_ids"] == ["two"]
    assert features["440000"]["properties"]["opportunity_count"] == 3
    assert "PRIVATE_SENTINEL" not in repr(result)


def test_invalid_nonfinite_or_unverified_coordinates_never_produce_wgs84_geojson():
    catalog = ChinaPlaceCatalog({"places": [
        {"id": "one", "name": "公开省", "level": "province", "longitude": 999, "latitude": 30, "crs": "WGS84"},
        {"id": "two", "name": "未知省", "level": "province", "longitude": 110, "latitude": 30},
        {"id": "three", "name": "非法省", "level": "province", "longitude": float("nan"), "latitude": True, "crs": "EPSG:4326"},
    ]})
    places, status = catalog.resolve("公开省/未知省/非法省")
    assert status == "mapped" and len(places) == 3
    result = geography_projection([{"id": "job", "places": places}], catalog=catalog)
    assert result["features"] == {"type": "FeatureCollection", "features": []}
    assert result["unplottable_places"] == 3


def test_real_offline_catalog_resolves_key_cities_colon_ids_and_hong_kong_districts():
    catalog = china_place_catalog()
    assert len(catalog._places) >= 3_000
    for value, identity in (
        ("Beijing", "110000"), ("Shanghai", "310000"), ("深圳", "440300"),
        ("广州", "440100"), ("杭州", "330100"), ("成都", "510100"),
        ("Hong Kong", "810000"), ("广东省深圳市南山区", "geonames:6571327"),
        ("北京市朝阳区", "110105"), ("香港中西区", "810001"),
        ("Central and Western District", "810001"),
    ):
        places, status = catalog.resolve(value)
        assert status == "mapped" and [place["id"] for place in places] == [identity]
        assert places[0]["crs"] == "WGS84"
    assert catalog.resolve("南山区") == ([], "ambiguous")
    assert catalog.resolve("朝阳区") == ([], "ambiguous")
    assert catalog.resolve("西湖区") == ([], "ambiguous")
    # Province-level municipalities/SARs remain their actual catalog level;
    # neither a guessed city nor headquarters are manufactured.
    assert catalog.resolve("Beijing")[0][0]["level"] == "province"
    assert catalog.resolve("Hong Kong")[0][0]["level"] == "province"


def test_duplicate_districts_with_same_city_parent_stay_ambiguous_with_only_explicit_parent_pin():
    data = {"places": [
        {"id": "440000", "name": "广东省", "level": "province", "crs": "WGS84", "longitude": 113, "latitude": 23},
        {"id": "440300", "name": "深圳市", "level": "city", "province_id": "440000", "parent_id": "440000",
         "city_id": "440300", "crs": "WGS84", "longitude": 114, "latitude": 22},
        *[{"id": f"geonames:{index}", "name": "同名区", "level": "district", "province_id": "440000",
           "city_id": "440300", "parent_id": "440300", "crs": "WGS84", "longitude": 114, "latitude": 22}
          for index in (1, 2)],
    ]}
    catalog = ChinaPlaceCatalog(data)
    places, status = catalog.resolve("深圳市同名区")
    assert status == "ambiguous" and [place["id"] for place in places] == ["440300"]
    result = geography_projection([{"id": "job", "places": places, "location_status": status}], catalog=catalog)
    assert result["matched_opportunities"] == result["ambiguous_opportunities"] == 1
    assert result["unmapped_opportunities"] == 0
    assert {feature["id"] for feature in result["features"]["features"]} == {"440300", "440000"}


def same_name_city_catalog(*, district_parent="city"):
    return ChinaPlaceCatalog({"places": [
        {"id": "province", "name": "示例省", "level": "province"},
        {"id": "city", "name": "示例市", "level": "city", "province_id": "province",
         "city_id": "city", "parent_id": "province", "aliases": ["示例", "Example City"]},
        {"id": "legacy", "name": "示例市", "level": "district", "province_id": "province",
         "city_id": district_parent, "parent_id": district_parent,
         "aliases": ["示例", "Example City", "现区", "Legacy District"]},
    ]})


@pytest.mark.parametrize("value", ["示例市", "示例", "Example City", "示例省示例市", "示例省示例"])
def test_shared_city_district_name_or_alias_does_not_invent_district_precision(value):
    places, status = same_name_city_catalog().resolve(value)
    assert status == "mapped"
    assert [(place["id"], place["level"]) for place in places] == [("city", "city")]


@pytest.mark.parametrize("value", ["现区", "Legacy District", "示例市现区"])
def test_distinct_alias_still_establishes_explicit_historical_district(value):
    places, status = same_name_city_catalog().resolve(value)
    assert status == "mapped"
    assert [(place["id"], place["level"]) for place in places] == [("legacy", "district")]


def test_shared_cross_level_name_without_confirmed_city_parent_stays_ambiguous():
    assert same_name_city_catalog(district_parent="unknown").resolve("示例市") == ([], "ambiguous")


@pytest.mark.parametrize("value, identity", [
    ("宜宾市", "511500"), ("四川省宜宾市", "511500"),
    ("毕节市", "520500"), ("贵州省毕节市", "520500"), ("毕节", "520500"),
    ("吐鲁番市", "650400"), ("新疆维吾尔自治区吐鲁番市", "650400"), ("吐鲁番", "650400"),
])
def test_real_current_city_names_never_choose_same_named_historical_adm3(value, identity):
    places, status = china_place_catalog().resolve(value)
    assert status == "mapped"
    assert [(place["id"], place["level"]) for place in places] == [(identity, "city")]


@pytest.mark.parametrize("value, identity", [
    ("宜宾市翠屏区", "geonames:1786768"), ("吐鲁番市高昌区", "geonames:1529111"),
])
def test_real_explicit_distinct_district_alias_is_not_lost(value, identity):
    places, status = china_place_catalog().resolve(value)
    assert status == "mapped"
    assert [(place["id"], place["level"]) for place in places] == [(identity, "district")]


def test_shared_alias_for_differently_named_city_and_district_stays_ambiguous():
    catalog = ChinaPlaceCatalog({"places": [
        {"id": "province", "name": "示例省", "level": "province"},
        {"id": "city", "name": "示例市", "level": "city", "province_id": "province",
         "city_id": "city", "parent_id": "province", "aliases": ["示例"]},
        {"id": "district", "name": "示例县", "level": "district", "province_id": "province",
         "city_id": "city", "parent_id": "city", "aliases": ["示例"]},
    ]})
    assert catalog.resolve("示例") == ([], "ambiguous")
    places, status = catalog.resolve("示例市示例县")
    assert status == "mapped" and [place["id"] for place in places] == ["district"]
    assert china_place_catalog().resolve("宜宾") == ([], "ambiguous")
