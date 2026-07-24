"""Tests for the ecosystem-cache readiness guard (scripts/check_caches.py)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from check_caches import evaluate  # noqa: E402

_GB = 1024 ** 3
_MB = 1024 ** 2

_DEFAULTS = {"max_uncached_gb": 2.0, "max_geometry_gb": 1.0, "max_features": 200_000}


def _stats(*, size=None, is_parquet=False, geom=None, rows=None, rgs=None):
    return {
        "size_bytes": size,
        "is_parquet": is_parquet,
        "geometry_uncompressed_bytes": geom,
        "num_rows": rows,
        "num_row_groups": rgs,
    }


def _evaluate(configured=None, missing=None, stats=None, thresholds=None):
    return evaluate(
        configured=configured or {},
        missing=missing or [],
        stats=stats if stats is not None else _stats(),
        thresholds=thresholds or _DEFAULTS,
    )


# --- configured-cache branch -------------------------------------------------

def test_configured_and_present_passes():
    ok, _ = _evaluate(configured={"optimized_data": "gs://b/x.parquet"}, missing=[])
    assert ok


def test_configured_but_missing_fails():
    ok, msg = _evaluate(
        configured={"aoo_grid_cache_url": "gs://b/g.parquet"},
        missing=[("aoo_grid_cache_url", "gs://b/g.parquet")],
    )
    assert not ok
    assert "build-caches" in msg


# --- compressed-size fallback (non-parquet / unreadable footer) --------------

def test_no_caches_large_compressed_fails():
    ok, msg = _evaluate(stats=_stats(size=int(11 * _GB)))
    assert not ok
    assert "11.0 GB" in msg


def test_no_caches_small_passes():
    ok, _ = _evaluate(stats=_stats(size=50 * _MB))
    assert ok


def test_no_caches_unknown_size_passes():
    ok, _ = _evaluate(stats=_stats())
    assert ok


def test_compressed_threshold_override_allows_large_data():
    ok, _ = _evaluate(
        stats=_stats(size=int(11 * _GB)),
        thresholds={**_DEFAULTS, "max_uncached_gb": 20.0},
    )
    assert ok


# --- parquet footer signals (the Colombia OOM the old guard missed) ----------

def test_parquet_large_geometry_fails():
    # Colombia: 1.68 GB file but 2.17 GB uncompressed geometry — passed the old
    # 2 GB file-size guard, OOM-killed the render.
    ok, msg = _evaluate(stats=_stats(
        is_parquet=True, size=int(1.68 * _GB),
        geom=int(2.17 * _GB), rows=460_350, rgs=1,
    ))
    assert not ok
    assert "uncompressed" in msg


def test_parquet_many_features_fails():
    # Small geometry per feature but a huge feature count.
    ok, msg = _evaluate(stats=_stats(
        is_parquet=True, geom=int(0.2 * _GB), rows=300_000, rgs=50,
    ))
    assert not ok
    assert "features" in msg


def test_parquet_single_row_group_large_geom_fails():
    # Under the 1 GB geometry and 200k feature thresholds, but one row group +
    # substantial geometry defeats per-ecosystem pushdown.
    ok, msg = _evaluate(stats=_stats(
        is_parquet=True, geom=int(0.6 * _GB), rows=50_000, rgs=1,
    ))
    assert not ok
    assert "row group" in msg


def test_parquet_small_single_row_group_passes():
    # A small national subset (e.g. a city extent) is fine without caches even
    # as one row group: geometry below the pushdown floor, few features.
    ok, _ = _evaluate(stats=_stats(
        is_parquet=True, geom=int(0.2 * _GB), rows=30_000, rgs=1,
    ))
    assert ok


def test_geometry_threshold_override_allows_large_geometry():
    ok, _ = _evaluate(
        stats=_stats(is_parquet=True, geom=int(2.17 * _GB)),
        thresholds={**_DEFAULTS, "max_geometry_gb": 5.0},
    )
    assert ok
