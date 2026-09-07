import os
import tempfile
import unittest
from unittest.mock import patch

from app import app
from cache_manager_new import CacheManager
from cohort_queue import CohortQueue
from historical_import import merge_history


class HistoricalImportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = CacheManager(os.path.join(self.tmp.name, 'cache.db'))
        self.old = {'postal_code':'v8a0a8', 'segment_number':'21', 'segment_name':'Historical',
                    'status':'success', 'confirmed':0, 'expires_at':'2025-12-01 00:00:00',
                    'average_household_net_worth':123456, 'home_type':'Detached'}

    def test_current_row_preserved_entirely_and_normalized_overlap(self):
        self.cache.cache_data('V8A 0A8', {'status':'error', 'message':'Current result'})
        with self.cache._connect() as db:
            before = tuple(db.execute('SELECT * FROM postal_code_cache').fetchone())
        result = merge_history(self.cache.db_path, [self.old])
        with self.cache._connect() as db:
            self.assertEqual(before, tuple(db.execute('SELECT * FROM postal_code_cache').fetchone()))
        self.assertEqual(result['preserved_current'], 1)
        self.assertEqual(result['inserted'], 0)

    def test_import_preserves_age_values_and_history_after_cache_deletion(self):
        result = merge_history(self.cache.db_path, [self.old])
        self.assertEqual(result['inserted'], 1)
        self.assertIsNone(self.cache.get_cached_data('V8A 0A8'))
        row = self.cache.list_cache_entries(include_expired=True)[0]
        self.assertIsNone(row['cached_at'])
        self.assertEqual(row['expires_at'], self.old['expires_at'])
        self.assertEqual(row['average_household_net_worth_amount'], 123456)
        self.assertIn('Historical import', row['data_source'])
        self.assertEqual(merge_history(self.cache.db_path, [self.old])['inserted'], 0)
        self.cache.delete_cached_data('V8A 0A8')
        queue = CohortQueue(self.cache.db_path)
        with queue.connect() as db:
            db.execute("INSERT INTO donor_cohort VALUES ('V8A 0A8',1)")
        self.assertIsNone(queue.claim('2026-09-07'))
        self.assertEqual(queue.entries()[0]['captured_at'], '')
        self.assertEqual(merge_history(self.cache.db_path, [self.old])['inserted'], 1)
        self.assertIsNone(queue.claim('2026-09-08'))

    def test_bad_row_aborts_whole_import_and_bad_formats_are_reported(self):
        with self.assertRaises(ValueError):
            merge_history(self.cache.db_path, [self.old, {'postal_code':'V8A 1A1','status':'oops'}])
        self.assertEqual(self.cache.list_cache_entries(include_expired=True), [])
        result = merge_history(self.cache.db_path, [{'postal_code':'90210','status':'invalid'}])
        self.assertEqual(result['skipped_invalid_format'], 1)

    def test_production_startup_snapshot_is_preserved(self):
        import sqlite3
        self.cache.cache_data('V8A 0A8', {'status':'success', 'segment_number':'21'})
        with patch.dict(os.environ, {'RAILWAY_ENVIRONMENT_ID':'test-production'}):
            CacheManager(self.cache.db_path)
            self.cache.delete_cached_data('V8A 0A8')
            CacheManager(self.cache.db_path)
        with sqlite3.connect(self.cache.db_path + '.before-cohort-upgrade.db') as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM postal_code_cache').fetchone()[0], 1)

    def test_endpoint_auth_and_input_validation(self):
        with patch('app.cache_manager', self.cache), patch.dict(os.environ, {'PRIZM_API_KEY':'key'}):
            client = app.test_client()
            self.assertEqual(client.post('/api/cache/import-history', json={'rows':[self.old]}).status_code, 401)
            response = client.post('/api/cache/import-history', headers={'X-API-Key':'key'}, json={'rows':[self.old]})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json['inserted'], 1)


if __name__ == '__main__':
    unittest.main()
