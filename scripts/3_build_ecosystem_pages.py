"""Generate Quarto pages for each ecosystem from template notebooks.

For each config/ecosystems/*/ecosystem.yaml, creates two pages under
content/3_ecosystem_assessments/{ecosystem_code}/:
  - {ecosystem_code}.qmd         — from templates/assessment.qmd
  - {ecosystem_code}_crit_b.qmd  — from templates/crit_b.qmd

Each generated file is a copy of the template with the ecosystem_code
line replaced. Templates are renderable on their own for development.

Existing pages are overwritten in place; nothing is deleted. Stale pages for
ecosystems no longer in config/ecosystems/ are left untouched (build_ecosystems.py
errors on such orphans rather than deleting them).
"""

import argparse
import hashlib
import re
import shutil
import sys
from pathlib import Path

import yaml

CACHE_DIR = Path(".cache")
CONFIG_DIR = Path("config/ecosystems")
COUNTRY_CONFIG = Path("config/country_config.yaml")
OUTPUT_DIR = Path("content/3_ecosystem_assessments")
TEMPLATE_DIR = Path("templates")
QUARTO_YML = Path("_quarto.yml")

# Pattern matching the ecosystem_code assignment line in templates.
_CODE_PATTERN = re.compile(r"^(ecosystem_code\s*=\s*)['\"].*['\"]", re.MULTILINE)

# Default ecosystem code and name used in templates.
_DEFAULT_CODE = "M1.1.1"
_DEFAULT_NAME = "Null Island Marine Shelf"


def _write_if_changed(path: Path, text: str) -> bool:
    """Write ``text`` to ``path`` only when it differs from what is on disk.

    Skipping unchanged files preserves their mtime. ``quarto preview``'s
    serveFiles re-renders a page only when its input is newer than its output
    (an mtime comparison), so rewriting byte-identical pages every build would
    force a full-book re-render on every preview. Leaving them untouched keeps
    preview fast and avoids the large render batch that triggers the
    fileRenderHash crash.
    """
    if path.exists() and path.read_text() == text:
        print(f"  Unchanged {path}")
        return False
    path.write_text(text)
    print(f"  Wrote {path}")
    return True


def _replace_ecosystem_code(template_text: str, code: str, name: str,
                            config_hash: str | None = None) -> str:
    """Replace the ecosystem_code assignment and heading text in a template.

    When ``config_hash`` is given, it is embedded as a comment on the
    ``ecosystem_code`` line. The generated page is otherwise byte-identical no
    matter what the source ``ecosystem.yaml`` contains, so without this Quarto's
    ``freeze: auto`` cache would serve a stale render after the config is edited.
    Threading the config hash into the page invalidates that cache on any edit.
    """
    replacement = rf"\g<1>'{code}'"
    if config_hash is not None:
        replacement += f"  # source-config sha256: {config_hash}"
    text = _CODE_PATTERN.sub(replacement, template_text)
    text = text.replace(f"{_DEFAULT_NAME} ({_DEFAULT_CODE})", f"{name} ({code})")
    text = text.replace(f"{_DEFAULT_CODE} Criterion B", f"{code} Criterion B")
    return text


