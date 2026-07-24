"""Rasterize the country ecosystem vector to a COG and store it in GCS.

Reads ``config/country_config.yaml``, rasterizes the ``ecosystem_source`` vector
data to a single-band categorical Cloud-Optimized GeoTIFF (COG) in the
``ESRI:54034`` projection used by the AOO/EOO calculations, and uploads it to a
public Cloud Storage bucket in the assessment's GCP project. The resulting
public URL is recorded back into ``config/country_config.yaml`` under
``ecosystem_raster``.

This is a manual, one-shot step (run it when the ecosystem data changes) rather
than part of the render pipeline: rasterizing + uploading is slow and needs GCP
write credentials. Existing COGs are skipped unless ``--force`` is passed.

Usage:
    python scripts/rasterize_ecosystem_to_cog.py --project my-gcp-project
    python scripts/rasterize_ecosystem_to_cog.py --resolution 10 --force
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

from _config import ensure_vector_source

# The IUCN-approved equal-area projection used by the AOO/EOO grid. The COG is
# generated in this CRS so it aligns with the assessment's area calculations.
AOO_CRS = "ESRI:54034"
DEFAULT_RESOLUTION_M = 100
CONFIG_PATH = Path("config/country_config.yaml")

# CORS policy for the COG bucket. The client-side viewer
# (assets/cog-viewer.mjs) reads the COG with HTTP Range requests from the
# published site's origin. Exposing Content-Range is the load-bearing part:
# without it the browser can't read the range-response size and COG loading
# breaks (Content-Length alone is CORS-safelisted, Content-Range is not).
CORS_POLICY = [
    {
        "origin": ["*"],
        "method": ["GET", "HEAD"],
        "responseHeader": ["Range", "Content-Range", "Content-Length", "Accept-Ranges"],
        "maxAgeSeconds": 3600,
    }
]


def _bucket_name(project: str) -> str:
    """Derive the COG bucket name from the GCP project ID."""
    return f"{project}-rle-cogs"


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    """Run a command, capturing output (does not raise on failure)."""
    return subprocess.run(cmd, capture_output=True, text=True)


_REAUTH_HINT = (
    "Application Default Credentials are missing or expired.\n"
    "Run the following, then re-run this script:\n\n"
    "  gcloud auth application-default login --project=<your-gcp-project>\n"
)


def check_adc() -> None:
    """Fail fast with a clear hint if ADC is missing/expired.

    gcsfs (``token='google_default'``) authenticates via Application Default
    Credentials, which are separate from ``gcloud auth login``. A 401/reauth
    error surfaces deep inside gcsfs as an unhelpful traceback, so we probe ADC
    up front instead.
    """
    result = _run(["gcloud", "auth", "application-default", "print-access-token"])
    if result.returncode != 0:
        sys.exit(_REAUTH_HINT)


def _is_auth_error(exc: BaseException) -> bool:
    """Detect ADC reauthentication / invalid-credential failures."""
    msg = str(exc).lower()
    return (
        "reauthentication" in msg
        or "invalid credentials" in msg
        or "401" in msg
    )


def _is_permission_error(result: subprocess.CompletedProcess) -> bool:
    """True if a gcloud command failed due to insufficient permissions.

    Covers the several phrasings gcloud uses across commands, e.g.
    ``services enable`` reports ``PERMISSION_DENIED`` while
    ``buckets add-iam-policy-binding`` reports ``does not have permission ...
    denied on resource``.
    """
    text = f"{result.stdout}\n{result.stderr}".lower()
    return (
        "permission_denied" in text
        or "permission denied" in text
        or "does not have permission" in text
        or "does not have storage." in text
        or "denied on resource" in text
        or "accessdenied" in text
        or "forbidden" in text
        or "403" in text
    )


def ensure_bucket(project: str, bucket: str, location: str) -> None:
    """Best-effort: ensure the Storage API is enabled and a public bucket exists.

    The provisioning steps here (enable API, create bucket, grant public read,
    set CORS) are best-effort. Once a project/bucket has been provisioned by an
    earlier run, a service account holding only object-write permission
    (roles/storage.objectAdmin) gets PERMISSION_DENIED on these admin
    operations — which is the normal case when this runs in CI. So we warn and
    continue rather than fail; the essential step is uploading the COG object
    (done by the caller), not re-provisioning infrastructure. Missing billing
    stays fatal, since nothing can succeed without it.
    """
    print(f"Ensuring Cloud Storage API is enabled on {project}...")
    enable = _run(
        ["gcloud", "services", "enable", "storage.googleapis.com",
         f"--project={project}"]
    )
    if enable.returncode != 0:
        if _is_permission_error(enable):
            print("  WARNING: no permission to enable storage.googleapis.com; "
                  "assuming it is already enabled.")
        else:
            sys.exit(f"Failed to enable storage.googleapis.com:\n{enable.stderr.strip()}")

    print(f"Ensuring bucket gs://{bucket} exists...")
    create = _run(
        ["gcloud", "storage", "buckets", "create", f"gs://{bucket}",
         f"--project={project}", f"--location={location}",
         "--uniform-bucket-level-access"]
    )
    if create.returncode != 0:
        stderr = create.stderr or ""
        if "already" in stderr.lower() and "exist" in stderr.lower():
            print("  Bucket already exists - skipping creation.")
        elif "HTTPError 409" in stderr or "Conflict" in stderr:
            # 409 = the name is taken. If we own it the IAM step below still
            # succeeds; otherwise that step surfaces a clear permission error.
            print("  Bucket name already in use - assuming it is ours.")
        elif "billing" in stderr.lower():
            sys.exit(
                f"Cloud Storage requires billing, which is not enabled on "
                f"project {project}.\n"
                f"(Earth Engine can run without billing, but GCS buckets cannot.)\n\n"
                f"List your billing accounts, then link one:\n"
                f"  gcloud billing accounts list\n"
                f"  gcloud billing projects link {project} "
                f"--billing-account=<ACCOUNT_ID>\n\n"
                f"Then re-run this script."
            )
        elif _is_permission_error(create):
            print(f"  WARNING: no permission to create gs://{bucket}; "
                  "assuming it already exists.")
        else:
            sys.exit(f"Failed to create bucket gs://{bucket}:\n{stderr.strip()}")

    print("Granting public read (allUsers -> roles/storage.objectViewer)...")
    grant = _run(
        ["gcloud", "storage", "buckets", "add-iam-policy-binding", f"gs://{bucket}",
         "--member=allUsers", "--role=roles/storage.objectViewer"]
    )
    if grant.returncode != 0:
        if _is_permission_error(grant):
            print(f"  WARNING: no permission to set public-read IAM on gs://{bucket}; "
                  "assuming it is already public.")
        else:
            sys.exit(
                f"Failed to grant public read on gs://{bucket}:\n{grant.stderr.strip()}"
            )

    ensure_cors(bucket)


def ensure_cors(bucket: str) -> None:
    """Apply the browser byte-range CORS policy to the bucket.

    Best-effort/idempotent. Applied whenever the script runs (both on bucket
    creation and on the skip-if-exists path) so existing buckets pick up the
    policy too. Setting CORS needs bucket-admin permission, so an object-write
    service account is warned-and-skipped (the existing bucket keeps its policy).
    """
    print(f"Setting CORS on gs://{bucket} (expose Content-Range)...")
    fd, cors_file = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(CORS_POLICY, f)
        result = _run(
            ["gcloud", "storage", "buckets", "update", f"gs://{bucket}",
             f"--cors-file={cors_file}"]
        )
    finally:
        os.unlink(cors_file)
    if result.returncode != 0:
        if _is_permission_error(result):
            print(f"  WARNING: no permission to set CORS on gs://{bucket}; "
                  "assuming the byte-range CORS policy is already applied.")
        else:
            sys.exit(f"Failed to set CORS on gs://{bucket}:\n{result.stderr.strip()}")


def upload_to_gcs(local_path: Path, bucket: str, key: str) -> None:
    """Upload a local file to gs://{bucket}/{key} via gcsfs.

    Mirrors the credential/error handling used elsewhere in the RLE codebase
    (rle.gee.upload). Uses Application Default Credentials.
    """
    import gcsfs

    fs = gcsfs.GCSFileSystem(token="google_default")
    dest = f"{bucket}/{key}"
    print(f"Uploading COG to gs://{dest} ...")
    try:
        fs.put(str(local_path), dest)
    except Exception as e:
        if _is_auth_error(e):
            raise SystemExit(_REAUTH_HINT) from e
        if "Forbidden" in str(e) or "billing" in str(e).lower():
            raise RuntimeError(
                f"GCS upload to gs://{bucket} failed: permission denied.\n"
                f"This usually means the ADC quota project has billing disabled "
                f"or you lack storage.objects.create on gs://{bucket}.\n"
            ) from e
        raise


def validate_cog(path: Path) -> None:
    """Assert the local file is a COG in the expected CRS before uploading."""
    import pyproj
    import rasterio

    with rasterio.open(path) as src:
        layout = src.tags(ns="IMAGE_STRUCTURE").get("LAYOUT")
        if layout != "COG":
            raise RuntimeError(
                f"Generated raster is not a COG (LAYOUT={layout!r}): {path}"
            )
        # Confirm the CRS matches the AOO equal-area projection.
        if src.crs is None or not pyproj.CRS(src.crs.to_wkt()).equals(
            pyproj.CRS(AOO_CRS)
        ):
            raise RuntimeError(
                f"Generated COG CRS {src.crs} does not match {AOO_CRS}"
            )
    print(f"  Validated COG: {path.name} ({AOO_CRS})")


def check_index_consistency(mapping: dict[int, str], config_path: Path) -> None:
    """Warn if the COG's index->code mapping disagrees with index.csv.

    The canonical ``config/ecosystems/index.csv`` (written by
    build_ecosystem_index.py) and this COG both derive their indices from
    ``Ecosystems.unique_ecosystems()``, so they should be identical. A mismatch
    means the COG and the report/configs were built from different source data
    and the map would be mis-colored. Warn (don't fail) so the upload still
    completes.
    """
    import csv

    index_csv = config_path.parent / "ecosystems" / "index.csv"
    if not index_csv.exists():
        return
    csv_map: dict[int, str] = {}
    with open(index_csv) as f:
        for row in csv.DictReader(f):
            csv_map[int(row["index"])] = row["code"]
    if csv_map != mapping:
        print(
            f"  WARNING: COG index->code mapping does not match {index_csv}.\n"
            f"  The COG and the report configs appear to come from different "
            f"source data; regenerate one so they agree."
        )


def record_config(config: dict, config_path: Path, resolution_m: int,
                  gs_uri: str, cog_url: str) -> None:
    """Write the raster outputs back into the country config file."""
    config["ecosystem_raster"] = {
        "resolution_m": resolution_m,
        "crs": AOO_CRS,
        "mode": "index",
        "gs_uri": gs_uri,
        "cog_url": cog_url,
    }
    with open(config_path, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False,
                  allow_unicode=True)
    print(f"Recorded ecosystem_raster in {config_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project",
        default=os.environ.get("GOOGLE_CLOUD_PROJECT"),
        help="GCP project ID (default: $GOOGLE_CLOUD_PROJECT)",
    )
    parser.add_argument(
        "--location", default="US",
        help="Bucket location when creating the bucket (default: US)",
    )
    parser.add_argument(
        "--resolution", type=int, default=None,
        help="Pixel size in meters (overrides config; default: "
             f"ecosystem_raster.resolution_m or {DEFAULT_RESOLUTION_M})",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Regenerate and re-upload even if the COG already exists",
    )
    parser.add_argument(
        "--config", type=Path, default=CONFIG_PATH,
        help=f"Path to country config (default: {CONFIG_PATH})",
    )
    args = parser.parse_args()

    if not args.project:
        parser.error("Provide --project or set GOOGLE_CLOUD_PROJECT")

    with open(args.config) as f:
        config = yaml.safe_load(f)

    source = config["ecosystem_source"]
    raster_cfg = config.get("ecosystem_raster") or {}
    resolution_m = (
        args.resolution
        if args.resolution is not None
        else raster_cfg.get("resolution_m", DEFAULT_RESOLUTION_M)
    )

    bucket = _bucket_name(args.project)
    stem = Path(str(source["data"]).split("?")[0].rstrip("/")).stem or "ecosystems"
    key = f"ecosystems/{stem}_{resolution_m}m.tif"
    gs_uri = f"gs://{bucket}/{key}"
    cog_url = f"https://storage.googleapis.com/{bucket}/{key}"

    print(f"Project:     {args.project}")
    print(f"Bucket:      gs://{bucket}")
    print(f"Object:      {key}")
    print(f"Resolution:  {resolution_m} m")
    print(f"CRS:         {AOO_CRS}")

    # Cloud Storage reserves bucket names beginning with "goog" or containing
    # "google" (https://cloud.google.com/storage/docs/buckets#naming). This bites
    # when the project is still the template placeholder "goog-rle-assessments".
    if bucket.startswith("goog") or "google" in bucket:
        hint = ""
        if args.project == "goog-rle-assessments":
            hint = (
                "\n\n'goog-rle-assessments' is the template's placeholder project. "
                "Pass --project <your-gcp-project> (or run scripts/init_repo.py to "
                "configure the repository), then re-run."
            )
        sys.exit(
            f"Cannot use bucket gs://{bucket}: Cloud Storage reserves bucket names "
            f"beginning with 'goog' or containing 'google'.{hint}"
        )

    # Verify credentials up front so a 401 fails with a clear hint rather than
    # a deep gcsfs traceback.
    check_adc()

    # Skip-if-exists (before any expensive work).
    import gcsfs

    fs = gcsfs.GCSFileSystem(token="google_default")
    if not args.force and fs.exists(f"{bucket}/{key}"):
        print(f"\nCOG already exists at {gs_uri} - skipping (use --force to regenerate).")
        # Still (re)apply CORS so the browser viewer can byte-range read an
        # already-uploaded COG whose bucket predates the CORS policy.
        ensure_cors(bucket)
        record_config(config, args.config, resolution_m, gs_uri, cog_url)
        print(f"\nPublic URL: {cog_url}")
        return

    ensure_bucket(args.project, bucket, args.location)

    # Load + rasterize to a local temp COG, then upload. The GDAL COG driver is
    # unreliable writing straight to /vsigs, so we stage locally first.
    print(f"\nLoading ecosystem data from {source['data']}...")
    from rle.core import Ecosystems

    # ecosystem_code_column is optional: fall back to the name column so the
    # raster indices match the name-based enumeration used elsewhere.
    ecosystem_column = source.get("ecosystem_code_column") or source.get("ecosystem_name_column")
    if ecosystem_column is None:
        sys.exit(
            "ecosystem_source needs ecosystem_code_column or ecosystem_name_column "
            "in config/country_config.yaml"
        )

    eco = Ecosystems.from_file(
        ensure_vector_source(source["data"]),
        ecosystem_column=ecosystem_column,
        ecosystem_name_column=source.get("ecosystem_name_column"),
        functional_group_column=source.get("functional_group_column"),
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_tif = Path(tmpdir) / f"{stem}_{resolution_m}m.tif"
        print(f"Rasterizing to COG at {resolution_m} m ({AOO_CRS})...")
        mapping = eco.to_raster(
            tmp_tif, crs=AOO_CRS, scale=resolution_m, mode="index"
        )
        print(f"  {len(mapping)} ecosystem classes: "
              f"{json.dumps(mapping, ensure_ascii=False)}")
        check_index_consistency(mapping, args.config)
        validate_cog(tmp_tif)
        upload_to_gcs(tmp_tif, bucket, key)

    record_config(config, args.config, resolution_m, gs_uri, cog_url)
    print(f"\nDone. Public URL: {cog_url}")


if __name__ == "__main__":
    main()
