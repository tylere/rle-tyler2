"""Shared helpers for reading and validating config/country_config.yaml.

Kept dependency-light (stdlib + PyYAML) so it can be imported from the build
scripts and from the rendered Quarto templates alike.
"""

from pathlib import Path

import yaml

CONFIG_PATH = Path("config/country_config.yaml")

# Raster (COG) extensions that must NOT appear in ecosystem_source.data.
_RASTER_SUFFIXES = (".tif", ".tiff")


def load_country_config(path=CONFIG_PATH):
    """Parse the country config YAML."""
    with open(path) as f:
        return yaml.safe_load(f)


def ensure_vector_source(data):
    """Fail fast when ``ecosystem_source.data`` is a raster instead of a vector.

    The scripts and templates read ``ecosystem_source.data`` with geopandas,
    which only understands vector formats (.parquet, .geojson, .gpkg,
    shapefile, GeoDatabase). A raster COG (.tif/.tiff) otherwise raises a
    cryptic pyogrio "not recognized as being in a supported file format" error
    deep in GDAL. Catch it here with an actionable message. Raster COGs belong
    under ``ecosystem_raster.cog_url``.

    Returns ``data`` unchanged so callers can wrap the value inline.
    """
    stem = str(data).split("?")[0].rstrip("/").lower()  # strip ?query and trailing /
    if stem.endswith(_RASTER_SUFFIXES):
        raise SystemExit(
            f"ecosystem_source.data points at a raster file:\n  {data}\n\n"
            "It must be a VECTOR source (.parquet, .geojson, .gpkg, shapefile, "
            "or GeoDatabase) that geopandas can read.\n"
            "Raster COGs (.tif/.tiff) belong under ecosystem_raster.cog_url in "
            "config/country_config.yaml."
        )
    return data


def load_ecosystems_lite(source):
    """Load an Ecosystems object with only the identifier/name/functional-group
    columns — no geometry — for enumeration, indexing and scaffolding.

    Prefers ``optimized_data`` and, for parquet sources, reads only the needed
    columns via column projection so a large national map is not fully
    downloaded (geometry, the bulk of the file, is never read). This is what lets
    ``build_ecosystem_index`` / ``2_build_ecosystems_config`` enumerate a 1.7 GB
    national parquet without a full, timeout-prone HTTP read. Non-parquet sources
    (e.g. GeoJSON) are typically small and read in full.

    The result behaves like any Ecosystems object for ``unique_ecosystems()``,
    ``unique_functional_groups()``, ``ecosystem_name()`` and column access, but
    has NO geometry — do not call geometry/EOO/AOO methods on it. Heavy imports
    are deferred so this module stays importable from the Quarto templates.
    """
    from rle.core import Ecosystems, EcosystemsGeoDataFrame

    # Prefer the ecosystem-sorted, pushdown-friendly cache when configured.
    data = source.get("optimized_data") or source["data"]
    ensure_vector_source(data)

    code_col = source.get("ecosystem_code_column") or source.get("ecosystem_name_column")
    name_col = source.get("ecosystem_name_column")
    fg_col = source.get("functional_group_column")

    is_parquet = str(data).split("?")[0].rstrip("/").lower().endswith(".parquet")
    if is_parquet:
        import fsspec
        import pyarrow.parquet as pq

        wanted = [c for c in dict.fromkeys([code_col, name_col, fg_col]) if c]
        # Read via an fsspec file handle + pyarrow so only the requested column
        # chunks are fetched over HTTP range requests (true column projection).
        # NOTE: pandas.read_parquet(<url string>) instead downloads the WHOLE file
        # via urllib (no projection, and rejected by some hosts) — avoid that.
        with fsspec.open(data, "rb") as f:
            pf = pq.ParquetFile(f)
            # functional_group_column is optional and may be absent from the data.
            available = set(pf.schema_arrow.names)
            cols = [c for c in wanted if c in available]
            df = pf.read(columns=cols).to_pandas()
        return EcosystemsGeoDataFrame(
            df,
            ecosystem_column=code_col,
            ecosystem_name_column=name_col,
            functional_group_column=fg_col,
        )

    # Non-parquet vector (e.g. GeoJSON): usually small, so a full read is fine.
    return Ecosystems.from_file(
        data,
        ecosystem_column=code_col,
        ecosystem_name_column=name_col,
        functional_group_column=fg_col,
    )
