# PRIZM Code Lookup API

Small Flask API for looking up Canadian PRIZM segments by postal code. It is shaped for a Salesforce Flow that submits up to 10 Canadian postal codes per run.

## What Changed

The current PRIZM site no longer needs browser scraping. The React frontend calls public JSON services:

- Environics geocoder: resolves a postal code to PRIZM segmentation code
- Supabase REST: returns PRIZM segment profile data

This app now uses those APIs directly instead of Selenium/Chromium, which makes Railway deployment much lighter and avoids the old long-running browser crash mode.

## Endpoints

### Health

```http
GET /health
```

### Password-protected Dashboard

```http
GET /dashboard
```

The root URL and `/dashboard` show cache totals, daily lookup outcomes, failure history (including uncached quota/network errors), searchable and paginated postal-code records with expandable details, complete cache/failure CSV exports, and weekly-report preview. It uses HTTP Basic Auth and fails closed without a configured password or API key. Dashboard credentials provide read-only reporting access; sending an email requires the API key.

Recommended dashboard/reporting variables:

```bash
DASHBOARD_USERNAME=dena
DASHBOARD_PASSWORD=<set a dashboard password>
WEEKLY_REPORT_RECIPIENTS=dena@example.org
SMTP_HOST=<smtp host>
SMTP_PORT=587
SMTP_USERNAME=<smtp username>
SMTP_PASSWORD=<smtp password>
SMTP_FROM=<sender email>
SMTP_STARTTLS=1
```

If `DASHBOARD_PASSWORD` is not set, the dashboard can temporarily use `PRIZM_API_KEY` as the password unless `ALLOW_API_KEY_AS_DASHBOARD_PASSWORD=0` is set.

### Single Lookup

```http
GET /api/prizm?postal_code=V8A0A8
```

### Batch Lookup

```http
POST /api/prizm/batch
Content-Type: application/json

{
  "postal_codes": ["V8A0A8", "M5V3L9"]
}
```

The default batch limit is 10 postal codes. Override with `MAX_BATCH_POSTAL_CODES`.

### All Segments

```http
GET /api/segments
```

Useful if you want the full segment reference data without postal-code lookup.

### Cache Entries and CSV Export

```http
GET /api/cache/entries?status=success&search=V8A&limit=500
GET /api/cache/export.csv?include_expired=1
GET /api/lookups?failures=1&days=7&limit=100&offset=0
GET /api/lookups/failures.csv?days=3650
```

### Weekly Report

```http
GET /api/reports/weekly
POST /api/reports/weekly/send
```

For Railway weekly email delivery, create a cron service/worker that runs:

```bash
python send_weekly_report.py
```

Railway evaluates native cron schedules in UTC; Monday 8am Vancouver is `0 15 * * 1` during PDT.

For Wilderness Committee Salesforce delivery, `WCPrizmWeeklyReportEmail` can be scheduled instead. It calls `/api/reports/weekly` with the configured API key/custom label and sends the plain-text report via Salesforce email. The deployed production schedule is `0 0 8 ? * MON` (`WC PRIZM Weekly Report`).

## Example Response

```json
{
  "postal_code": "V8A 0A8",
  "prizm_code": "21",
  "segment_number": "21",
  "segment_name": "Scenic Retirement",
  "segment_description": "Older, middle-income suburbanites",
  "average_household_income": "$140,223",
  "education": "High School/College",
  "urbanity": "Suburban",
  "occupation": "Mix",
  "diversity": "Low",
  "family_life": "Couples/Families",
  "tenure": "Own",
  "home_type": "Single Detached/Row",
  "status": "success"
}
```

## Local Development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Then open `http://localhost:8080/health`.

## Railway Deployment

Railway can deploy this repo with either the `Dockerfile` or the `Procfile`.

Recommended Railway variables:

