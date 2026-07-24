// Client-side ecosystem COG viewer.
//
// Reads the published single-band uint8 *index* COG (ESRI:54034, nodata=255)
// directly in the browser over HTTP range requests and renders it with
// @developmentseed/deck.gl-geotiff's COGLayer, which GPU-warps the source CRS
// into web-mercator and picks the overview appropriate for the zoom level
// (true level-of-detail). THIS ecosystem's pixels (value === index) are drawn
// crimson; all other valid classes are muted grey; nodata is transparent.
//
// No build step: every dependency is imported from esm.sh at view time. This
// is the pattern proven in https://github.com/tylere/test-mystmd-deckgl-raster
// — including the worker blob-bootstrap below.
//
// One non-obvious bit: @developmentseed/geotiff's DecoderPool spawns Workers
// from esm.sh URLs, and browsers reject `new Worker(crossOriginUrl)`. Work
// around it with a same-origin blob URL whose only content is an `import` of
// the real esm.sh worker. The Worker constructor accepts the blob (same
// origin); the inner import is a CORS module fetch esm.sh allows, and the
// worker's own dependency tree resolves against its esm.sh URL.

import maplibregl from "https://esm.sh/maplibre-gl@4.7.1";
import { MapboxOverlay } from "https://esm.sh/@deck.gl/mapbox@9.3.0";
import { COGLayer } from "https://esm.sh/@developmentseed/deck.gl-geotiff@0.7.0";
import { DecoderPool, GeoTIFF } from "https://esm.sh/@developmentseed/geotiff@0.7.0";

const MAPLIBRE_CSS_URL = "https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css";
const GEOTIFF_WORKER_URL = "https://esm.sh/@developmentseed/geotiff@0.7.0/pool/worker";
const BASEMAP_STYLE = "https://basemaps.cartocdn.com/gl/positron-gl-style/style.json";

// Highlight palette (straight-alpha RGBA, 0..255).
const HIGHLIGHT = [220, 20, 60, 235]; // crimson: this ecosystem
const MUTED = [200, 200, 200, 130]; // grey: all other classes
const NODATA = 255; // to_raster stores nodata as the uint8 maximum

// ESRI:54034 (World Cylindrical Equal Area) as a proj4-shaped projection
// definition. @developmentseed/geotiff cannot parse this CRS from the COG's
// GeoKeys — the projection method (28, Cylindrical Equal Area) isn't in its
// switch and `geotiff.crs` throws "Unsupported coordinate transformation type:
// 28". We sidestep that by seeding the GeoTIFF's cached crs with the numeric
// code (see mount()) so COGLayer calls this resolver instead of the GeoKey
// parser. proj4 knows this projection only by the short name "cea"; a WKT
// string would fail because wkt-parser passes "Cylindrical_Equal_Area" through
// verbatim and proj4 has no alias for it.
const ESRI_54034_DEF = {
  title: "World_Cylindrical_Equal_Area",
  projName: "cea",
  ellps: "WGS84",
  a: 6378137.0,
  rf: 298.257223563,
  lat_ts: 0,
  long0: 0,
  x0: 0,
  y0: 0,
  units: "meter",
  to_meter: 1,
  datumCode: "wgs84",
  datum_params: [0, 0, 0, 0, 0, 0, 0],
};

// Shared worker pool across all viewers on the page.
const workerBootstrapBlobUrl = URL.createObjectURL(
  new Blob([`import "${GEOTIFF_WORKER_URL}";`], { type: "application/javascript" }),
);
const decoderPool = new DecoderPool({
  createWorker: () => new Worker(workerBootstrapBlobUrl, { type: "module" }),
});

