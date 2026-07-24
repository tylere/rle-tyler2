"""Update the ecosystem data source in country_config.yaml."""

import argparse

import yaml

from _config import ensure_vector_source

CONFIG_PATH = "config/country_config.yaml"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "data",
        help="Ecosystem data source (EE asset ID, gs:// URI, or local file path)",
    )
    args = parser.parse_args()

    # Reject a raster value up front so the misconfiguration is caught here
    # rather than deep in geopandas/GDAL when a later step reads the source.
    ensure_vector_source(args.data)

    with open(CONFIG_PATH) as f:
        config = yaml.safe_load(f)

    config["ecosystem_source"]["data"] = args.data

    with open(CONFIG_PATH, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)

    print(f"Updated {CONFIG_PATH}: ecosystem_source.data = {args.data}")


if __name__ == "__main__":
    main()
