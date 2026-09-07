import base64
import logging
import os
import secrets
import smtplib
import time
import uuid
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Any, Dict, Optional

import requests
from flask import Flask, Response, jsonify, make_response, request

from cache_manager_new import cache_manager
from cohort_queue import CohortQueue
from historical_import import merge_history
from prizm_client import PrizmClient, PrizmLookupError, normalize_postal_code
from segment_net_worth import average_household_net_worth, average_household_net_worth_amount

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
prizm_client = PrizmClient()

DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PRIZM Dashboard</title>
  <style>
    :root { color-scheme: light; --ink:#17212b; --muted:#627386; --line:#d9e1ea; --bg:#f6f8fb; --card:#ffffff; --ok:#087f5b; --bad:#c92a2a; --warn:#b7791f; --brand:#143c5a; }
    * { box-sizing:border-box; }
    body { margin:0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; background:var(--bg); color:var(--ink); }
    header { background:linear-gradient(135deg,#0f2f48,#1f6b88); color:#fff; padding:28px 32px; }
    header h1 { margin:0 0 6px; font-size:30px; letter-spacing:-.02em; }
    header p { margin:0; opacity:.86; }
    main { padding:24px 32px 40px; max-width:1320px; margin:0 auto; }
    .grid { display:grid; gap:16px; grid-template-columns:repeat(4,minmax(0,1fr)); margin-bottom:20px; }
    .card { background:var(--card); border:1px solid var(--line); border-radius:14px; padding:18px; box-shadow:0 1px 2px rgba(20,45,70,.05); }
    .metric { font-size:32px; line-height:1; font-weight:750; letter-spacing:-.04em; }
    .label { color:var(--muted); font-size:13px; margin-top:8px; }
    .ok { color:var(--ok); } .bad { color:var(--bad); } .warn { color:var(--warn); }
    .toolbar { display:flex; gap:10px; flex-wrap:wrap; align-items:center; margin:18px 0; }
    input, select, button, a.button { border:1px solid var(--line); border-radius:10px; padding:10px 12px; background:#fff; font:inherit; color:var(--ink); }
    button, a.button { cursor:pointer; text-decoration:none; font-weight:650; }
    button.primary, a.primary { background:var(--brand); border-color:var(--brand); color:#fff; }
    table { width:100%; border-collapse:separate; border-spacing:0; overflow:hidden; background:#fff; border:1px solid var(--line); border-radius:14px; font-size:14px; }
    th, td { text-align:left; padding:11px 12px; border-bottom:1px solid var(--line); vertical-align:top; }
    th { background:#edf3f8; font-size:12px; text-transform:uppercase; letter-spacing:.04em; color:#496174; }
    tr:last-child td { border-bottom:0; }
    .pill { display:inline-block; border-radius:99px; padding:3px 8px; font-size:12px; font-weight:700; background:#edf2f7; }
    .pill.success { color:#087f5b; background:#dff7ed; } .pill.error { color:#c92a2a; background:#ffe3e3; } .pill.invalid { color:#8a5a00; background:#fff3bf; }
    .muted { color:var(--muted); }
    .section-title { display:flex; justify-content:space-between; align-items:end; gap:12px; margin:28px 0 12px; }
    .section-title h2 { margin:0; font-size:20px; }
    pre { white-space:pre-wrap; overflow-wrap:anywhere; max-width:540px; }
    #reportStatus { white-space:pre-wrap; }
    table { display:block; overflow-x:auto; }
    @media (max-width:900px) { .grid { grid-template-columns:repeat(2,minmax(0,1fr)); } main, header { padding-left:18px; padding-right:18px; } table { font-size:13px; } }
    @media (max-width:580px) { .grid { grid-template-columns:1fr; } th:nth-child(5),td:nth-child(5),th:nth-child(6),td:nth-child(6){display:none;} }
  </style>
</head>
<body>
<header>
  <h1>PRIZM Dashboard</h1>
  <p>Postal-code cache, lookup outcomes, export, and weekly reporting metrics.</p>
</header>
<main>
  <div class="grid">
    <div class="card"><div id="total" class="metric">–</div><div class="label">postal codes stored (all dates)</div></div>
    <div class="card"><div id="successful" class="metric ok">–</div><div class="label">successful stored codes</div></div>
    <div class="card"><div id="failed" class="metric bad">–</div><div class="label">failed / unassigned stored codes</div></div>
    <div class="card"><div id="weekLookups" class="metric">–</div><div class="label">lookups in the trailing 7 days</div></div>
  </div>

  <div class="section-title">
    <div><h2>Export and reports</h2><div class="muted">Download cached data and failed attempts, or preview the scheduled weekly report.</div></div>
  </div>
  <div class="card toolbar">
    <a class="button primary" href="/api/cache/export.csv?include_expired=1">Export cached data CSV</a>
    <a class="button" href="/api/lookups/failures.csv?days=3650">Export failure history CSV</a>
    <button id="sendReport">Preview weekly report</button>
    <span id="reportStatus" class="muted"></span>
  </div>

  <h2>Major-donor coverage</h2>
  <p id="cohortProgress" class="card">Loading…</p>
  <a class="button" href="/api/cohort/export.csv">Export major-donor coverage CSV</a>
  <div class="section-title">
    <div><h2>Daily lookup outcomes</h2><div class="muted">Trailing 7 days, grouped by UTC date. Cache hits do not represent new captures.</div></div>
  </div>
  <p id="captureWarning" class="warn" role="status"></p>
  <div class="card"><div id="dailyCounts" class="muted">Loading…</div></div>

  <div class="section-title">
    <div><h2>Postal codes</h2><div class="muted">Search all cached records, including expired entries. Net worth is historical segment reference data; unavailable values remain blank.</div></div>
  </div>
  <div class="toolbar">
    <input id="search" placeholder="Search postal code, segment, message" size="34">
    <select id="status"><option value="">All statuses</option><option value="success">Success</option><option value="error">Error</option><option value="invalid">Invalid</option></select>
    <button id="refresh" class="primary">Refresh</button>
  </div>
  <table>
    <thead><tr><th>Postal code</th><th>Status</th><th>Segment</th><th>Name</th><th>Home type</th><th>Income</th><th>Historical net worth</th><th>Cached</th><th>Message</th></tr></thead>
    <tbody id="rows"><tr><td colspan="9" class="muted">Loading…</td></tr></tbody>
  </table>
  <div class="toolbar"><button id="previous">Previous</button><span id="pageInfo"></span><button id="next">Next</button></div>
  <div class="section-title"><h2>Failed lookup history</h2></div>
  <p class="muted">Includes quota/network errors that are not stored in the cache. Dates are UTC. Export downloads all retained failures.</p>
  <div class="card" id="failures">Loading…</div>
  <div class="toolbar"><button id="failurePrevious">Previous</button><span id="failurePageInfo"></span><button id="failureNext">Next</button></div>
</main>
<script>
const fmt = new Intl.NumberFormat();
let offset = 0, failureOffset = 0;
const pageSize = 100;
function text(id, value) { document.getElementById(id).textContent = value; }
function esc(value) { return String(value ?? '').replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c])); }
async function fetchJson(url, options) {
  const res = await fetch(url, options);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || body.message || `HTTP ${res.status}`);
  return body;
}
async function loadSummary() {
  const data = await fetchJson('/api/dashboard/summary');
  const stats = data.cache_stats || {};
  const breakdown = stats.all_status_breakdown || stats.status_breakdown || {};
  const week = data.lookup_events_7d || {};
  text('total', fmt.format(stats.total_entries || 0));
  text('successful', fmt.format(breakdown.success || 0));
  text('failed', fmt.format((breakdown.error || 0) + (breakdown.invalid || 0)));
  text('weekLookups', fmt.format(week.lookups || 0));
  text('captureWarning', week.lookups && !week.upstream_attempts ? 'No new upstream lookups in the last 7 days. Check whether the enrichment queue is repeating already captured postal codes.' : '');
  const cohort = data.cohort || {};
  text('cohortProgress', cohort.configured ? `${cohort.captured} / ${cohort.unique_codes} codes captured · ${cohort.pending} pending · ${cohort.failed} failed · ${cohort.incomplete} incomplete. ${cohort.source_rows} source accounts; ${cohort.excluded_rows} address exceptions. Daily target: 10 uncaptured codes; captured codes never repeat.` : 'Cohort has not been imported.');
  const counts = week.by_day || [];
  document.getElementById('dailyCounts').innerHTML = counts.length ? counts.map(row =>
    `<div><strong>${esc(row.day)}</strong>: ${fmt.format(row.lookups || 0)} lookups · ${fmt.format(row.cache_hits || 0)} cache hits · ${fmt.format(row.upstream_attempts || 0)} upstream attempts · <span class="ok">${fmt.format(row.upstream_successful || 0)} upstream success</span> · <span class="bad">${fmt.format(row.failed || 0)} failed</span> · ${fmt.format(row.unique_postal_codes || 0)} unique codes · ${fmt.format(row.newly_captured || 0)} first captures in recorded history</div>`
  ).join('') : 'No daily additions recorded in the selected window.';
}
async function loadRows() {
  const params = new URLSearchParams({ limit: String(pageSize), offset: String(offset), include_expired: '1' });
  const search = document.getElementById('search').value.trim();
  const status = document.getElementById('status').value;
  if (search) params.set('search', search);
  if (status) params.set('status', status);
  const data = await fetchJson('/api/cache/entries?' + params.toString());
  const tbody = document.getElementById('rows');
  document.getElementById('previous').disabled = offset === 0;
  document.getElementById('next').disabled = data.count < pageSize;
  text('pageInfo', `Page ${offset / pageSize + 1} · ${data.count} records`);
  if (!data.entries?.length) { tbody.innerHTML = '<tr><td colspan="9" class="muted">No matching entries.</td></tr>'; return; }
  tbody.innerHTML = data.entries.map(row => `
    <tr>
      <td><details><summary><strong>${esc(row.postal_code)}</strong></summary><pre>${esc(JSON.stringify(row, null, 2))}</pre></details></td>
      <td><span class="pill ${esc(row.status)}">${esc(row.status)}</span></td>
      <td>${esc(row.segment_number || '')}</td>
      <td>${esc(row.segment_name || '')}</td>
      <td>${esc(row.home_type || '')}</td>
      <td>${esc(row.average_household_income || '')}</td>
      <td>${esc(row.average_household_net_worth || '')}</td>
      <td class="muted">${esc(row.cached_at || '')}</td>
      <td>${esc(row.message || '')}</td>
    </tr>`).join('');
}
async function sendReport() {
  const status = document.getElementById('reportStatus');
  status.textContent = 'Loading preview…';
  try {
    const data = await fetchJson('/api/reports/weekly');
    status.textContent = data.body || 'No report body returned.';
  } catch (err) { status.textContent = 'Report preview unavailable: ' + err.message; }
}
async function loadFailures() {
  const data = await fetchJson(`/api/lookups?failures=1&days=3650&limit=${pageSize}&offset=${failureOffset}`);
  document.getElementById('failures').innerHTML = data.entries.length ? data.entries.map(row => `<p><strong>${esc(row.postal_code)}</strong> · ${esc(row.requested_at)} · ${esc(row.source)} · ${esc(row.message || row.status)}</p>`).join('') : 'No failed attempts recorded.';
  document.getElementById('failurePrevious').disabled = failureOffset === 0;
  document.getElementById('failureNext').disabled = data.count < pageSize;
  text('failurePageInfo', `Page ${failureOffset / pageSize + 1} · ${data.count} attempts`);
}
function showError(err) { text('captureWarning', 'Could not load data: ' + err.message); }
document.getElementById('refresh').addEventListener('click', () => { offset = 0; loadSummary().catch(showError); loadRows().catch(showError); loadFailures().catch(showError); });
document.getElementById('previous').addEventListener('click', () => { offset = Math.max(0, offset - pageSize); loadRows().catch(showError); });
document.getElementById('next').addEventListener('click', () => { offset += pageSize; loadRows().catch(showError); });
document.getElementById('failurePrevious').addEventListener('click', () => { failureOffset = Math.max(0, failureOffset - pageSize); loadFailures().catch(showError); });
document.getElementById('failureNext').addEventListener('click', () => { failureOffset += pageSize; loadFailures().catch(showError); });
document.getElementById('sendReport').addEventListener('click', sendReport);
document.getElementById('search').addEventListener('keydown', e => { if (e.key === 'Enter') { offset = 0; loadRows().catch(showError); } });
loadSummary().catch(showError);
loadFailures().catch(showError);
loadRows().catch(err => { document.getElementById('rows').innerHTML = `<tr><td colspan="9" class="bad">${esc(err.message)}</td></tr>`; });
</script>
</body>
</html>
"""


def cache_duration_for_result(result: Dict[str, Any]) -> int:
    status = result.get("status")
    if status == "success":
        return int(os.environ.get("PRIZM_SUCCESS_CACHE_DAYS", "3650"))
    if status == "invalid":
        return int(os.environ.get("PRIZM_INVALID_CACHE_DAYS", "90"))
    return int(os.environ.get("PRIZM_ERROR_CACHE_DAYS", "30"))


def dashboard_credentials() -> tuple[Optional[str], Optional[str]]:
    username = os.environ.get("DASHBOARD_USERNAME", "dena")
    password = os.environ.get("DASHBOARD_PASSWORD") or os.environ.get("PRIZM_DASHBOARD_PASSWORD")
    if not password and os.environ.get("ALLOW_API_KEY_AS_DASHBOARD_PASSWORD", "1") == "1":
        password = os.environ.get("PRIZM_API_KEY")
    return username, password


def parse_basic_auth() -> tuple[Optional[str], Optional[str]]:
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Basic "):
        return None, None
    try:
        decoded = base64.b64decode(auth_header.removeprefix("Basic ")).decode("utf-8")
        username, password = decoded.split(":", 1)
        return username, password
    except (ValueError, UnicodeDecodeError):
        return None, None


def has_valid_api_key() -> bool:
    expected_key = os.environ.get("PRIZM_API_KEY")
    if not expected_key:
        return True

    provided_key = request.headers.get("X-API-Key")
    auth_header = request.headers.get("Authorization", "")
    bearer_token = auth_header.removeprefix("Bearer ").strip() if auth_header.startswith("Bearer ") else None
    return secrets.compare_digest(provided_key or "", expected_key) or secrets.compare_digest(bearer_token or "", expected_key)


def has_valid_dashboard_auth() -> bool:
    username, password = dashboard_credentials()
    if not password:
        return False
    provided_username, provided_password = parse_basic_auth()
    return secrets.compare_digest(provided_username or "", username or "") and secrets.compare_digest(provided_password or "", password)


def dashboard_auth_required() -> Response:
    response = make_response("Dashboard authentication required", 401)
    response.headers["WWW-Authenticate"] = 'Basic realm="PRIZM Dashboard"'
    return response


@app.before_request
def require_authentication():
    if request.path == "/health" or request.method == "OPTIONS":
        return None

    dashboard_paths = {"/", "/dashboard", "/api/dashboard/summary", "/api/cache/entries", "/api/cache/export.csv", "/api/reports/weekly", "/api/lookups", "/api/lookups/failures.csv", "/api/cohort", "/api/cohort/export.csv"}
    if request.path in dashboard_paths:
        if has_valid_dashboard_auth() or (os.environ.get("PRIZM_API_KEY") and has_valid_api_key()):
            return None
        return dashboard_auth_required()

    if request.path == "/api/reports/weekly/send" and not os.environ.get("PRIZM_API_KEY"):
        return jsonify({"error": "Email sending requires a configured API key"}), 503

    if request.path.startswith("/api/") and has_valid_api_key():
        return None

    return jsonify({"error": "Unauthorized"}), 401


def api_response_from_cache(cached_data: Dict[str, Any]) -> Dict[str, Any]:
    response = cached_data.copy()
    segment_number = response.get("segment_number")
    response["prizm_code"] = segment_number or "Unknown"
    if segment_number:
        net_worth_amount = average_household_net_worth_amount(segment_number)
        if net_worth_amount is not None:
            response["average_household_net_worth_amount"] = net_worth_amount
        if not response.get("average_household_net_worth"):
            response["average_household_net_worth"] = average_household_net_worth(segment_number)

    if response.get("status") == "invalid":
        response["prizm_code"] = "Unknown"

    cache_info = response.pop("_cache_info", None)
    if cache_info:
        response["cache_info"] = cache_info

    return response


def get_prizm_code(postal_code: str, endpoint: str = "single", batch_id: Optional[str] = None) -> Dict[str, Any]:
    started = time.monotonic()
    formatted_postal_code = normalize_postal_code(postal_code)
    cache_key = formatted_postal_code or postal_code

    cached_data = cache_manager.get_cached_data(cache_key)
    if cached_data:
        logger.info("Returning cached PRIZM result for %s", cache_key)
        result = api_response_from_cache(cached_data)
        cache_manager.record_lookup_event(
            cache_key,
            result.get("status", "error"),
            "cache",
            endpoint=endpoint,
            batch_id=batch_id,
            message=result.get("message"),
            from_cache=True,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return result

    source = "upstream" if formatted_postal_code else "validation"
    try:
        result = prizm_client.lookup(postal_code)
        should_cache = True
    except (PrizmLookupError, requests.RequestException) as exc:
        logger.exception("PRIZM lookup failed for %s", postal_code)
        should_cache = False
        result = {
            "postal_code": formatted_postal_code or postal_code,
            "prizm_code": "Unknown",
            "segment_number": None,
            "segment_name": "",
            "segment_description": "",
            "average_household_income": "",
            "education": "",
            "urbanity": "",
            "average_household_net_worth": "",
            "occupation": "",
            "diversity": "",
            "family_life": "",
            "tenure": "",
            "home_type": "",
            "status": "error",
            "message": str(exc),
            "retryable": True,
        }

    if should_cache:
        cache_duration = cache_duration_for_result(result)
        cache_manager.cache_data(cache_key, result, custom_duration_days=cache_duration)

    cache_manager.record_lookup_event(
        cache_key,
        result.get("status", "error"),
        source,
        endpoint=endpoint,
        batch_id=batch_id,
        message=result.get("message"),
        from_cache=False,
        duration_ms=int((time.monotonic() - started) * 1000),
    )
    return result


@app.route("/")
@app.route("/dashboard")
def dashboard():
    return Response(DASHBOARD_HTML, mimetype="text/html")


@app.route("/api")
def home():
    return jsonify(
        {
            "name": "PRIZM Code Lookup API",
            "status": "ok",
            "endpoints": {
                "single": "GET /api/prizm?postal_code=V8A0A8",
                "batch": "POST /api/prizm/batch",
                "dashboard": "GET /dashboard",
                "csv_export": "GET /api/cache/export.csv",
                "health": "GET /health",
            },
        }
    )


@app.route("/health", methods=["GET"])
def health_check():
    return jsonify({"status": "ok", "message": "Service is running"})


@app.route("/api/prizm", methods=["GET"])
def get_single_prizm():
    postal_code = request.args.get("postal_code")
    if not postal_code:
        return jsonify({"error": "postal_code is required"}), 400
    return jsonify(get_prizm_code(postal_code, endpoint="single"))


@app.route("/api/prizm/batch", methods=["POST"])
def get_batch_prizm():
    data = request.get_json(silent=True) or {}
    postal_codes = data.get("postal_codes")

    if not isinstance(postal_codes, list) or not postal_codes:
        return jsonify({"error": "postal_codes must be a non-empty list"}), 400

    max_postal_codes = int(os.environ.get("MAX_BATCH_POSTAL_CODES", "10"))
    if len(postal_codes) > max_postal_codes:
        return jsonify({"error": f"Too many postal codes. Maximum allowed is {max_postal_codes}."}), 400

    batch_id = str(uuid.uuid4())
    results = [get_prizm_code(str(postal_code), endpoint="batch", batch_id=batch_id) for postal_code in postal_codes]
    return jsonify(
        {
            "results": results,
            "total": len(results),
            "successful": sum(1 for result in results if result["status"] == "success"),
            "failed": sum(1 for result in results if result["status"] != "success"),
        }
    )


def cohort_queue():
    return CohortQueue(cache_manager.db_path)


@app.route("/api/cohort", methods=["GET"])
def cohort_status():
    queue = cohort_queue()
    return jsonify({"summary": queue.summary(), "entries": queue.entries()})


@app.route("/api/cohort/export.csv", methods=["GET"])
def cohort_export():
    response = Response(cohort_queue().export_csv(), mimetype="text/csv")
    response.headers["Content-Disposition"] = "attachment; filename=major-donor-coverage.csv"
    return response


@app.route("/api/cache/import-history", methods=["POST"])
def import_historical_cache():
    if not os.environ.get("PRIZM_API_KEY") or not has_valid_api_key():
        return jsonify({"error": "Historical import requires an API key"}), 401
    if request.content_length is None or request.content_length > 2_000_000:
        return jsonify({"error": "A historical JSON payload under 2 MB is required"}), 400
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Expected a JSON object"}), 400
    try:
        result = merge_history(cache_manager.db_path, data.get("rows"))
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid historical data; import rolled back"}), 400
    return jsonify(result)


@app.route("/api/cohort/import", methods=["POST"])
def import_cohort():
    if not os.environ.get("PRIZM_API_KEY") or not has_valid_api_key():
        return jsonify({"error": "Import requires an API key"}), 401
    if request.content_length is None or request.content_length > 1_000_000:
        return jsonify({"error": "A CSV body under 1 MB is required"}), 400
    try:
        import io
        summary = cohort_queue().import_accounts(io.StringIO(request.get_data().decode("utf-8-sig")))
    except (ValueError, UnicodeError):
        return jsonify({"error": "Invalid Canadian billing-address CSV; cohort unchanged"}), 400
    return jsonify(summary)


@app.route("/api/cohort/capture", methods=["POST"])
def capture_cohort():
    # Collection is a mutation and always requires an explicit API key.
    if not os.environ.get("PRIZM_API_KEY") or not has_valid_api_key():
        return jsonify({"error": "Collection requires an API key"}), 401
    queue = cohort_queue()
    if not queue.summary()["configured"]:
        return jsonify({"error": "Major-donor cohort has not been imported"}), 503
    results = []
    # One code per call keeps Salesforce callouts below transaction time limits.
    # Its queueable chains ten calls; the database also enforces the daily ceiling.
    for _ in range(1):
        code = queue.claim()
        if code is None:
            break
        # Claim persists before network I/O: retries/concurrent requests cannot repeat a code.
        result = get_prizm_code(code, endpoint="cohort")
        queue.finish(code, result)
        results.append(result)
    return jsonify({"results": results, "summary": queue.summary()})


@app.route("/api/segments", methods=["GET"])
def get_segments():
    return jsonify({"status": "success", "segments": prizm_client.get_all_segments()})


@app.route("/api/dashboard/summary", methods=["GET"])
def dashboard_summary():
    summary = cache_manager.get_dashboard_summary()
    summary["cohort"] = cohort_queue().summary()
    return jsonify(summary)


@app.route("/api/lookups", methods=["GET"])
def lookup_events():
    rows = cache_manager.list_lookup_events(
        failures_only=request.args.get("failures") == "1",
        days=request.args.get("days", 7, type=int),
        limit=request.args.get("limit", 100, type=int),
        offset=request.args.get("offset", 0, type=int),
    )
    return jsonify({"entries": rows, "count": len(rows)})


@app.route("/api/lookups/failures.csv", methods=["GET"])
def export_lookup_failures():
    response = Response(cache_manager.export_failures_csv(request.args.get("days", 7, type=int)), mimetype="text/csv")
    response.headers["Content-Disposition"] = "attachment; filename=prizm-failures.csv"
    return response


@app.route("/api/cache/entries", methods=["GET"])
def get_cache_entries():
    entries = cache_manager.list_cache_entries(
        status=request.args.get("status"),
        search=request.args.get("search"),
        limit=request.args.get("limit", 500, type=int),
        offset=request.args.get("offset", 0, type=int),
        include_expired=request.args.get("include_expired") == "1",
    )
    return jsonify({"status": "success", "entries": entries, "count": len(entries)})


@app.route("/api/cache/export.csv", methods=["GET"])
def export_cache_csv():
    csv_data = cache_manager.export_cache_csv(include_expired=request.args.get("include_expired") == "1")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    response = Response(csv_data, mimetype="text/csv")
    response.headers["Content-Disposition"] = f"attachment; filename=prizm-cache-{timestamp}.csv"
    return response


@app.route("/api/cache/stats", methods=["GET"])
def get_cache_stats():
    return jsonify({"status": "success", "cache_stats": cache_manager.get_cache_stats()})


@app.route("/api/cache/cleanup", methods=["POST"])
def cleanup_cache():
    deleted_count = cache_manager.cleanup_expired_cache()
    return jsonify({"status": "success", "message": f"Cleaned up {deleted_count} expired cache entries"})


@app.route("/api/cache/clear", methods=["POST"])
def clear_cache():
    if cache_manager.clear_cache():
        return jsonify({"status": "success", "message": "All cache entries cleared"})
    return jsonify({"status": "error", "error": "Failed to clear cache"}), 500


@app.route("/api/cache/check/<postal_code>", methods=["GET"])
def check_cache(postal_code):
    cached_data = cache_manager.get_cached_data(postal_code)
    return jsonify(
        {
            "status": "success",
            "postal_code": postal_code,
            "is_cached": cached_data is not None,
            "cached_at": cached_data.get("_cache_info", {}).get("cached_at") if cached_data else None,
        }
    )


@app.route("/api/cache/delete/<postal_code>", methods=["DELETE"])
def delete_cache_entry(postal_code):
    if cache_manager.delete_cached_data(postal_code):
        return jsonify({"status": "success", "message": f"Successfully deleted cache entry for {postal_code}"})
    return jsonify({"status": "error", "error": f"No cache entry found for {postal_code}"}), 404


@app.route("/api/cache/confirm/<postal_code>", methods=["POST"])
def confirm_cache_entry(postal_code):
    if cache_manager.confirm_data(postal_code):
        return jsonify({"status": "success", "message": f"Successfully confirmed data for {postal_code}"})
    return jsonify({"status": "error", "error": f"No valid cache entry found for {postal_code}"}), 404


@app.route("/api/cache/unconfirm/<postal_code>", methods=["POST"])
def unconfirm_cache_entry(postal_code):
    if cache_manager.unconfirm_data(postal_code):
        return jsonify({"status": "success", "message": f"Successfully unconfirmed data for {postal_code}"})
    return jsonify({"status": "error", "error": f"No valid cache entry found for {postal_code}"}), 404


@app.route("/api/cache/unconfirmed", methods=["GET"])
def get_unconfirmed_entries():
    limit = request.args.get("limit", 100, type=int)
    entries = cache_manager.get_unconfirmed_entries(limit)
    return jsonify({"status": "success", "unconfirmed_entries": entries, "count": len(entries)})


@app.route("/api/debug/html/<postal_code>", methods=["GET"])
def get_debug_html(postal_code):
    html_content = cache_manager.get_cached_html(postal_code)
    if html_content:
        return html_content, 200, {"Content-Type": "text/html; charset=utf-8"}
    return jsonify({"status": "error", "error": f"No HTML content found for postal code {postal_code}"}), 404


def build_weekly_report(days: int = 7) -> Dict[str, Any]:
    summary = cache_manager.get_dashboard_summary()
    events = cache_manager.get_lookup_event_summary(days)
    summary["lookup_events_7d"] = events
    summary["cohort"] = cohort_queue().summary()
    stats = summary.get("cache_stats", {})
    daily_counts = events.get("by_day", [])
    failures = cache_manager.list_lookup_events(failures_only=True, days=days, limit=20)

    subject = os.environ.get("WEEKLY_REPORT_SUBJECT", "PRIZM weekly report")
    lines = [
        "PRIZM weekly report",
        "",
        f"Window: trailing {days} days; daily dates are UTC.",
        "API success does not prove Salesforce household updates or email delivery.",
        f"Lookups recorded: {events.get('lookups', 0)}",
        f"Successful lookups: {events.get('successful', 0)}",
        f"Failed lookups: {events.get('failed', 0)}",
        f"Cache hits: {events.get('cache_hits', 0)}",
        f"Upstream attempts: {events.get('upstream_attempts', 0)}",
        f"Upstream successful: {events.get('upstream_successful', 0)}",
        f"Upstream failed: {events.get('upstream_failed', 0)}",
        f"Unique postal codes requested: {events.get('unique_postal_codes', 0)}",
        f"New captures (first successful lookup in recorded history): {events.get('newly_captured', 0)}",
        "Historical net worth is a segment reference, not current postal-code wealth.",
        "",
        f"Total active cached postal codes: {stats.get('valid_entries', 0)}",
        f"Cached successes: {(stats.get('status_breakdown') or {}).get('success', 0)}",
        f"Cached failed/unassigned: {((stats.get('status_breakdown') or {}).get('error', 0) + (stats.get('status_breakdown') or {}).get('invalid', 0))}",
        "",
        "Daily lookup outcomes (UTC):",
    ]
    cohort = summary["cohort"]
    lines.extend(["", "Major-donor Canadian postal-code coverage:",
                  f"Captured: {cohort['captured']}; pending: {cohort['pending']}; failed: {cohort['failed']}; incomplete: {cohort['incomplete']}",
                  "Daily target: 10 uncaptured codes. Quota/network rejections recover after untouched codes on later days; captured codes never repeat.", ""])
    if daily_counts:
        for row in daily_counts:
            lines.append(f"- {row.get('day')}: {row.get('lookups', 0)} lookups, {row.get('cache_hits', 0)} cached, {row.get('upstream_successful', 0)} upstream successes, {row.get('failed', 0)} failures")
    else:
        lines.append("- None recorded")

    if events.get("lookups", 0) and not events.get("upstream_attempts", 0):
        lines.extend(["", "ATTENTION: All recorded lookups were served without new upstream attempts. Check the enrichment queue for repeated postal codes."])
    lines.extend(["", "Failed lookup attempts in this reporting window:"])
    if failures:
        for row in failures:
            lines.append(f"- {row.get('postal_code')}: {row.get('message') or row.get('status')}")
    else:
        lines.append("- None")

    lines.extend(["", f"Dashboard: https://{os.environ.get('RAILWAY_PUBLIC_DOMAIN', 'prizm-api-production.up.railway.app')}/dashboard"])
    return {"subject": subject, "body": "\n".join(lines), "summary": summary}


def send_weekly_report_email(report: Dict[str, Any]) -> list[str]:
    recipients = [addr.strip() for addr in os.environ.get("WEEKLY_REPORT_RECIPIENTS", "").split(",") if addr.strip()]
    if not recipients:
        raise RuntimeError("WEEKLY_REPORT_RECIPIENTS is not configured")

    smtp_host = os.environ.get("SMTP_HOST")
    if not smtp_host:
        raise RuntimeError("SMTP_HOST is not configured")

    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ.get("SMTP_USERNAME")
    smtp_password = os.environ.get("SMTP_PASSWORD")
    sender = os.environ.get("SMTP_FROM") or smtp_user
    if not sender:
        raise RuntimeError("SMTP_FROM or SMTP_USERNAME is required")

    message = EmailMessage()
    message["Subject"] = report["subject"]
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message.set_content(report["body"])

    use_ssl = os.environ.get("SMTP_SSL", "0") == "1"
    smtp_cls = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
    with smtp_cls(smtp_host, smtp_port, timeout=30) as smtp:
        if not use_ssl and os.environ.get("SMTP_STARTTLS", "1") == "1":
            smtp.starttls()
        if smtp_user and smtp_password:
            smtp.login(smtp_user, smtp_password)
        smtp.send_message(message)
    return recipients


@app.route("/api/reports/weekly", methods=["GET"])
def weekly_report_preview():
    return jsonify(build_weekly_report())


@app.route("/api/reports/weekly/send", methods=["POST"])
def weekly_report_send():
    report = build_weekly_report()
    try:
        recipients = send_weekly_report_email(report)
    except RuntimeError as exc:
        return jsonify({"status": "error", "error": str(exc), "report": report}), 500
    return jsonify({"status": "success", "recipients": recipients, "report": report})


@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = os.environ.get("CORS_ALLOW_ORIGIN", "*")
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-API-Key, Authorization"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
    return response


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG") == "1")
