"""Precompute the cached artifacts renders read instead of the national map.

Building these from the full ``ecosystem_source.data`` map peaks at many GB of
RAM, which OOM-kills CI runners. This script runs the heavy work once — on a
machine with enough memory — and writes small artifacts that renders read
cheaply:

  * ``optimized_data`` — an ecosystem-sorted copy of the map with small row
    groups, so filtering to one ecosystem uses parquet predicate pushdown
    (reads only that ecosystem's rows) instead of loading the whole map. When the
    source is already sorted with small row groups (see ``scripts/optimize_source.py``),
    that copy is skipped and ``optimized_data`` points straight at the source.
  * ``aoo_grid_cache_url`` — the precomputed national AOO grid.

When neither URL is configured, the destinations are derived from the GCP
project + the data filename (bucket ``{project}-rle-cogs``, keys
``cache/{data-stem}_sorted.parquet`` / ``cache/{data-stem}_aoo_grid.parquet``)
and recorded back into the config — the same project/data-keyed scheme
``rasterize_ecosystem_to_cog.py`` uses for the COG, so changing
``ecosystem_source.data`` points at fresh paths automatically. Explicitly
configured URLs are respected (only those are built).

Usage:
    pixi run build-caches --project my-gcp-project
    # or set GOOGLE_CLOUD_PROJECT; or pre-set optimized_data/aoo_grid_cache_url.
"""

import argparse
import os
import sys
from pathlib import Path

import yaml

from _config import ensure_vector_source
from optimize_source import DEFAULT_ROW_GROUP_SIZE, optimize_to_parquet

CONFIG_PATH = Path("config/country_config.yaml")

_PLACEHOLDER_PROJECT = "goog-rle-assessments"
_REMOTE_SCHEMES = ("http://", "https://", "gs://", "s3://", "az://")
# Loading a remote parquet this large (uncompressed geometry) whole will time out
# unless it has small row groups; guard before attempting it.
_LARGE_GEOMETRY_BYTES = 1024 ** 3


def _bucket_name(project: str) -> str:
    """Derive the cache bucket name from the GCP project ID."""
    return f"{project}-rle-cogs"


def _data_stem(data: str) -> str:
    """Filename stem of ``data`` (query string / trailing slash stripped)."""
    return Path(str(data).split("?")[0].rstrip("/")).stem or "ecosystems"


def derive_cache_urls(project: str, data: str) -> dict:
    """gs:// URLs for the sorted parquet + AOO grid, keyed by project + data."""
    bucket = _bucket_name(project)
    stem = _data_stem(data)
    return {
        "optimized_data": f"gs://{bucket}/cache/{stem}_sorted.parquet",
        "aoo_grid_cache_url": f"gs://{bucket}/cache/{stem}_aoo_grid.parquet",
    }


def _require_project(project) -> str:
    """Return a usable project id, or exit with an actionable message."""
    if not project:
        sys.exit(
            "Set --project or GOOGLE_CLOUD_PROJECT so build-caches can derive the "
            "cache URLs (bucket {project}-rle-cogs), or pre-set "
            "ecosystem_source.optimized_data / aoo_grid_cache_url in the config."
        )
    if project == _PLACEHOLDER_PROJECT:
        sys.exit(
            f"'{_PLACEHOLDER_PROJECT}' is the template's placeholder project. "
            "Pass --project <your-gcp-project> (or set GOOGLE_CLOUD_PROJECT)."
        )
    return project


def record_urls_in_config(config_path: Path, updates: dict) -> None:
    """Insert ``updates`` keys under ``ecosystem_source:`` in the YAML file.

    A targeted line insertion (rather than a full ``yaml.dump``) so the rest of
    the file — comments, ordering, formatting — is preserved.
    """
    lines = config_path.read_text().splitlines(keepends=True)
    start = next(
        (i for i, ln in enumerate(lines)
         if not ln[:1].isspace() and ln.lstrip().startswith("ecosystem_source:")),
        None,
    )
    if start is None:
        raise SystemExit(f"No ecosystem_source: block in {config_path}")

    end = len(lines)
    for i in range(start + 1, len(lines)):
        ln = lines[i]
        if ln.strip() and not ln[:1].isspace():  # next top-level key ends the block
            end = i
            break
    if end > 0 and not lines[end - 1].endswith("\n"):
        lines[end - 1] += "\n"

    lines[end:end] = [f"  {k}: {v}\n" for k, v in updates.items()]
    config_path.write_text("".join(lines))


