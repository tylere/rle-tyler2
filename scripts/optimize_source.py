"""Write a query-optimized GeoParquet copy of an ecosystem vector source.

Two properties make per-ecosystem reads cheap for the render and for
``build-caches``:

  * the rows are **sorted by the ecosystem column**, so each ecosystem's features
    are contiguous; and
  * the file is written with **small row groups**, so a ``filter(ecosystem)`` prunes
    (predicate pushdown) to just that ecosystem's row groups instead of scanning the
    whole map.

A single giant row group (the geopandas/pyarrow default when ``row_group_size`` is
not set) defeats both: pushdown can't prune, and reading the file over HTTP becomes
one enormous range request that times out. This script is the reproducible way to
produce the optimized source, so that logic lives here rather than in a notebook.

Usage:
    python scripts/optimize_source.py INPUT OUTPUT.parquet --ecosystem-column ecos_general
    # INPUT may be a GeoDatabase, GeoParquet, GeoJSON, GeoPackage or shapefile.
"""

import argparse
from pathlib import Path

# Small row groups let pyarrow prune to a single ecosystem's rows on read.
DEFAULT_ROW_GROUP_SIZE = 10_000


def read_vector(path):
    """Read a vector source into a (Geo)DataFrame (parquet or any OGR format)."""
    import geopandas as gpd

    if str(path).split("?")[0].rstrip("/").lower().endswith(".parquet"):
        return gpd.read_parquet(path)
    return gpd.read_file(path)


def optimize_to_parquet(gdf, out_path, *, ecosystem_column,
                        row_group_size=DEFAULT_ROW_GROUP_SIZE):
    """Sort ``gdf`` by ``ecosystem_column`` and write GeoParquet with small row groups.

    Sorting makes each ecosystem's rows contiguous; the small ``row_group_size``
    then lets a per-ecosystem ``filter()`` prune to just those row groups. Returns
    ``out_path``.
    """
    if ecosystem_column not in gdf.columns:
        raise SystemExit(
            f"ecosystem column {ecosystem_column!r} not found in the source "
            f"(columns: {', '.join(map(str, gdf.columns))})"
        )
    (
        gdf.sort_values(ecosystem_column, ignore_index=True)
        .to_parquet(out_path, compression="zstd", row_group_size=row_group_size)
    )
    return out_path


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", help="Vector source (gdb / parquet / geojson / gpkg / shp).")
    parser.add_argument("output", type=Path, help="Output GeoParquet path.")
    parser.add_argument(
        "--ecosystem-column", required=True,
        help="Column to sort by (the ecosystem code/name column, e.g. ecos_general).",
    )
    parser.add_argument(
        "--row-group-size", type=int, default=DEFAULT_ROW_GROUP_SIZE,
        help=f"Rows per parquet row group (default: {DEFAULT_ROW_GROUP_SIZE}).",
    )
    args = parser.parse_args()

    print(f"Reading {args.input} ...")
    gdf = read_vector(args.input)
    print(f"  {len(gdf):,} features; sorting by {args.ecosystem_column!r} and writing "
          f"row groups of {args.row_group_size:,} ...")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    optimize_to_parquet(
        gdf, args.output,
        ecosystem_column=args.ecosystem_column,
        row_group_size=args.row_group_size,
    )
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()
