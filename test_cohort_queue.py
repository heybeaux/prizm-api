import io
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from cache_manager_new import CacheManager
from cohort_queue import CohortQueue, postal_code
from app import app


class CohortTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = CacheManager(os.path.join(self.tmp.name, 'test.db'))
        self.queue = CohortQueue(self.cache.db_path)
        self.codes = [f'V8A {n}A{i}' for n in range(3) for i in range(10)]
        self.csv = 'Billing Country Code,Billing Zip/Postal Code\n' + ''.join('CA,' + p + '\n' for p in self.codes)
        self.queue.import_accounts(io.StringIO(self.csv))

    def test_normalization_import_and_exclusions(self):
        self.assertIsNone(postal_code('D1D 1D1'))
        self.queue.import_accounts(io.StringIO('Billing Country Code,Billing Zip/Postal Code\nCA,v8a0a0\nCA,V8A 0A0\nUS,90210\nCA,\n'))
        summary = self.queue.summary()
        self.assertEqual((summary['source_rows'], summary['excluded_rows'], summary['unique_codes'], summary['eligible_accounts']), (4, 2, 1, 2))
        with self.assertRaises(ValueError):
            self.queue.import_accounts(io.StringIO('wrong\ncolumn\n'))
        self.assertEqual(self.queue.summary()['unique_codes'], 1)

    def test_concurrency_limit_restart_next_day_and_no_repeat(self):
        with ThreadPoolExecutor(max_workers=12) as pool:
            claimed = list(pool.map(lambda _: self.queue.claim('2026-09-06'), range(20)))
        codes = [p for p in claimed if p]
        self.assertEqual(len(codes), 10)
        self.assertEqual(len(set(codes)), 10)
        for code in codes:
            self.queue.finish(code, {'status': 'error'})
        queue = CohortQueue(self.cache.db_path)
        self.assertIsNone(queue.claim('2026-09-06'))
        self.assertNotIn(queue.claim('2026-09-07'), codes)
        self.assertEqual(queue.summary()['failed'], 10)

    def test_captured_history_survives_expiry_deletion_and_reimport(self):
        code = self.codes[0]
        self.cache.cache_data(code, {'status': 'success', 'segment_number': '21'}, custom_duration_days=-1)
        queue = CohortQueue(self.cache.db_path)
        self.cache.delete_cached_data(code)
        queue.import_accounts(io.StringIO(self.csv))
        self.assertEqual(queue.summary()['captured'], 1)
        self.assertNotEqual(queue.claim('2026-09-06'), code)

    def test_api_auth_daily_limit_and_success_exclusion(self):
        with patch('app.cache_manager', self.cache), patch.dict(os.environ, {'PRIZM_API_KEY': 'test-key', 'DASHBOARD_PASSWORD': 'dashboard'}), patch('app.get_prizm_code', side_effect=lambda code, **kw: {'postal_code': code, 'status': 'success'}) as lookup:
            client = app.test_client()
            self.assertEqual(client.post('/api/cohort/capture').status_code, 401)
            first = client.post('/api/cohort/capture', headers={'X-API-Key': 'test-key'})
            self.assertEqual(first.status_code, 200)
            self.assertEqual(len(first.json['results']), 1)
            for _ in range(9):
                client.post('/api/cohort/capture', headers={'X-API-Key': 'test-key'})
            second = client.post('/api/cohort/capture', headers={'X-API-Key': 'test-key'})
            self.assertEqual(second.json['results'], [])
            self.assertEqual(lookup.call_count, 10)
            self.assertEqual(self.queue.summary()['captured'], 10)

    def test_import_and_capture_require_api_key_even_with_dashboard_login(self):
        import base64
        dashboard = 'Basic ' + base64.b64encode(b'dena:dashboard').decode()
        with patch('app.cache_manager', self.cache), patch.dict(os.environ, {'PRIZM_API_KEY': 'key', 'DASHBOARD_USERNAME': 'dena', 'DASHBOARD_PASSWORD': 'dashboard'}):
            client = app.test_client()
            self.assertEqual(client.get('/api/cohort', headers={'Authorization': dashboard}).status_code, 200)
            for path in ('/api/cohort/capture', '/api/cohort/import'):
                self.assertEqual(client.post(path, headers={'Authorization': dashboard}).status_code, 401)
            response = client.post('/api/cohort/import', data='wrong\ncolumn\n', headers={'X-API-Key': 'key'})
            self.assertEqual(response.status_code, 400)
            self.assertEqual(self.queue.summary()['unique_codes'], 30)

    def test_capture_before_import_fails_closed(self):
        with self.queue.connect() as db:
            db.execute('DELETE FROM cohort_metadata')
        with patch('app.cache_manager', self.cache), patch.dict(os.environ, {'PRIZM_API_KEY': 'key'}), patch('app.get_prizm_code') as lookup:
            response = app.test_client().post('/api/cohort/capture', headers={'X-API-Key': 'key'})
            self.assertEqual(response.status_code, 503)
            lookup.assert_not_called()

    def test_failed_and_interrupted_codes_not_repeated(self):
        first = self.queue.claim('2026-09-06')
        second = self.queue.claim('2026-09-06')
        self.queue.finish(second, {'status': 'error', 'retryable': True})
        new = self.queue.claim('2026-09-07')
        self.assertNotIn(new, [first, second])
        self.assertEqual(self.queue.summary()['incomplete'], 2)


if __name__ == '__main__':
    unittest.main()
