"""Quarto post-render step: share anywidget bundles across the whole book.

lonboard maps are anywidget widgets. Quarto embeds each map's JavaScript
(`_esm`, ~3.4 MB) and CSS (`_css`, ~0.34 MB) *inline* in the page's
`application/vnd.jupyter.widget-state+json` block — once per map. Those bytes
are identical for every map on every ecosystem page (same lonboard version), so
a book of ~34 ecosystems carries ~100 copies of the same bundle (~350 MB) and
each `*_crit_b.html` is ~11.6 MB.

This step rewrites each rendered HTML page so the bundle is stored once as a
shared asset and every map references it by URL. anywidget's frontend loader
imports `_esm`/`_css` from a URL when the trait is an absolute http(s) href
(``if (isHref(esm)) return await import(esm)``), so:

  1. Each unique inline `_esm`/`_css` is written once to
     ``<output>/site_libs/anywidget-assets/<hash>.<ext>`` (shared across pages).
  2. In the page's widget-state JSON, the inline value is replaced with a
     placeholder ``anywidget-ref:<page-relative-path>``.
  3. A tiny inline shim (injected right after the state block, so it runs
     synchronously before the jupyter widget manager reads the state on
     DOMContentLoaded) rewrites each placeholder to
     ``new URL(<page-relative-path>, document.baseURI).href`` — an absolute URL
     that satisfies `isHref` and resolves correctly under localhost preview,
     ``file://``, and the GitHub Pages subpath alike.

The result: `*_crit_b.html` drops from ~11.6 MB to ~0.4 MB, the bundle is a
single ~3.4 MB file for the entire book, and the maps render unchanged.

Quarto invokes this after rendering, passing ``QUARTO_PROJECT_OUTPUT_DIR`` and
``QUARTO_PROJECT_OUTPUT_FILES``. Idempotent: pages already rewritten (detected by
the shim marker) are skipped.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

STATE_TYPE = "application/vnd.jupyter.widget-state+json"
ASSET_SUBPATH = "site_libs/anywidget-assets"
REF_PREFIX = "anywidget-ref:"
SHIM_MARKER = "data-anywidget-dedup"
MIN_INLINE = 4096  # only externalize sizable inline bundles
FIELDS = {"_esm": "js", "_css": "css"}

_STATE_OPEN = re.compile(
    r'<script[^>]*type=["\']' + re.escape(STATE_TYPE) + r'["\'][^>]*>'
)

# Injected verbatim after the widget-state <script>. Runs synchronously during
# parse (before DOMContentLoaded, when the widget manager reads the state) and
# turns each `anywidget-ref:` placeholder back into an absolute URL.
_SHIM = (
    '\n<script ' + SHIM_MARKER + '="1">\n'
    "(function(){try{"
    "var s=document.querySelector('script[type=\"" + STATE_TYPE + "\"]');"
    "if(!s)return;var state=JSON.parse(s.textContent);var m=state.state||{};"
    "for(var k in m){var st=m[k]&&m[k].state;if(!st)continue;"
    "['_esm','_css'].forEach(function(f){var v=st[f];"
    "if(typeof v==='string'&&v.indexOf('" + REF_PREFIX + "')===0){"
    "st[f]=new URL(v.slice(" + str(len(REF_PREFIX)) + "),document.baseURI).href;}});}"
    "s.textContent=JSON.stringify(state);"
    "}catch(e){if(window.console)console.error('anywidget dedup shim failed',e);}})();\n"
    "</script>\n"
)


def _target_files(output_dir: Path) -> list[Path]:
    listed = os.environ.get("QUARTO_PROJECT_OUTPUT_FILES", "").strip()
    if listed:
        files = []
        for line in listed.splitlines():
            line = line.strip()
            if not line:
                continue
            p = Path(line)
            if not p.is_absolute():
                p = output_dir / p
            if p.suffix.lower() in (".html", ".htm") and p.exists():
                files.append(p)
        if files:
            return files
    return [p for p in output_dir.rglob("*.html")]


def _rel_prefix(page: Path, output_dir: Path) -> str:
    rel_dir = os.path.relpath(page.parent, output_dir)
    depth = 0 if rel_dir == "." else len(rel_dir.split(os.sep))
    return "../" * depth


def process_html(path: Path, output_dir: Path, assets_dir: Path) -> bool:
    html = path.read_text(encoding="utf-8")
    if SHIM_MARKER in html:
        return False  # already rewritten
    m = _STATE_OPEN.search(html)
    if not m:
        return False
    body_start = m.end()
    body_end = html.find("</script>", body_start)
    if body_end == -1:
        return False
    close_end = body_end + len("</script>")

    try:
        state = json.loads(html[body_start:body_end])
    except json.JSONDecodeError:
        return False

    prefix = _rel_prefix(path, output_dir)
    changed = False
    for model in state.get("state", {}).values():
        model_state = model.get("state")
        if not isinstance(model_state, dict):
            continue
        for field, ext in FIELDS.items():
            value = model_state.get(field)
            if (
                not isinstance(value, str)
                or value.startswith(REF_PREFIX)
                or len(value) < MIN_INLINE
            ):
                continue
            digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]
            asset = assets_dir / f"{digest}.{ext}"
            if not asset.exists():
                asset.parent.mkdir(parents=True, exist_ok=True)
                # Write bytes (not text) so the served module is byte-identical
                # to the original inline bundle — no newline translation.
                asset.write_bytes(value.encode("utf-8"))
            model_state[field] = f"{REF_PREFIX}{prefix}{ASSET_SUBPATH}/{digest}.{ext}"
            changed = True

    if not changed:
        return False

    # Escape `</` so the embedded JSON can't prematurely close the <script>.
    new_body = json.dumps(state).replace("</", "<\\/")
    new_html = html[:body_start] + new_body + html[body_end:close_end] + _SHIM + html[close_end:]
    path.write_text(new_html, encoding="utf-8")
    return True


def main() -> None:
    output_dir = Path(os.environ.get("QUARTO_PROJECT_OUTPUT_DIR", "_book")).resolve()
    if not output_dir.is_dir():
        print(f"  anywidget-dedup: output dir {output_dir} not found; skipping")
        return
    assets_dir = output_dir / ASSET_SUBPATH

    rewritten = 0
    for page in _target_files(output_dir):
        try:
            if process_html(page, output_dir, assets_dir):
                rewritten += 1
                print(f"  anywidget-dedup: shared bundle in {page.relative_to(output_dir)}")
        except OSError as exc:
            print(f"  anywidget-dedup: skipped {page} ({exc})")

    if rewritten:
        shared = sorted(assets_dir.glob("*")) if assets_dir.is_dir() else []
        print(
            f"  anywidget-dedup: rewrote {rewritten} page(s); "
            f"{len(shared)} shared asset(s) in {ASSET_SUBPATH}/"
        )


if __name__ == "__main__":
    main()
