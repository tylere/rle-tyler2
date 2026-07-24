"""Regression tests for the ecosystem_source raster guard (see issue #15).

`ecosystem_source.data` must be a vector source; a raster COG (.tif) otherwise
fails deep in geopandas/GDAL with a cryptic pyogrio error. `ensure_vector_source`
turns that into an early, actionable SystemExit.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from _config import ensure_vector_source  # noqa: E402


def test_raster_source_raises():
    with pytest.raises(SystemExit) as exc:
        ensure_vector_source("gs://bucket/ecosystems/x_10m.tif")
    assert "ecosystem_raster.cog_url" in str(exc.value)


def test_raster_with_query_and_uppercase_suffix():
    with pytest.raises(SystemExit):
        ensure_vector_source("https://host/ECO.TIFF?token=abc")


def test_vector_source_passes_through():
    url = "https://host/data/eco.parquet"
    assert ensure_vector_source(url) == url