// Inline passthrough shader module: samples the per-tile RGBA texture built on
// the CPU and discards fully-transparent (nodata) fragments. Modeled on the
// published CreateTexture module plus PaletteColormap's alpha discard, inlined
// here to avoid depending on a deep esm.sh subpath export.
const DrawRGBA = {
  name: "draw-rgba",
  inject: {
    "fs:#decl": `uniform sampler2D textureName;`,
    "fs:DECKGL_FILTER_COLOR": `
      color = texture(textureName, geometry.uv);
      if (color.a == 0.0) { discard; }
    `,
  },
  getUniforms: (props) => ({ textureName: props.textureName }),
};

// Resolve a numeric code to a projection definition. Our COGs are always
// ESRI:54034 (seeded onto the GeoTIFF in mount()), so return that def; no
// epsg.io lookup is needed.
async function esriResolver() {
  return ESRI_54034_DEF;
}

let _cssPromise = null;
function ensureMaplibreCSS() {
  // MapLibre's stylesheet is global (the map is not in a shadow root here), so
  // inject it once into <head>.
  if (_cssPromise) return _cssPromise;
  _cssPromise = new Promise((resolve) => {
    if (document.querySelector(`link[href="${MAPLIBRE_CSS_URL}"]`)) return resolve();
    const link = document.createElement("link");
    link.rel = "stylesheet";
    link.href = MAPLIBRE_CSS_URL;
    link.onload = () => resolve();
    link.onerror = () => resolve();
    document.head.appendChild(link);
  });
  return _cssPromise;
}

// Build a getTileData that uploads each tile as an rgba8unorm texture with the
// highlight/mute/transparent mapping already applied on the CPU.
function makeGetTileData(index) {
  const highlightVal = Number.isInteger(index) ? index : -1;
  return async function getTileData(image, options) {
    const { device, x, y, signal, pool } = options;
    const tile = await image.fetchTile(x, y, { boundless: false, pool, signal });
    const { array } = tile;
    const { width, height } = array;
    const src = array.data; // single-band uint8, pixel-interleaved
    const rgba = new Uint8Array(width * height * 4);
    for (let i = 0; i < src.length; i++) {
      const v = src[i];
      const o = i * 4;
      let c;
      if (v === NODATA) {
        continue; // leave (0,0,0,0) → discarded in shader
      } else if (v === highlightVal) {
        c = HIGHLIGHT;
      } else {
        c = MUTED;
      }
      rgba[o] = c[0];
      rgba[o + 1] = c[1];
      rgba[o + 2] = c[2];
      rgba[o + 3] = c[3];
    }
    const texture = device.createTexture({
      data: rgba,
      format: "rgba8unorm",
      width,
      height,
      sampler: { minFilter: "nearest", magFilter: "nearest" },
    });
    return { texture, byteLength: rgba.byteLength, width, height };
  };
}

// Build a getTileData that colors each pixel by its category, looking up
// `palette[value]` → [r,g,b,a]. `palette` is indexed by the raw pixel value
// (the ecosystem index); slot 0 and any value without an entry (or with alpha
// 0) render transparent, as does NODATA. Categories must use non-zero alpha or
// the DrawRGBA shader discards them.
function makeGetTileDataCategorical(palette) {
  return async function getTileData(image, options) {
    const { device, x, y, signal, pool } = options;
    const tile = await image.fetchTile(x, y, { boundless: false, pool, signal });
    const { array } = tile;
    const { width, height } = array;
    const src = array.data; // single-band uint8, pixel-interleaved
    const rgba = new Uint8Array(width * height * 4);
    for (let i = 0; i < src.length; i++) {
      const v = src[i];
      if (v === NODATA) continue; // leave (0,0,0,0) → discarded in shader
      const c = palette[v];
      if (!c || c[3] === 0) continue; // unknown/transparent class
      const o = i * 4;
      rgba[o] = c[0];
      rgba[o + 1] = c[1];
      rgba[o + 2] = c[2];
      rgba[o + 3] = c[3];
    }
    const texture = device.createTexture({
      data: rgba,
      format: "rgba8unorm",
      width,
      height,
      sampler: { minFilter: "nearest", magFilter: "nearest" },
    });
    return { texture, byteLength: rgba.byteLength, width, height };
  };
}