def _is_remote(data) -> bool:
    return str(data).startswith(_REMOTE_SCHEMES)


def source_footer(data, ecosystem_column) -> dict | None:
    """Footer-only stats for a parquet source (no data download).

    Returns None for non-parquet sources or if the footer can't be read. The dict
    carries ``num_row_groups``, ``num_rows``, ``max_rows_per_group``, the ecosystem
    column's per-row-group ``(min, max)`` stats (``eco_minmax``; None if any are
    absent), and ``geometry_uncompressed_bytes`` — enough to decide "already
    optimized?" and to guard against a doomed full load, all from HTTP range reads
    of the footer.
    """
    if not str(data).split("?")[0].rstrip("/").lower().endswith(".parquet"):
        return None
    try:
        import json

        import fsspec
        import pyarrow.parquet as pq

        with fsspec.open(data, "rb") as f:
            md = pq.ParquetFile(f).metadata
            names = [md.schema.column(i).path for i in range(md.num_columns)]

            geo_col = None
            raw = (md.metadata or {}).get(b"geo")
            if raw:
                try:
                    geo_col = json.loads(raw.decode()).get("primary_column")
                except (ValueError, AttributeError):
                    geo_col = None

            eco_idx = names.index(ecosystem_column) if ecosystem_column in names else None
            per_bytes: dict[str, int] = {}
            eco_minmax = [] if eco_idx is not None else None
            max_rows = 0
            for g in range(md.num_row_groups):
                rg = md.row_group(g)
                max_rows = max(max_rows, rg.num_rows)
                for c in range(rg.num_columns):
                    col = rg.column(c)
                    per_bytes[col.path_in_schema] = (
                        per_bytes.get(col.path_in_schema, 0) + col.total_uncompressed_size
                    )
                if eco_minmax is not None:
                    st = rg.column(eco_idx).statistics
                    eco_minmax.append((st.min, st.max) if st and st.has_min_max else None)

            if eco_minmax is not None and any(mm is None for mm in eco_minmax):
                eco_minmax = None  # can't verify sortedness without complete stats

            if geo_col not in per_bytes:
                geo_col = "geometry" if "geometry" in per_bytes else (
                    max(per_bytes, key=per_bytes.get) if per_bytes else None)

            return {
                "num_row_groups": md.num_row_groups,
                "num_rows": md.num_rows,
                "max_rows_per_group": max_rows,
                "eco_minmax": eco_minmax,
                "geometry_uncompressed_bytes": per_bytes.get(geo_col, 0) if geo_col else 0,
            }
    except Exception:  # noqa: BLE001 - footer read is best-effort
        return None


def is_pushdown_optimized(footer, row_group_size=DEFAULT_ROW_GROUP_SIZE) -> bool:
    """True if a parquet is sorted by the ecosystem column with small row groups.

    Requires (from ``source_footer``): more than one row group, the largest row
    group small enough to prune usefully, and the ecosystem column's per-row-group
    min/max monotonically non-decreasing (globally sorted). All footer-only.
    """
    if not footer or footer["eco_minmax"] is None:
        return False
    if footer["num_row_groups"] < 2:
        return False
    if footer["max_rows_per_group"] > max(row_group_size * 5, 50_000):
        return False
    prev_max = None
    for mn, mx in footer["eco_minmax"]:
        if prev_max is not None and mn < prev_max:
            return False  # a later row group starts below an earlier one → not sorted
        prev_max = mx
    return True


