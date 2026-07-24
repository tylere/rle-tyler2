"""Derive and persist the ecosystem class index.

The ecosystem "index" is the 1-based position of an ecosystem code in
``Ecosystems.unique_ecosystems()`` — the same natural-sorted, deduplicated order
that ``rle.core.Ecosystems.to_raster`` uses to assign COG pixel values. Because
it is a pure function of ``ecosystem_source``, it is a *derived* artifact:
regenerated idempotently on every render so it stays consistent with both the
COG and the per-ecosystem configs.

This module:
  1. writes the canonical ``config/ecosystems/index.csv`` (``index,code,name``),
     the legend for interpreting COG pixel values; and
  2. backfills an ``index:`` field into every existing
     ``config/ecosystems/{code}/ecosystem.yaml`` (additive; never touches other
     fields), so each ecosystem page can reference its index.

Both steps are idempotent — running twice produces no further changes.

Usage:
    python scripts/build_ecosystem_index.py
"""

import argparse
import csv
from pathlib import Path

import yaml

from _config import load_country_config, load_ecosystems_lite

CONFIG_PATH = Path("config/country_config.yaml")
ECOSYSTEMS_DIR = Path("config/ecosystems")
INDEX_CSV = ECOSYSTEMS_DIR / "index.csv"


def compute_index(config_path: Path = CONFIG_PATH) -> list[tuple[int, str, str]]:
    """Return [(index, code, name), ...] in natural-sorted code order.

    ``index`` is 1-based and matches the COG pixel values produced by
    ``Ecosystems.to_raster``.
    """
    config = load_country_config(config_path)
    source = config["ecosystem_source"]

    # ecosystem_code_column is optional: fall back to the name column so
    # ecosystems are enumerated (and indexed) by name when no code exists.
    ecosystem_column = source.get("ecosystem_code_column") or source.get("ecosystem_name_column")
    if ecosystem_column is None:
        raise SystemExit(
            "ecosystem_source needs ecosystem_code_column or ecosystem_name_column "
            "in config/country_config.yaml"
        )

    # Column-projected read (no geometry) so a large national source is not
    # fully downloaded just to enumerate ecosystems.
    eco = load_ecosystems_lite(source)

    codes = eco.unique_ecosystems()  # naturally sorted, deduplicated

    # Look up a display name per code (falls back to the code itself).
    name_column = source.get("ecosystem_name_column")
    names: dict[str, str] = {}
    if name_column is not None:
        gdf = eco.to_geodataframe()
        if name_column in gdf.columns:
            for code in codes:
                rows = gdf[gdf[eco.ecosystem_column] == code]
                if not rows.empty:
                    names[code] = str(rows[name_column].iloc[0])

    return [(i, code, names.get(code, code)) for i, code in enumerate(codes, start=1)]


def write_index_csv(rows: list[tuple[int, str, str]], path: Path = INDEX_CSV) -> None:
    """Write the canonical index.csv (index,code,name)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["index", "code", "name"])
        writer.writerows(rows)
    print(f"Wrote {path} ({len(rows)} ecosystems)")


def _with_index(eco: dict, index: int) -> dict:
    """Return a copy of an ecosystem.yaml dict with ``index`` set.

    Placed right after ``global_classification`` when present (to co-locate the
    code↔index pairing), otherwise appended. Preserves the order of all other
    keys.
    """
    result: dict = {}
    inserted = False
    for key, value in eco.items():
        if key == "index":
            continue  # drop any existing index; we re-add it in the right place
        result[key] = value
        if key == "global_classification":
            result["index"] = index
            inserted = True
    if not inserted:
        result["index"] = index
    return result


def backfill_ecosystem_yamls(rows: list[tuple[int, str, str]]) -> int:
    """Add/fix the ``index:`` field in each config/ecosystems/{code}/ecosystem.yaml.

    Returns the number of files changed. Idempotent.
    """
    code_to_index = {code: i for i, code, _ in rows}
    changed = 0
    for eco_file in sorted(ECOSYSTEMS_DIR.glob("*/ecosystem.yaml")):
        code = eco_file.parent.name
        index = code_to_index.get(code)
        if index is None:
            print(f"  WARNING: {eco_file} has no matching ecosystem_source code — skipping")
            continue
        with open(eco_file) as f:
            eco = yaml.safe_load(f)
        if eco.get("index") == index:
            continue  # already correct
        eco = _with_index(eco, index)
        with open(eco_file, "w") as f:
            yaml.dump(eco, f, default_flow_style=False, sort_keys=False,
                      allow_unicode=True)
        changed += 1
        print(f"  Set index: {index} in {eco_file}")
    return changed


def ensure_indices(config_path: Path = CONFIG_PATH) -> list[tuple[int, str, str]]:
    """Regenerate index.csv and backfill index: into per-ecosystem configs."""
    rows = compute_index(config_path)
    write_index_csv(rows)
    changed = backfill_ecosystem_yamls(rows)
    if changed:
        print(f"Updated index: in {changed} ecosystem config(s)")
    else:
        print("All ecosystem configs already have the correct index.")
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=CONFIG_PATH,
        help=f"Path to country config (default: {CONFIG_PATH})",
    )
    args = parser.parse_args()
    ensure_indices(args.config)


if __name__ == "__main__":
    main()
