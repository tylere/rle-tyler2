"""Rebuild the generated ecosystem layer before rendering — non-destructively.

`config/ecosystems/` is the source of truth (hand-editable). The assessment pages
under `content/3_ecosystem_assessments/` and the ecosystem chapters in `_quarto.yml`
are DERIVED from it. This keeps them consistent before Quarto reads the book config,
but it never deletes tracked content to do so: when regenerating would remove files,
it stops with an actionable error so you delete them deliberately.

Behaviour:
  * `config/ecosystems/` empty -> scaffold from `ecosystem_source` (script 2) and
    record the source in `config/ecosystems/.source.yaml`.
  * `config/ecosystems/` populated but generated from a different `ecosystem_source`
    (or provenance unrecorded) -> out-of-sync error (touches nothing).
  * `content/3_ecosystem_assessments/` contains pages not backed by `config/ecosystems/`
    -> error, because regenerating would delete them (touches nothing).
  * otherwise (in sync) -> regenerate the derived pages in place (script 3) and rewrite
    the `_quarto.yml` chapter list.

Run from the project root (the pixi task working directory).
"""

import subprocess
import sys
from pathlib import Path

import yaml

CONFIG_PATH = Path("config/country_config.yaml")
ECOSYSTEMS_DIR = Path("config/ecosystems")
MANIFEST_PATH = ECOSYSTEMS_DIR / ".source.yaml"
PAGES_DIR = Path("content/3_ecosystem_assessments")
SCRIPTS_DIR = Path("scripts")


def _run(*args):
    """Run a build script, exiting cleanly on failure (the child already
    printed its own error) instead of raising a noisy CalledProcessError."""
    try:
        subprocess.run([sys.executable, *args], check=True)
    except subprocess.CalledProcessError as exc:
        sys.exit(exc.returncode)


def _current_source():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f).get("ecosystem_source")


def _recorded_source():
    if MANIFEST_PATH.exists():
        with open(MANIFEST_PATH) as f:
            return yaml.safe_load(f)
    return None


def _config_codes():
    return {p.parent.name for p in ECOSYSTEMS_DIR.glob("*/ecosystem.yaml")}


def _page_codes():
    return {p.name for p in PAGES_DIR.glob("*") if p.is_dir()}


def _source_drift_error(recorded, current):
    recorded_data = recorded.get("data") if isinstance(recorded, dict) else "an unrecorded source"
    current_data = current.get("data") if isinstance(current, dict) else "none"
    sys.exit(
        f"\nERROR: {ECOSYSTEMS_DIR}/ is out of sync with ecosystem_source in "
        f"{CONFIG_PATH}.\n"
        f"    {ECOSYSTEMS_DIR}/ was generated from: {recorded_data}\n"
        f"    {CONFIG_PATH} now specifies:      {current_data}\n\n"
        f"{ECOSYSTEMS_DIR}/*/ecosystem.yaml may contain hand-written assessments, so it\n"
        "is not regenerated automatically. To rebuild it from the new source:\n"
        f"  1. Commit your work:  git add {ECOSYSTEMS_DIR} {PAGES_DIR} && git commit -m \"Save assessments\"\n"
        f"  2. Delete them:       rm -rf {ECOSYSTEMS_DIR} {PAGES_DIR}\n"
        "  3. Re-run:            pixi run render      (re-scaffolds from the new ecosystem_source)\n"
    )


def _orphan_pages_error(orphans):
    listed = ", ".join(sorted(orphans))
    sys.exit(
        f"\nERROR: {PAGES_DIR}/ contains assessment pages not backed by "
        f"{ECOSYSTEMS_DIR}/:\n"
        f"    {listed}\n\n"
        f"These pages are generated from {ECOSYSTEMS_DIR}/; regenerating would delete\n"
        "them, so the render is stopped instead of removing them. To proceed:\n"
        f"  1. Commit your work:  git add {PAGES_DIR} && git commit -m \"Save assessment pages\"\n"
        f"  2. Delete them:       rm -rf {PAGES_DIR}\n"
        "  3. Re-run:            pixi run render      (regenerates pages for the current ecosystems)\n"
    )


def main():
    source = _current_source()

    # --- config/ecosystems: source of truth (scaffold if empty, else check provenance)
    if not any(ECOSYSTEMS_DIR.glob("*/ecosystem.yaml")):
        print(f"{ECOSYSTEMS_DIR}/ is empty — scaffolding from ecosystem_source...")
        _run(str(SCRIPTS_DIR / "2_build_ecosystems_config.py"))
        MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(MANIFEST_PATH, "w") as f:
            yaml.safe_dump(source, f, sort_keys=False, allow_unicode=True)
        print(f"Recorded ecosystem_source provenance in {MANIFEST_PATH}")
    elif _recorded_source() != source:
        _source_drift_error(_recorded_source(), source)

    # --- ecosystem index: derived from ecosystem_source, regenerated every run.
    # Writes config/ecosystems/index.csv and backfills index: into each
    # ecosystem.yaml (idempotent, additive). Kept current regardless of whether
    # the scaffolds were (re)generated above.
    _run(str(SCRIPTS_DIR / "build_ecosystem_index.py"))

    # --- content/3: derived, but never delete stale pages implicitly
    orphans = _page_codes() - _config_codes()
    if orphans:
        _orphan_pages_error(orphans)

    _run(str(SCRIPTS_DIR / "3_build_ecosystem_pages.py"))


if __name__ == "__main__":
    main()
