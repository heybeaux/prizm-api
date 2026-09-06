import json
import os
import unittest
from unittest.mock import patch

from app import app, cache_duration_for_result
from prizm_client import PrizmClient, PrizmLookupError, normalize_postal_code


LOOKUP_RESULT = {
    "postal_code": "V8A 0A8",
    "prizm_code": "21",
    "segment_number": "21",
    "segment_name": "Scenic Retirement",
    "segment_description": "Older, middle-income suburbanites",
    "average_household_income": "$140,223",
    "education": "High School/College",
    "urbanity": "Suburban",
    "average_household_net_worth": "$1M to $2.15M",
    "average_household_net_worth_amount": 1255437,
    "occupation": "Mix",
    "diversity": "Low",
    "family_life": "Couples/Families",
    "tenure": "Own",
    "home_type": "Single Detached/Row",
    "status": "success",
}


class TestPrizmAPI(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        self.client.testing = True

    def test_health_check(self):
        response = self.client.get("/health")
        data = json.loads(response.data)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["status"], "ok")

    def test_normalize_postal_code(self):
        self.assertEqual(normalize_postal_code("v8a0a8"), "V8A 0A8")
        self.assertEqual(normalize_postal_code("V8A 0A8"), "V8A 0A8")
        self.assertIsNone(normalize_postal_code("123456"))

    @patch("app.cache_manager.cache_data", return_value=True)
    @patch("app.cache_manager.get_cached_data", return_value=None)
    @patch("app.prizm_client.lookup", return_value=LOOKUP_RESULT)
    def test_single_postal_code(self, lookup, get_cached_data, cache_data):
        response = self.client.get("/api/prizm?postal_code=V8A0A8")
        data = json.loads(response.data)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["postal_code"], "V8A 0A8")
        self.assertEqual(data["prizm_code"], "21")
        lookup.assert_called_once_with("V8A0A8")
        get_cached_data.assert_called_once_with("V8A 0A8")
        cache_data.assert_called_once_with("V8A 0A8", LOOKUP_RESULT, custom_duration_days=3650)

    def test_cache_duration_for_result(self):
        self.assertEqual(cache_duration_for_result({"status": "success"}), 3650)
        self.assertEqual(cache_duration_for_result({"status": "invalid"}), 90)
        self.assertEqual(cache_duration_for_result({"status": "error"}), 30)

    def test_new_lookup_includes_known_net_worth(self):
        client = PrizmClient()
        response = client._build_response(
            "V8A 2P4",
            62,
            {
                "PRIZM Name": "Down to Earth",
                "PRIZM Descriptor": "Older, lower-middle-income suburban singles",
                "Average Income": "95199",
            },
            {"geography": {}, "attributes": {}},
        )

        self.assertEqual(response["average_household_net_worth"], "$0 to $600K")
        self.assertEqual(response["average_household_net_worth_amount"], 461727)

    @patch("app.cache_manager.cache_data", return_value=True)
    @patch("app.cache_manager.get_cached_data", return_value=None)
    @patch("app.prizm_client.lookup", side_effect=PrizmLookupError("quota exceeded"))
    def test_upstream_exceptions_are_not_cached(self, _lookup, _get_cached_data, cache_data):
        response = self.client.get("/api/prizm?postal_code=V8A0A8")
        data = json.loads(response.data)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["status"], "error")
        cache_data.assert_not_called()

    @patch("app.cache_manager.cache_data", return_value=True)
    @patch("app.cache_manager.get_cached_data", return_value=None)
    @patch("app.prizm_client.lookup", return_value=LOOKUP_RESULT)
    def test_batch_postal_codes(self, lookup, _get_cached_data, _cache_data):
        response = self.client.post("/api/prizm/batch", json={"postal_codes": ["V8A0A8", "V8A 0A8"]})
        data = json.loads(response.data)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["total"], 2)
        self.assertEqual(data["successful"], 2)
        self.assertEqual(lookup.call_count, 2)

    def test_missing_postal_code_parameter(self):
        response = self.client.get("/api/prizm")
        self.assertEqual(response.status_code, 400)

    def test_empty_postal_codes_batch(self):
        response = self.client.post("/api/prizm/batch", json={"postal_codes": []})
        self.assertEqual(response.status_code, 400)

    def test_batch_limit(self):
        response = self.client.post("/api/prizm/batch", json={"postal_codes": ["V8A0A8"] * 11})
        self.assertEqual(response.status_code, 400)

    def test_api_key_protects_all_routes_except_health(self):
        os.environ["PRIZM_API_KEY"] = "secret"
        try:
            self.assertEqual(self.client.get("/").status_code, 401)
            self.assertEqual(self.client.get("/api/segments").status_code, 401)

            self.assertEqual(self.client.get("/health").status_code, 200)
            self.assertEqual(
                self.client.get("/", headers={"X-API-Key": "secret"}).status_code, 200
            )

            with patch("app.prizm_client.get_all_segments", return_value=[]):
                response = self.client.get("/api/segments", headers={"X-API-Key": "secret"})
            self.assertEqual(response.status_code, 200)

            self.assertEqual(self.client.get("/").status_code, 401)
            self.assertEqual(self.client.get("/health").status_code, 200)
        finally:
            os.environ.pop("PRIZM_API_KEY", None)