function renderTile(data) {
  return {
    renderPipeline: [{ module: DrawRGBA, props: { textureName: data.texture } }],
  };
}

/**
 * Mount the COG viewer into `el`.
 *
 * @param {HTMLElement} el   Container element (its height is set from opts).
 * @param {object} opts
 * @param {string} opts.url     Public COG URL (HTTP range readable + CORS).
 * @param {number} [opts.index] This ecosystem's index; its pixels are crimson
 *   (highlight mode). Ignored when `palette` is given.
 * @param {number[][]} [opts.palette] Categorical mode: colors indexed by pixel
 *   value → [r,g,b,a] (0..255). Each ecosystem index renders in its own color;
 *   values without an entry (and NODATA) are transparent.
 * @param {number} [opts.height] Map height in px (default 500).
 */
export default async function mount(el, opts) {
  const { url, index, palette } = opts || {};
  const height = Number(opts && opts.height) || 500;

  el.style.position = "relative";
  el.style.height = `${height}px`;
  el.style.width = "100%";

  await ensureMaplibreCSS();

  const mapDiv = document.createElement("div");
  Object.assign(mapDiv.style, { position: "absolute", inset: "0" });
  el.appendChild(mapDiv);

  const status = document.createElement("div");
  Object.assign(status.style, {
    position: "absolute", top: "8px", left: "8px", zIndex: "10",
    font: "12px/1.3 system-ui, sans-serif",
    background: "rgba(255,255,255,0.9)", color: "#333",
    padding: "6px 8px", borderRadius: "4px", maxWidth: "80%",
    whiteSpace: "pre-wrap",
  });
  status.textContent = "Loading ecosystem map…";
  el.appendChild(status);
  const showError = (msg) => {
    if (!el.contains(status)) el.appendChild(status);
    status.style.background = "#fee";
    status.style.color = "#900";
    status.textContent = String(msg);
  };

  if (!url) {
    showError("cog-viewer: no COG url provided.");
    return;
  }

  const map = new maplibregl.Map({
    container: mapDiv,
    style: BASEMAP_STYLE,
    center: [0, 0],
    zoom: 1,
  });

  try {
    await new Promise((r) => map.on("load", r));

    const overlay = new MapboxOverlay({
      onError: (e) => showError("Layer error: " + (e?.message ?? String(e))),
      layers: [],
    });
    map.addControl(overlay);

    // Open the COG ourselves so we can seed the cached CRS with the numeric
    // code 54034 BEFORE COGLayer reads `geotiff.crs`. Left to itself the crs
    // getter parses the ESRI:54034 GeoKeys and throws (projection method 28 is
    // unsupported); seeding `_crs` short-circuits that getter and makes
    // COGLayer resolve the CRS via esriResolver instead.
    const gt = await GeoTIFF.fromUrl(url);
    gt._crs = 54034;

    const layer = new COGLayer({
      id: "ecosystem-cog",
      geotiff: gt,
      pool: decoderPool,
      epsgResolver: esriResolver,
      getTileData: palette ? makeGetTileDataCategorical(palette) : makeGetTileData(index),
      renderTile,
      onGeoTIFFLoad: (_geotiff, options) => {
        const b = options.geographicBounds;
        if (Number.isFinite(b.west) && Number.isFinite(b.east) && b.east > b.west) {
          map.fitBounds([[b.west, b.south], [b.east, b.north]], { padding: 20, duration: 0 });
        }
        status.remove();
      },
      onTileError: (e) => showError("Tile error: " + (e?.message ?? String(e))),
    });
    overlay.setProps({ layers: [layer] });
  } catch (e) {
    showError(e?.message ?? String(e));
  }

  return () => {
    try { map.remove(); } catch { /* ignore */ }
    el.replaceChildren();
  };
}
