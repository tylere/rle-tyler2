# GCP Workload Identity Federation Setup

This guide explains how to configure Google Cloud Platform (GCP) authentication for GitHub Actions to enable automated rendering of Quarto notebooks that use Google Earth Engine.

## Prerequisites

- [Google Cloud CLI](https://cloud.google.com/sdk/docs/install) installed and authenticated
- Access to the GCP project (e.g., `PLACEHOLDER_GCP_PROJECT_ID`)
- Admin access to the GitHub repository

## One-Time Setup (Infrastructure)

These steps only need to be done once per GCP project. If the Workload Identity Pool already exists, skip to [Per-Repository Setup](#per-repository-setup).

### 1. Create a Workload Identity Pool

```bash
gcloud iam workload-identity-pools create "github-pool" \
  --project="PLACEHOLDER_GCP_PROJECT_ID" \
  --location="global" \
  --display-name="GitHub Actions Pool"
```

### 2. Create a Workload Identity Provider

```bash
gcloud iam workload-identity-pools providers create-oidc "github-provider" \
  --project="PLACEHOLDER_GCP_PROJECT_ID" \
  --location="global" \
  --workload-identity-pool="github-pool" \
  --display-name="GitHub Provider" \
  --attribute-mapping="google.subject=assertion.sub,attribute.actor=assertion.actor,attribute.repository=assertion.repository,attribute.repository_owner=assertion.repository_owner" \
  --attribute-condition="assertion.repository_owner == 'RLE-Assessment'" \
  --issuer-uri="https://token.actions.githubusercontent.com"
```

### 3. Create a Service Account

```bash
gcloud iam service-accounts create "github-actions-rle" \
  --project="PLACEHOLDER_GCP_PROJECT_ID" \
  --display-name="GitHub Actions RLE"
```

### 4. Grant Required IAM Roles

The service account needs two roles:
- `roles/earthengine.writer` - Access to Earth Engine
- `roles/serviceusage.serviceUsageConsumer` - Permission to use GCP APIs

```bash
gcloud projects add-iam-policy-binding "PLACEHOLDER_GCP_PROJECT_ID" \
  --member="serviceAccount:github-actions-rle@PLACEHOLDER_GCP_PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/earthengine.writer"

gcloud projects add-iam-policy-binding "PLACEHOLDER_GCP_PROJECT_ID" \
  --member="serviceAccount:github-actions-rle@PLACEHOLDER_GCP_PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/serviceusage.serviceUsageConsumer"
```

### 5. Get Project Number

You'll need the GCP project number (not the project ID) for the next steps:

```bash
gcloud projects describe PLACEHOLDER_GCP_PROJECT_ID --format="value(projectNumber)"
```

## Per-Repository Setup

For each new repository created from this template, complete these steps.

### 1. Allow GitHub Repository to Impersonate Service Account

Replace `PROJECT_NUMBER` with your actual project number and `YOUR-REPO-NAME` with your repository name:

```bash
gcloud iam service-accounts add-iam-policy-binding \
  "github-actions-rle@PLACEHOLDER_GCP_PROJECT_ID.iam.gserviceaccount.com" \
  --project="PLACEHOLDER_GCP_PROJECT_ID" \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/github-pool/attribute.repository/RLE-Assessment/YOUR-REPO-NAME"
```

### 2. Get the Workload Identity Provider Resource Name

```bash
gcloud iam workload-identity-pools providers describe "github-provider" \
  --project="PLACEHOLDER_GCP_PROJECT_ID" \
  --location="global" \
  --workload-identity-pool="github-pool" \
  --format="value(name)"
```

This outputs a value like:
```
projects/123456789/locations/global/workloadIdentityPools/github-pool/providers/github-provider
```

### 3. Add GitHub Repository Secrets

Go to your repository's **Settings > Secrets and variables > Actions** and add:

| Secret Name | Value |
|-------------|-------|
| `GCP_WORKLOAD_IDENTITY_PROVIDER` | `projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/github-pool/providers/github-provider` |
| `GCP_SERVICE_ACCOUNT` | `github-actions-rle@PLACEHOLDER_GCP_PROJECT_ID.iam.gserviceaccount.com` |

## Rasterize the Ecosystem Map to a COG (optional)

To store a high-resolution raster of the ecosystem map as a Cloud-Optimized GeoTIFF (COG) in the GCP project — for example, to serve later as a web-map layer — run:

```bash
python scripts/rasterize_ecosystem_to_cog.py --project PLACEHOLDER_GCP_PROJECT_ID
```

This manual step:

- rasterizes the `ecosystem_source` vector (from `config/country_config.yaml`) in the `ESRI:54034` projection used by the AOO/EOO calculations, at the resolution set by `ecosystem_raster.resolution_m` (default 100 m; override with `--resolution 10`);
- **enables the `storage.googleapis.com` API** and **creates a public-read bucket** `gs://PLACEHOLDER_GCP_PROJECT_ID-rle-cogs` if it does not already exist;
- uploads the COG and records its public URL back into `config/country_config.yaml` under `ecosystem_raster.cog_url`.

It is intentionally **not** part of `pixi run render` (rasterizing + uploading is slow and needs GCP write credentials). Existing COGs are skipped; re-run with `--force` after the ecosystem data changes.

## Verification

After configuration, push a commit to the `main` branch. The GitHub Actions workflow should:

1. Authenticate to Google Cloud (you should see "Authenticate to Google Cloud" step succeed)
2. Render the Quarto project with Earth Engine access
3. Deploy to GitHub Pages

If the secrets are not configured, the authentication step will be skipped and notebooks requiring Earth Engine will fail.

## Troubleshooting

### Authentication Step Skipped

If you see the "Authenticate to Google Cloud" step skipped in the workflow run:
- Verify both secrets (`GCP_WORKLOAD_IDENTITY_PROVIDER` and `GCP_SERVICE_ACCOUNT`) are set
- Check that the secret names match exactly (they are case-sensitive)

### Permission Denied Errors

If you see permission errors:
- Verify the IAM binding was created for your specific repository
- Check that the repository owner matches the `attribute-condition` in the provider (e.g., `RLE-Assessment`)
- Ensure the service account has the `earthengine.writer` role

### Earth Engine Errors

If notebooks fail with Earth Engine errors:
- Verify the service account is registered in Earth Engine
- Check that `GOOGLE_CLOUD_PROJECT` in the workflow matches your GCP project

### "Caller does not have required permission" Error

If you see an error like:
```
Caller does not have required permission to use project PLACEHOLDER_GCP_PROJECT_ID.
Grant the caller the roles/serviceusage.serviceUsageConsumer role...
```

The service account is missing the Service Usage Consumer role. Run:
```bash
gcloud projects add-iam-policy-binding "PLACEHOLDER_GCP_PROJECT_ID" \
  --member="serviceAccount:github-actions-rle@PLACEHOLDER_GCP_PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/serviceusage.serviceUsageConsumer"
```
