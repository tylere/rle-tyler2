"""Tests for build_caches URL derivation and comment-preserving config write-back."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import build_caches as bc  # noqa: E402


def test_derive_cache_urls_is_project_and_data_keyed():
    urls = bc.derive_cache_urls("myproj", "https://h/ECOSISTEMAS_MEC_122024.parquet")
    assert urls["optimized_data"] == (
        "gs://myproj-rle-cogs/cache/ECOSISTEMAS_MEC_122024_sorted.parquet"
    )
    assert urls["aoo_grid_cache_url"] == (
        "gs://myproj-rle-cogs/cache/ECOSISTEMAS_MEC_122024_aoo_grid.parquet"
    )


def test_data_stem_strips_query_and_trailing_slash():
    assert bc._data_stem("https://h/map.parquet?token=abc") == "map"
    assert bc._data_stem("gs://b/dir/map.parquet/") == "map"


def test_require_project_rejects_missing_and_placeholder():
    with pytest.raises(SystemExit):
        bc._require_project(None)
    with pytest.raises(SystemExit):
        bc._require_project("goog-rle-assessments")
    assert bc._require_project("real-proj") == "real-proj"


def test_record_urls_inserts_under_ecosystem_source_preserving_comments(tmp_path):
    cfg = tmp_path / "country_config.yaml"
    cfg.write_text(
        "country_name: Ruritania\n"
        "ecosystem_source:\n"
        "  # keep this comment\n"
        "  data: https://h/map.parquet\n"
        "  ecosystem_name_column: ecos_general\n"
        "view_state:\n"
        "  zoom: 10\n"
    )
    bc.record_urls_in_config(
        cfg,
        {"optimized_data": "gs://b/cache/map_sorted.parquet",
         "aoo_grid_cache_url": "gs://b/cache/map_aoo_grid.parquet"},
    )
    text = cfg.read_text()
    assert "  # keep this comment" in text            # comment preserved
    lines = text.splitlines()
    es = lines.index("ecosystem_source:")
    vs = lines.index("view_state:")
    inserted = [i for i, ln in enumerate(lines) if "optimized_data:" in ln][0]
    assert es < inserted < vs                          # inserted inside the block
    assert "  optimized_data: gs://b/cache/map_sorted.parquet" in lines
    assert "  aoo_grid_cache_url: gs://b/cache/map_aoo_grid.parquet" in lines


def test_record_urls_when_ecosystem_source_is_last_block(tmp_path):
    cfg = tmp_path / "country_config.yaml"
    cfg.write_text(
        "country_name: Ruritania\n"
        "ecosystem_source:\n"
        "  data: https://h/map.parquet\n"
        "  ecosystem_name_column: ecos_general\n"
    )
    bc.record_urls_in_config(cfg, {"optimized_data": "gs://b/cache/map_sorted.parquet"})
    lines = cfg.read_text().splitlines()
    assert lines[-1] == "  optimized_data: gs://b/cache/map_sorted.parquet"


# --- is_pushdown_optimized: pure decision on a footer dict --------------------

def _footer(*, num_row_groups, max_rows_per_group, eco_minmax, num_rows=1000):
    return {
        "num_row_groups": num_row_groups,
        "num_rows": num_rows,
        "max_rows_per_group": max_rows_per_group,
        "eco_minmax": eco_minmax,
        "geometry_uncompressed_bytes": 0,
    }


def test_optimized_false_for_single_row_group():
    assert not bc.is_pushdown_optimized(
        _footer(num_row_groups=1, max_rows_per_group=1000, eco_minmax=[("a", "z")]))


def test_optimized_true_for_sorted_small_row_groups():
    assert bc.is_pushdown_optimized(_footer(
        num_row_groups=3, max_rows_per_group=10,
        eco_minmax=[("a", "c"), ("c", "m"), ("m", "z")]))  # non-decreasing


def test_optimized_false_for_unsorted_row_groups():
    assert not bc.is_pushdown_optimized(_footer(
        num_row_groups=3, max_rows_per_group=10,
        eco_minmax=[("a", "z"), ("a", "z"), ("a", "z")]))  # overlapping -> unsorted


def test_optimized_false_for_large_row_groups():
    assert not bc.is_pushdown_optimized(_footer(
        num_row_groups=2, max_rows_per_group=200_000,
        eco_minmax=[("a", "m"), ("m", "z")]))


def test_optimized_false_when_stats_missing():
    assert not bc.is_pushdown_optimized(_footer(
        num_row_groups=3, max_rows_per_group=10, eco_minmax=None))


def test_optimized_false_for_none_footer():
    assert not bc.is_pushdown_optimized(None)


# --- source_footer: read real parquet footers (no geometry needed) -----------

def _write_parquet(path, values, row_group_size):
    import pandas as pd
    import pyarrow as pa
    import pyarrow.parquet as pq

    df = pd.DataFrame({"ecos_general": list(values), "x": range(len(values))})
    pq.write_table(pa.Table.from_pandas(df), path, row_group_size=row_group_size)
    return str(path)


def test_source_footer_and_optimized_end_to_end(tmp_path):
    sorted_vals = [f"E{i:03d}" for i in range(100)]

    # Sorted with small row groups -> optimized.
    p_opt = _write_parquet(tmp_path / "opt.parquet", sorted_vals, row_group_size=10)
    f_opt = bc.source_footer(p_opt, "ecos_general")
    assert f_opt["num_row_groups"] == 10
    assert bc.is_pushdown_optimized(f_opt)

    # One giant row group -> not optimized (the Colombia case).
    p_one = _write_parquet(tmp_path / "one.parquet", sorted_vals, row_group_size=1000)
    assert bc.source_footer(p_one, "ecos_general")["num_row_groups"] == 1
    assert not bc.is_pushdown_optimized(bc.source_footer(p_one, "ecos_general"))

    # Multiple row groups but unsorted -> not optimized.
    interleaved = [v for pair in zip(sorted_vals[:50], sorted_vals[50:]) for v in pair]
    p_uns = _write_parquet(tmp_path / "uns.parquet", interleaved, row_group_size=10)
    assert not bc.is_pushdown_optimized(bc.source_footer(p_uns, "ecos_general"))


def test_source_footer_none_for_non_parquet():
    assert bc.source_footer("https://h/map.geojson", "ecos_general") is None
