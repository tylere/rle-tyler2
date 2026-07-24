"""Fail fast when the ecosystem caches a render depends on are missing/insufficient.

Rendering ``crit_b`` recomputes the national AOO grid in memory, which OOM-kills CI
runners for large (e.g. national) ecosystem maps. The fix is to precompute caches
offline (``pixi run build-caches``) and point ``optimized_data`` /
``aoo_grid_cache_url`` at them. This guard runs in the deploy *before* the
expensive render and fails with an actionable message when:

  * a configured cache (``optimized_data`` / ``aoo_grid_cache_url``) does not exist
    at its URL — it was never built, or ``ecosystem_source.data`` changed and the
    caches were not rebuilt; or
  * no caches are configured but ``ecosystem_source.data`` is heavy enough that the
    render will likely OOM or time out.

For the second case, compressed file size is a poor proxy for the render's memory:
a 1.7 GB parquet whose geometry is 2.2 GB *uncompressed* (and materialises far
larger as shapely objects) passed a 2 GB file-size guard yet OOM-killed the render.
So for parquet sources this reads the cheap footer metadata and thresholds on the
memory-relevant signals — uncompressed geometry size and feature count — plus flags
few-row-group parquet (which defeats the per-ecosystem predicate pushdown). It
falls back to compressed size for non-parquet sources or when the footer can't be
read.

It intentionally does NOT build the caches in CI — that is the very memory-heavy
work the caches exist to move offline. It only reads HTTP metadata (the cache and
data buckets are public), so it needs no GCP credentials.
"""

import os
import sys
import urllib.request
from urllib.error import HTTPError, URLError

from _config import load_country_config

_GB = 1024 ** 3
_MB = 1024 ** 2

# No caches configured: thresholds above which the render likely OOMs/times out.
DEFAULT_MAX_UNCACHED_GB = 2.0        # compressed size (non-parquet / no-footer fallback)
DEFAULT_MAX_GEOMETRY_GB = 1.0        # uncompressed geometry column (parquet)
DEFAULT_MAX_FEATURES = 200_000       # feature (row) count (parquet)
# A parquet with fewer than this many row groups can't be pruned by the
# per-ecosystem predicate pushdown (every page rescans the whole geometry
# column), but only worth flagging when the geometry is non-trivial.
_MIN_ROW_GROUPS = 3
_PUSHDOWN_GEOMETRY_FLOOR = 512 * _MB

_CACHE_KEYS = ("optimized_data", "aoo_grid_cache_url")


def _to_http(url: str) -> str:
    """Map a ``gs://`` URI to its public https URL; pass http(s) through."""
    if url.startswith("gs://"):
        return "https://storage.googleapis.com/" + url[len("gs://"):]
    return url


def _head(url: str):
    """Return the HEAD response for ``url``, or None on error."""
    req = urllib.request.Request(_to_http(url), method="HEAD")
    try:
        return urllib.request.urlopen(req, timeout=30)
    except (HTTPError, URLError, ValueError):
        return None


def remote_exists(url: str) -> bool:
    resp = _head(url)
    return resp is not None and 200 <= resp.status < 300


def remote_size(url: str):
    """Content-Length in bytes, or None if unknown."""
    resp = _head(url)
    if resp is None:
        return None
    cl = resp.headers.get("Content-Length")
    return int(cl) if cl and cl.isdigit() else None


def _parquet_footer_stats(fileobj) -> dict:
    """Return memory-relevant stats from a parquet footer (no data read).

    ``geometry_uncompressed_bytes`` is the uncompressed size of the GeoParquet
    primary geometry column (from the file's ``geo`` metadata; falls back to a
    column named ``geometry`` or the largest column). This is a far better proxy
    for the render's peak RAM than the compressed file size.
    """
    import json

    import pyarrow.parquet as pq

    md = pq.ParquetFile(fileobj).metadata

    geo_col = None
    kv = md.metadata or {}
    raw = kv.get(b"geo")
    if raw:
        try:
            geo_col = json.loads(raw.decode()).get("primary_column")
        except (ValueError, AttributeError):
            geo_col = None

    per: dict[str, int] = {}
    for g in range(md.num_row_groups):
        rg = md.row_group(g)
        for c in range(rg.num_columns):
            col = rg.column(c)
            per[col.path_in_schema] = per.get(col.path_in_schema, 0) + col.total_uncompressed_size

    if geo_col not in per:
        geo_col = "geometry" if "geometry" in per else (max(per, key=per.get) if per else None)

    return {
        "num_rows": md.num_rows,
        "num_row_groups": md.num_row_groups,
        "geometry_uncompressed_bytes": per.get(geo_col, 0) if geo_col else 0,
    }


