"""Tests for AstroBin acquisition parsing and mix aggregation."""

from photonscript.scheduler.astrobin_client import (aggregate_mix,
                                                    classify_filter,
                                                    extract_acquisition_hours)


def test_classify_filter_real_world_names():
    assert classify_filter("Astrodon Ha 5nm") == "Ha"
    assert classify_filter("H-alpha 7nm") == "Ha"
    assert classify_filter("Chroma OIII 3nm") == "OIII"
    assert classify_filter("SII 6.5nm") == "SII"
    assert classify_filter("Baader Luminance") == "L"
    assert classify_filter("Red") == "R"
    assert classify_filter("g") == "G"
    assert classify_filter("Optolong Blue CCD") == "B"
    assert classify_filter("UV/IR Cut") == "L"
    assert classify_filter("mystery filter 9000") is None
    assert classify_filter("") is None


def test_extract_hours_from_acquisitions():
    image = {"deep_sky_acquisitions": [
        {"filter": "Ha 3nm", "number": "24", "duration": "600"},
        {"filter": "OIII", "number": 12, "duration": 600},
        {"filter": {"name": "SII"}, "number": 12, "duration": 600},
        {"filter": "junk", "number": 5, "duration": 300},   # unclassifiable
        {"filter": "Ha 3nm", "number": 0, "duration": 600},  # zero -> skip
    ]}
    hours = extract_acquisition_hours(image)
    assert hours["Ha"] == 4.0
    assert hours["OIII"] == 2.0
    assert hours["SII"] == 2.0
    assert "junk" not in hours


def test_aggregate_mix_across_images():
    images = [
        {"deep_sky_acquisitions": [
            {"filter": "Ha", "number": 10, "duration": 600},     # 1.67h
            {"filter": "OIII", "number": 10, "duration": 600},
            {"filter": "SII", "number": 20, "duration": 600}]},  # SII-heavy
        {"deep_sky_acquisitions": [
            {"filter": "Ha", "number": 30, "duration": 300},
            {"filter": "SII", "number": 30, "duration": 300}]},
        {"title": "no acquisition data"},
    ]
    result = aggregate_mix(images)
    assert result["images_sampled"] == 3
    assert result["images_with_data"] == 2
    assert set(result["mix"]) == {"Ha", "OIII", "SII"}
    assert abs(sum(result["mix"].values()) - 100) < 0.5
    assert result["mix"]["SII"] > result["mix"]["OIII"]


def test_aggregate_mix_empty():
    result = aggregate_mix([{"title": "nothing"}])
    assert result["mix"] == {}
    assert result["images_with_data"] == 0


def test_curated_mix_lookup():
    from photonscript.scheduler.astrobin_client import curated_mix
    r = curated_mix("Crescent Nebula", "NGC 6888")
    assert r is not None
    assert r["mix"]["OIII"] == 45     # the OIII envelope is the picture
    assert r["source"] == "curated"
    # normalization: "NGC6888" without space
    assert curated_mix("", "NGC6888") is not None
    # unknown target -> None
    assert curated_mix("Made Up Object", "XYZ 1") is None


def test_curated_mixes_sum_to_100():
    from photonscript.scheduler.astrobin_client import CURATED_MIXES
    for key, entry in CURATED_MIXES.items():
        assert abs(sum(entry["mix"].values()) - 100) < 0.5, key