class TestOperationalReporting(unittest.TestCase):
    def setUp(self):
        import tempfile
        from cache_manager_new import CacheManager
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cache = CacheManager(os.path.join(self.directory.name, 'cache.db'))
        self.override = patch('app.cache_manager', self.cache)
        self.override.start()
        self.addCleanup(self.override.stop)
        self.env = patch.dict(os.environ, {'PRIZM_API_KEY': 'api-secret', 'DASHBOARD_PASSWORD': 'dashboard-secret', 'DASHBOARD_USERNAME': 'dena'}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        import base64
        self.auth = {'Authorization': 'Basic ' + base64.b64encode(b'dena:dashboard-secret').decode()}
        self.client = app.test_client()

    def test_dashboard_fails_closed_without_either_secret(self):
        with patch.dict(os.environ, {}, clear=True):
            for path in ['/', '/dashboard', '/api/dashboard/summary', '/api/cache/entries', '/api/cache/export.csv', '/api/lookups', '/api/lookups/failures.csv', '/api/reports/weekly']:
                self.assertEqual(self.client.get(path).status_code, 401, path)
            self.assertEqual(self.client.get('/health').status_code, 200)

    def test_dashboard_password_required_when_api_key_absent(self):
        os.environ.pop('PRIZM_API_KEY')
        self.assertEqual(self.client.get('/').status_code, 401)
        self.assertEqual(self.client.get('/', headers=self.auth).status_code, 200)

    def test_dashboard_credentials_cannot_send_email_or_modify_cache(self):
        self.assertEqual(self.client.get('/api/reports/weekly', headers=self.auth).status_code, 200)
        self.assertEqual(self.client.post('/api/reports/weekly/send', headers=self.auth).status_code, 401)
        self.assertEqual(self.client.post('/api/cache/clear', headers=self.auth).status_code, 401)

    def test_events_distinguish_repeated_cache_hits_and_fresh_successes(self):
        self.cache.record_lookup_event('V8A 0A8', 'success', 'cache', from_cache=True)
        self.cache.record_lookup_event('V8A 0A8', 'success', 'cache', from_cache=True)
        self.cache.record_lookup_event('V8A 0A8', 'success', 'upstream')
        self.cache.record_lookup_event('V8A 2P4', 'success', 'upstream')
        self.cache.record_lookup_event('M5V 3L9', 'error', 'upstream', message='quota unavailable')
        result = self.cache.get_lookup_event_summary()
        self.assertEqual(result['lookups'], 5)
        self.assertEqual(result['unique_postal_codes'], 3)
        self.assertEqual(result['cache_hits'], 2)
        self.assertEqual(result['upstream_successful'], 2)
        self.assertEqual(result['upstream_failed'], 1)
        self.assertEqual(result['newly_captured'], 1)
        self.assertEqual(result['by_day'][0]['newly_captured'], 1)
        summary = self.client.get('/api/dashboard/summary', headers=self.auth).get_json()
        self.assertEqual(summary['recent_failures'][0]['message'], 'quota unavailable')
        self.assertEqual(self.cache.list_cache_entries(), [])

    def test_failure_history_paginates_and_csv_neutralizes_formulas(self):
        import csv
        import io
        self.cache.record_lookup_event('V8A 0A8', 'error', 'upstream', message='=HYPERLINK("bad")')
        self.cache.record_lookup_event('V8A 2P4', 'invalid', 'validation', message='invalid')
        first = self.client.get('/api/lookups?failures=1&limit=1', headers=self.auth).get_json()['entries']
        second = self.client.get('/api/lookups?failures=1&limit=1&offset=1', headers=self.auth).get_json()['entries']
        self.assertNotEqual(first[0]['id'], second[0]['id'])
        rows = list(csv.DictReader(io.StringIO(self.cache.export_failures_csv())))
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[1]['message'].startswith("'="))

    def test_csv_exports_more_than_5000_entries_and_marks_historical_values(self):
        import csv
        import io
        self.cache.cache_data('V8A 0A8', LOOKUP_RESULT, custom_duration_days=10)
        with self.cache._connect() as conn:
            columns = [r[1] for r in conn.execute('PRAGMA table_info(postal_code_cache)') if r[1] not in ('postal_code', 'id')]
            names = ','.join(columns)
            conn.executemany(f'INSERT INTO postal_code_cache (postal_code,{names}) SELECT ?,{names} FROM postal_code_cache WHERE postal_code = ?', [(f'X{i:05}', 'V8A 0A8') for i in range(5000)])
            conn.commit()
        rows = list(csv.DictReader(io.StringIO(self.cache.export_cache_csv())))
        self.assertEqual(len(rows), 5001)
        self.assertEqual(rows[0]['net_worth_source'], 'Historical segment reference')

    def test_report_excludes_old_failures_and_warns_about_cache_only_activity(self):
        from app import build_weekly_report
        self.cache.record_lookup_event('V8A 0A8', 'error', 'upstream', message='old failure')
        with self.cache._connect() as conn:
            conn.execute("UPDATE lookup_events SET requested_at = datetime('now', '-10 days')")
            conn.commit()
        self.cache.record_lookup_event('V8A 2P4', 'success', 'cache', from_cache=True)
        report = build_weekly_report()
        self.assertIn('ATTENTION', report['body'])
        self.assertNotIn('old failure', report['body'])
        self.assertIn('Unique postal codes requested: 1', report['body'])
        report = build_weekly_report(days=14)
        self.assertIn('old failure', report['body'])
        self.assertIn('Lookups recorded: 2', report['body'])

    @patch('app.prizm_client.lookup', side_effect=PrizmLookupError('quota unavailable'))
    def test_transient_failure_is_retryable_and_visible_without_cache(self, lookup):
        response = self.client.get('/api/prizm?postal_code=V8A0A8', headers={'X-API-Key': 'api-secret'}).get_json()
        self.assertTrue(response['retryable'])
        self.assertEqual(self.cache.list_cache_entries(), [])
        self.assertEqual(len(self.cache.list_lookup_events(failures_only=True)), 1)


if __name__ == "__main__":
    unittest.main()