```bash
PRIZM_API_KEY=<set-this-and-send-it-from-salesforce>
MAX_BATCH_POSTAL_CODES=10
PRIZM_CACHE_DB_PATH=/data/prizm_cache_v2.db
PRIZM_SUCCESS_CACHE_DAYS=3650
PRIZM_INVALID_CACHE_DAYS=90
PRIZM_ERROR_CACHE_DAYS=30
WEB_CONCURRENCY=2
GUNICORN_THREADS=4
GUNICORN_TIMEOUT=60
```

If you set `PRIZM_CACHE_DB_PATH=/data/...`, add a Railway volume mounted at `/data` so cached lookups survive redeploys. The cache is optional; the API works without a persistent volume.

To preload Railway's persistent cache from the existing CSV export, run this in a Railway shell after the volume is mounted:

```bash
python cache_cli.py import-csv prizm_import_09112025.csv --replace
python cache_cli.py stats
```

Successful lookups default to a 10-year cache, invalid postal-code formats default to 90 days, and cacheable not-found/error results default to 30 days. Upstream quota/network failures are not cached.

When `PRIZM_API_KEY` is set, all `/api/*` routes require either:

```http
X-API-Key: <key>
```

or:

```http
Authorization: Bearer <key>
```

Optional override variables:

```bash
PRIZM_GEOCODER_API_URL=https://api.environicsanalytics.com/geocoder-openauth
PRIZM_GEOCODER_COUNTRY=ca
PRIZM_GEOCODER_VINTAGE=2026
PRIZM_SUPABASE_URL=https://rkfddhcgcubrelqdzajw.supabase.co
PRIZM_SUPABASE_KEY=<public publishable key>
PRIZM_UPSTREAM_TIMEOUT=12
CORS_ALLOW_ORIGIN=*
```

## Tests

```bash
python3 -m unittest test_prizm_api.py
```

## Notes

- Selenium files are left in the repo for historical reference, but the Flask API no longer imports or uses Selenium.
- Rural postal codes are resolved directly from the frontend's Supabase `rural_postal_codes` table.
- Urban postal codes still depend on the Environics geocoder API. During triage on June 21, 2026, that endpoint returned `403 Quota Exceeded`, so urban postal-code lookups may fail until the public quota replenishes or a licensed/geocoding credential is available.
- `prizm_cache_v2.db`, CSV exports, debug screenshots, and `node_modules` are runtime/local artifacts and are ignored by Docker.
- The old crash was most likely caused by a globally reused Selenium Chrome session combined with Flask debug mode and repeated screenshot/page-source capture. Removing the browser from request handling eliminates that failure path.

## Operational behavior

- Weekly metrics use a trailing seven-day window with UTC daily dates. Lookup success means an API result, not a successful Salesforce update. Cache hits, upstream attempts, upstream successes/failures, and unique postal codes are separate metrics. First captures mean the first successful upstream result in retained lookup-event history; imported/earlier records may predate that history.
- The daily Salesforce selector no longer retries an assigned household solely because net worth is unavailable. It preserves an existing net-worth value when no historical reference exists. Retryable quota/network errors leave the household eligible for a later attempt.
- Salesforce household updates now roll back the batch and surface a failed job if any update fails. This exposes errors previously hidden by ignored partial-update results; inspect the job error before retrying a persistently failing batch.
- The current public reference data includes dwelling type but no net worth. Net worth shown here is historical segment reference data of mixed vintage, not a current postal-code-specific estimate. Missing values remain unavailable. Some dwelling types cannot be represented by the existing Salesforce picklist and map to `None or Unverifiable`.
- `MAX_BATCH_POSTAL_CODES=10` limits this application's request size; it does not establish an upstream allowance. A claimed three-per-day restriction remains unverified. Confirm licensed access with Environics before adjusting collection volume.
- Weekly delivery already uses Salesforce's scheduled sender; do not create a second Railway email schedule. Salesforce job completion is not an inbox-delivery receipt.

See [September 6 investigation and upgrade scope](docs/2026-09-06-upgrade.md).