def probe_source(data: str) -> dict:
    """Gather stats about ``ecosystem_source.data`` for the OOM/timeout heuristic.

    HTTP-only. Reads the parquet footer for a memory-relevant estimate; falls back
    to the compressed Content-Length for non-parquet sources or on any error.
    """
    stats = {
        "size_bytes": remote_size(data),
        "is_parquet": str(data).split("?")[0].rstrip("/").lower().endswith(".parquet"),
        "geometry_uncompressed_bytes": None,
        "num_rows": None,
        "num_row_groups": None,
    }
    if stats["is_parquet"]:
        try:
            import fsspec

            with fsspec.open(data, "rb") as f:
                stats.update(_parquet_footer_stats(f))
        except Exception as exc:  # noqa: BLE001 - footer read is best-effort
            print(f"check-caches: could not read parquet footer ({exc}); "
                  "falling back to compressed file size.")
    return stats


def evaluate(*, configured, missing, stats, thresholds):
    """Pure decision. Returns ``(ok: bool, message: str)``.

    ``configured`` is the {key: url} of cache URLs that are set; ``missing`` is the
    subset [(key, url), ...] that do not exist. ``stats`` (from ``probe_source``) is
    only consulted when no caches are configured. ``thresholds`` carries
    ``max_uncached_gb`` / ``max_geometry_gb`` / ``max_features``.
    """
    if configured:
        if missing:
            lines = "\n".join(f"  - {name}: {url}" for name, url in missing)
            return False, (
                "Ecosystem cache(s) referenced in config/country_config.yaml do not "
                "exist (never built, or ecosystem_source.data changed and they were "
                f"not rebuilt):\n{lines}\n\n"
                "Rebuild them (GCS write access + enough RAM), then re-deploy:\n"
                "  gcloud auth application-default login --project <your-gcp-project>\n"
                "  pixi run build-caches --project <your-gcp-project>"
            )
        return True, "All configured ecosystem caches are present."

    geom = stats.get("geometry_uncompressed_bytes")
    rows = stats.get("num_rows")
    rgs = stats.get("num_row_groups")
    size = stats.get("size_bytes")

    reasons = []
    if geom is not None and geom >= thresholds["max_geometry_gb"] * _GB:
        reasons.append(f"its geometry is {geom / _GB:.1f} GB uncompressed")
    if rows is not None and rows >= thresholds["max_features"]:
        reasons.append(f"it has {rows:,} features")
    if (rgs is not None and rgs < _MIN_ROW_GROUPS
            and geom is not None and geom >= _PUSHDOWN_GEOMETRY_FLOOR):
        reasons.append(
            f"it has only {rgs} parquet row group(s), so per-ecosystem filtering "
            "cannot use predicate pushdown (every page rescans the whole map)"
        )
    # Fallback for non-parquet sources / unreadable footers: compressed size only.
    if not reasons and geom is None and size is not None \
            and size >= thresholds["max_uncached_gb"] * _GB:
        reasons.append(f"it is {size / _GB:.1f} GB")

    if reasons:
        bullets = "\n".join(f"  - {r}" for r in reasons)
        return False, (
            "ecosystem_source.data will likely OOM or time out the render, and no "
            "caches are configured (optimized_data / aoo_grid_cache_url):\n"
            f"{bullets}\n\n"
            "Fix: build the caches once. `build-caches` precomputes the "
            "ecosystem-sorted map and the national AOO grid so the render reads "
            "them instead of recomputing the whole map in memory, and it records "
            "optimized_data + aoo_grid_cache_url into config/country_config.yaml "
            "for you (you do not set them by hand):\n\n"
            "  gcloud auth application-default login --project <your-gcp-project>\n"
            "  pixi run build-caches --project <your-gcp-project>\n\n"
            "Then commit the updated config/country_config.yaml and re-deploy. Run "
            "it on a machine with GCS write access and enough RAM to load the whole "
            "map; for a large remote source, download it and point "
            "ecosystem_source.data at the local copy first (restore the URL after).\n"
            "(Thresholds: CHECK_CACHES_MAX_GEOMETRY_GB, CHECK_CACHES_MAX_FEATURES, "
            "CHECK_CACHES_MAX_UNCACHED_GB.)"
        )
    return True, "Ecosystem data is small enough to render without caches."


def _thresholds() -> dict:
    return {
        "max_uncached_gb": float(
            os.environ.get("CHECK_CACHES_MAX_UNCACHED_GB", DEFAULT_MAX_UNCACHED_GB)
        ),
        "max_geometry_gb": float(
            os.environ.get("CHECK_CACHES_MAX_GEOMETRY_GB", DEFAULT_MAX_GEOMETRY_GB)
        ),
        "max_features": int(
            os.environ.get("CHECK_CACHES_MAX_FEATURES", DEFAULT_MAX_FEATURES)
        ),
    }


def main() -> None:
    config = load_country_config()
    source = config["ecosystem_source"]

    configured = {k: source[k] for k in _CACHE_KEYS if source.get(k)}
    missing = [(k, url) for k, url in configured.items() if not remote_exists(url)]

    stats = {} if configured else probe_source(source["data"])

    ok, message = evaluate(
        configured=configured,
        missing=missing,
        stats=stats,
        thresholds=_thresholds(),
    )
    if ok:
        print(f"check-caches: {message}")
    else:
        sys.exit(f"check-caches: {message}")


if __name__ == "__main__":
    main()
