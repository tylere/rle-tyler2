"""Generate per-ecosystem config files from the country ecosystem source.

Reads config/country_config.yaml, loads the ecosystem data, and creates
a scaffold YAML file for each unique ecosystem under
config/ecosystems/{ecosystem_code}/ecosystem.yaml.

The functional-group column is optional: when it is not configured (or not
present in the data), each scaffold's ``functional_group`` is left as "N/A".

Existing files are not overwritten unless --overwrite is passed.
"""

import argparse
import shutil
from pathlib import Path

import yaml

from _config import load_country_config, load_ecosystems_lite

CACHE_DIR = Path(".cache")
ECOSYSTEMS_DIR = Path("config/ecosystems")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "max_ecosystems", type=int, nargs="?", default=None,
        help="Maximum number of ecosystems to generate config files for "
             "(default: all ecosystems in the data)",
    )
    parser.add_argument(
        "--overwrite", action="store_true",
        help="Overwrite existing ecosystem config files",
    )
    args = parser.parse_args()

    if CACHE_DIR.exists():
        shutil.rmtree(CACHE_DIR)
        print(f"  Cleared {CACHE_DIR}/")

    config = load_country_config()

    country_name = config["country_name"]
    source = config["ecosystem_source"]

    # ecosystem_code_column is optional: when absent, identify/enumerate
    # ecosystems by the name column (the index then serves as the numeric handle).
    ecosystem_column = source.get("ecosystem_code_column") or source.get("ecosystem_name_column")
    if ecosystem_column is None:
        raise SystemExit(
            "ecosystem_source needs ecosystem_code_column or ecosystem_name_column "
            "in config/country_config.yaml"
        )
    ecosystem_name_column = source.get("ecosystem_name_column")
    functional_group_column = source.get("functional_group_column")

    print(f"Loading ecosystem data from {source.get('optimized_data') or source['data']}...")
    # Column-projected read (no geometry) so a large national source is not
    # fully downloaded just to enumerate ecosystems and read their names/groups.
    eco = load_ecosystems_lite(source)
    gdf = eco.to_geodataframe()

    # The functional-group column is used only when it is both configured and
    # present in the data; otherwise every scaffold gets "N/A".
    has_functional_group = (
        functional_group_column is not None
        and functional_group_column in gdf.columns
    )
    has_name = (
        ecosystem_name_column is not None
        and ecosystem_name_column in gdf.columns
    )

    # Naturally sorted, de-duplicated ecosystem codes. The 1-based position in
    # this list is the ecosystem "index" — the same value the COG encodes for
    # each pixel (see rle.core.Ecosystems.to_raster / build_ecosystem_index.py).
    codes = eco.unique_ecosystems()
    code_to_index = {code: i for i, code in enumerate(codes, start=1)}
    if args.max_ecosystems is not None:
        codes = codes[:args.max_ecosystems]
    print(f"Generating config for {len(codes)} ecosystems...")

    overwrite = args.overwrite
    if not overwrite and ECOSYSTEMS_DIR.exists() and any(ECOSYSTEMS_DIR.iterdir()):
        response = input(
            f"  Existing config files found in {ECOSYSTEMS_DIR}/. "
            f"Overwrite all? [y/N] "
        )
        if response.lower() != 'y':
            print("  Aborting.")
            return
        overwrite = True

    if overwrite and ECOSYSTEMS_DIR.exists():
        shutil.rmtree(ECOSYSTEMS_DIR)
        print(f"  Cleared {ECOSYSTEMS_DIR}/")

    for ecosystem_code in codes:
        rows = gdf[gdf[ecosystem_column] == ecosystem_code]

        functional_group = "N/A"
        if has_functional_group and not rows.empty:
            functional_group = str(rows[functional_group_column].iloc[0])

        eco_name = ""
        if has_name and not rows.empty:
            eco_name = str(rows[ecosystem_name_column].iloc[0])

        eco_dir = ECOSYSTEMS_DIR / str(ecosystem_code)
        eco_file = eco_dir / "ecosystem.yaml"
        eco_dir.mkdir(parents=True, exist_ok=True)

        scaffold = {
            "ecosystem_name": eco_name,
            "country_name": country_name,
            "authors": ["John Smith", "Jane Smith"],
            "biome": "TODO",
            "functional_group": functional_group,
            "global_classification": ecosystem_code,
            "index": code_to_index[ecosystem_code],
            "iucn_status": "TODO",
            "description": "TODO",
            "distribution": "TODO",
            "characteristic_native_biota": "TODO",
            "abiotic_environment": "TODO",
            "key_processes_and_interactions": "TODO",
            "major_threats": "TODO",
            "ecosystem_collapse_definition": "TODO",
            "assessment_summary": "TODO",
            "criteria_status": {
                "A": {"A1": "NE", "A2a": "NE", "A2b": "NE", "A3": "NE"},
                "B": {"B1": "NE", "B2": "NE", "subcriteria": "NE", "B3": "NE"},
                "C": {"C1": "NE", "C2a": "NE", "C2b": "NE", "C3": "NE"},
                "D": {"D1": "NE", "D2a": "NE", "D2b": "NE", "D3": "NE"},
                "E": {"E": "NE"},
            },
            "assessment_outcome": "TODO",
            "year_published": "TODO",
            "date_assessed": "TODO",
            "assessment_credits": {
                "assessed_by": "TODO",
                "reviewed_by": "TODO",
                "contributions_by": "TODO",
            },
            "criterion_a_description": "TODO",
            "criterion_b_description": "TODO",
            "criterion_c_description": "TODO",
            "criterion_d_description": "TODO",
            "criterion_e_description": "TODO",
        }

        with open(eco_file, "w") as f:
            yaml.dump(scaffold, f, default_flow_style=False, sort_keys=False,
                      allow_unicode=True)

        print(f"  Created {eco_file}")

    print(f"\nDone. Ecosystem configs are in {ECOSYSTEMS_DIR}/")


if __name__ == "__main__":
    main()