def _update_quarto_yml(eco_configs: list[Path]) -> None:
    """Update _quarto.yml to include ecosystem assessment pages.

    Inserts ecosystem pages before 'references.qmd' in the chapters list.
    Removes any previously inserted ecosystem assessment entries first.
    """
    text = QUARTO_YML.read_text()

    # Order parts by ecosystem index so the sidebar reads 1, 2, 3, ... N, matching
    # the page headings. eco_configs is directory-name sorted (case-sensitive), which
    # disagrees with the index order (assigned from the natural/case-insensitive
    # enumeration in unique_ecosystems()). Ecosystems without an index (index not
    # built yet) sort last, by name.
    ecos = []
    for eco_path in eco_configs:
        with open(eco_path) as f:
            ecos.append(yaml.safe_load(f))
    ecos.sort(
        key=lambda e: (
            e.get("index") is None,
            e.get("index") if e.get("index") is not None else 0,
            str(e.get("ecosystem_name", e["global_classification"])),
        )
    )

    # Build per-ecosystem part entries
    new_entries = []
    for eco in ecos:
        code = eco["global_classification"]
        name = eco.get("ecosystem_name", code)
        index = eco.get("index")
        prefix = f"{OUTPUT_DIR}/{code}/{code}"
        # When there is no separate code (code == name), show just the name
        # rather than a redundant "Name - Name". Prefix with the ecosystem index
        # so the sidebar matches the page heading ("{index} · {name}").
        base = name if str(code) == str(name) else f"{code} - {name}"
        label = f"{index} · {base}" if index is not None else base
        new_entries.append(f'    - part: "{label}"')
        new_entries.append(f"      chapters:")
        new_entries.append(f"        - {prefix}.qmd")
        new_entries.append(f"        - {prefix}_crit_b.qmd")

    # Remove existing ecosystem assessment blocks
    lines = text.splitlines()
    filtered = []
    i = 0
    while i < len(lines):
        if lines[i].strip().startswith("- part:") and i + 1 < len(lines):
            j = i + 1
            while j < len(lines) and (
                lines[j].strip() in ("chapters:", "contents:")
                or f"{OUTPUT_DIR}/" in lines[j]
            ):
                j += 1
            if j > i + 1:
                i = j
                continue
        if f"{OUTPUT_DIR}/" in lines[i]:
            i += 1
            continue
        filtered.append(lines[i])
        i += 1

    # Insert new entries before references.qmd
    result = []
    for ln in filtered:
        if ln.strip() == "- references.qmd":
            result.extend(new_entries)
        result.append(ln)

    new_text = "\n".join(result) + "\n"
    if new_text == text:
        print(f"  Unchanged {QUARTO_YML}")
    else:
        QUARTO_YML.write_text(new_text)
        print(f"  Updated {QUARTO_YML}")


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()

    if CACHE_DIR.exists():
        shutil.rmtree(CACHE_DIR)
        print(f"  Cleared {CACHE_DIR}/")

    # Pages are (over)written in place per ecosystem; nothing is deleted. Removing
    # stale pages for ecosystems no longer in config/ecosystems/ is the caller's
    # responsibility (build_ecosystems.py errors on such orphans rather than
    # deleting them).
    assessment_template = (TEMPLATE_DIR / "assessment.qmd").read_text()
    crit_b_template = (TEMPLATE_DIR / "crit_b.qmd").read_text()

    eco_configs = sorted(CONFIG_DIR.glob("*/ecosystem.yaml"))
    if not eco_configs:
        print(f"No ecosystem configs found in {CONFIG_DIR}/")
        return

    # The assessment page reads country_config.yaml too (ecosystem_source and the
    # ecosystem_raster COG URL), so fold it into the freeze hash — otherwise
    # editing the COG config wouldn't re-execute the page under freeze: auto.
    country_config_bytes = COUNTRY_CONFIG.read_bytes() if COUNTRY_CONFIG.exists() else b""

    for eco_path in eco_configs:
        with open(eco_path) as f:
            eco = yaml.safe_load(f)
        if not isinstance(eco, dict) or "global_classification" not in eco:
            sys.exit(
                f"ERROR: {eco_path} is empty or not a valid ecosystem config "
                f"(expected a YAML mapping with 'global_classification'). "
                f"Fix or remove it."
            )
        # Hash the source config so freeze re-executes the assessment page when it
        # is edited. The page reads both this ecosystem.yaml and country_config.yaml
        # (ecosystem_source + ecosystem_raster), so hash both. crit_b derives from
        # the spatial data, so it gets no hash (avoids needless AOO/EOO reruns).
        config_hash = hashlib.sha256(
            eco_path.read_bytes() + country_config_bytes
        ).hexdigest()

        code = eco["global_classification"]
        name = eco.get("ecosystem_name", code)
        out_dir = OUTPUT_DIR / code
        out_dir.mkdir(parents=True, exist_ok=True)

        for page_name, template, page_hash in [
            (f"{code}.qmd", assessment_template, config_hash),
            (f"{code}_crit_b.qmd", crit_b_template, None),
        ]:
            page_path = out_dir / page_name
            _write_if_changed(
                page_path,
                _replace_ecosystem_code(template, code, name, page_hash),
            )

    # Update _quarto.yml chapters list
    _update_quarto_yml(eco_configs)

    print(f"\nDone. Pages are in {OUTPUT_DIR}/")


if __name__ == "__main__":
    main()
