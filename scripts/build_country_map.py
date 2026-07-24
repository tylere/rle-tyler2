"""
Generate country map images for ecosystem assessments.

This script checks if country_map.png exists in the images folder.
If not, it uses gee-redlist-python to create a PNG map and save it.
"""

from pathlib import Path

import yaml

from rle.gee.map import create_country_map

# Configuration
IMAGES_DIR = Path("images")
MAP_FILENAME = "country_map.png"
map_path = IMAGES_DIR / MAP_FILENAME

def load_yaml(yaml_path):
    """Load YAML configuration file."""
    with open(yaml_path, 'r') as f:
        return yaml.safe_load(f)

def check_map_exists() -> bool:
    """Check if the country map already exists."""
    return map_path.exists()


def create_map_for_country(country_code):
    """Create a country map using gee-redlist-python."""
    print(f"Creating country map: {map_path}")
    map_image = create_country_map(
        country_code=country_code,
        output_path=map_path,
        show_stock_img=False,
    )
    print(f'{map_image=}')

    print(f"✓ Country map saved to: {map_path}")
    return map_path


def main():
    """Main function to check and create country map if needed."""
    
    site_config = Path('_quarto.yml')
    site_data = load_yaml(site_config)
    country_code = site_data['country-code']

    if check_map_exists():
        print(f"✓ Country map already exists: {map_path}")
    else:
        print(f"Country map not found: {map_path}")
        create_map_for_country(country_code=country_code)

if __name__ == "__main__":
    main()