def build_caches(config_path: Path = CONFIG_PATH, project=None) -> None:
    """Load ``ecosystem_source.data`` once and write the configured caches."""
    with open(config_path) as f:
        config = yaml.safe_load(f)
    source = config["ecosystem_source"]

    # ecosystem_code_column is optional: fall back to the name column so the
    # sort key and grid columns match how crit_b filters ecosystems.
    ecosystem_column = source.get("ecosystem_code_column") or source.get("ecosystem_name_column")
    if ecosystem_column is None:
        raise SystemExit(
            "ecosystem_source needs ecosystem_code_column or ecosystem_name_column "
            f"in {config_path}"
        )

    data = ensure_vector_source(source["data"])

    # Footer-only inspection (no data download): is the source already sorted with
    # small row groups? If so we can point optimized_data straight at it.
    footer = source_footer(data, ecosystem_column)
    already_optimized = is_pushdown_optimized(footer)

    # A large, unoptimized, remote parquet can't be loaded whole without timing out
    # (one giant row group -> one enormous range read). Fail fast with a fix rather
    # than hang on the read below.
    if (not already_optimized and _is_remote(data) and footer
            and footer["geometry_uncompressed_bytes"] >= _LARGE_GEOMETRY_BYTES):
        sys.exit(
            f"{data}\nis a large, unoptimized remote parquet "
            f"({footer['geometry_uncompressed_bytes'] / 1024 ** 3:.1f} GB geometry, "
            f"{footer['num_row_groups']} row group(s)); loading it whole will time out.\n\n"
            "Optimize it first (sort by ecosystem + small row groups), then point "
            "ecosystem_source.data at the result:\n"
            "  # download a local copy of the source, then:\n"
            f"  pixi run optimize-source LOCAL_COPY.parquet OPTIMIZED.parquet "
            f"--ecosystem-column {ecosystem_column}\n"
            "  # publish OPTIMIZED.parquet and set ecosystem_source.data to it."
        )

    # Destinations: respect explicitly-configured URLs; otherwise (neither set)
    # derive both from the project + data and record them back into the config.
    optimized = source.get("optimized_data")
    cache_url = source.get("aoo_grid_cache_url")
    derived: dict = {}
    if not optimized and not cache_url:
        derived = derive_cache_urls(_require_project(project), data)
        cache_url = derived["aoo_grid_cache_url"]
        if already_optimized:
            # Source is already sorted + small-row-group: use it directly, no re-sort.
            optimized = data
            derived["optimized_data"] = data
        else:
            optimized = derived["optimized_data"]

    from rle.core import Ecosystems
    from rle.core.aoo import make_aoo_grid

    eco = Ecosystems.from_file(
        data,
        ecosystem_column=ecosystem_column,
        ecosystem_name_column=source.get("ecosystem_name_column"),
        functional_group_column=source.get("functional_group_column"),
    )
    print(f"Loading {data} ...")
    gdf = eco.load()
    print(f"  {len(gdf)} features")

    # Write the sorted copy only when we are not reusing the source as optimized_data.
    if optimized and optimized != data:
        print(f"Writing ecosystem-sorted parquet -> {optimized}")
        optimize_to_parquet(gdf, optimized, ecosystem_column=ecosystem_column)
    elif optimized == data:
        print(f"Source is already pushdown-optimized; using it as optimized_data ({data}).")

    if cache_url:
        print(f"Computing AOO grid -> {cache_url}")
        aoo = make_aoo_grid(eco).compute()
        print(f"  {aoo.cell_count} grid cells")
        aoo.to_parquet(cache_url)

    if derived:
        record_urls_in_config(config_path, derived)
        print(f"Recorded {', '.join(derived)} in {config_path}")

    print("Done.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=CONFIG_PATH,
        help=f"Path to country_config.yaml (default: {CONFIG_PATH})",
    )
    parser.add_argument(
        "--project", default=os.environ.get("GOOGLE_CLOUD_PROJECT"),
        help="GCP project ID used to derive cache URLs when they are not "
             "configured (default: $GOOGLE_CLOUD_PROJECT).",
    )
    args = parser.parse_args()
    build_caches(args.config, args.project)


if __name__ == "__main__":
    main()
