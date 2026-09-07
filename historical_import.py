"""Import legacy cache rows without replacing current records or refreshing their age."""
import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

from cohort_queue import postal_code

FIELDS = (
    'postal_code', 'segment_number', 'segment_name', 'segment_description', 'who_they_are',
    'average_household_income', 'education', 'urbanity', 'average_household_net_worth',
    'occupation', 'diversity', 'family_life', 'tenure', 'home_type', 'status', 'confirmed',
    'expires_at',
)


def read_legacy(path):
    """Open the source read-only; no source migration or modification."""
    with sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('Source database integrity check failed')
        rows = [dict(row) for row in db.execute('SELECT * FROM postal_code_cache ORDER BY postal_code')]
    return rows


def merge_history(db_path, rows, source='late-2025'):
    if not isinstance(rows, list) or len(rows) > 1000:
        raise ValueError('Expected at most 1000 historical rows')
    prepared, skipped, seen = [], 0, set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('Historical rows must be objects')
        code = postal_code(row.get('postal_code'))
        if code is None:
            skipped += 1
            continue
        if code in seen:
            raise ValueError('Duplicate normalized historical postal code')
        seen.add(code)
        if row.get('status') not in ('success', 'error', 'invalid') or not row.get('expires_at'):
            raise ValueError('Historical status and original expiry are required')
        values = {key: row.get(key) for key in FIELDS}
        values['postal_code'] = code
        prepared.append(values)
    db = sqlite3.connect(db_path, timeout=30)
    try:
        with db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('''CREATE TABLE IF NOT EXISTS historical_imports (
                postal_code TEXT PRIMARY KEY, source TEXT NOT NULL,
                original_expires_at TEXT, imported_at TEXT DEFAULT CURRENT_TIMESTAMP
            )''')
            # Match codes across spacing/case variations, preserving every current field.
            current = {postal_code(row[0]) for row in db.execute('SELECT postal_code FROM postal_code_cache')}
            inserted = preserved = successes = 0
            for row in prepared:
                code = row['postal_code']
                if row['status'] == 'success':
                    # Empty date explicitly means capture time unknown; never substitute import time.
                    db.execute("INSERT OR IGNORE INTO capture_history VALUES (?, '')", (code,))
                if code in current:
                    preserved += 1
                    continue
                columns = FIELDS + ('cached_at', 'average_household_net_worth_amount', 'historical_source')
                amount = row.get('average_household_net_worth')
                if not isinstance(amount, (int, float)):
                    amount = None
                db.execute('INSERT INTO postal_code_cache (' + ','.join(columns) + ') VALUES (' + ','.join('?' for _ in columns) + ')',
                           [row[key] for key in FIELDS] + [None, amount, source])
                db.execute('INSERT OR IGNORE INTO historical_imports (postal_code,source,original_expires_at) VALUES (?,?,?)',
                           (code, source, row['expires_at']))
                current.add(code)
                inserted += 1
                successes += row['status'] == 'success'
        return {'source_rows': len(rows), 'inserted': inserted, 'inserted_successes': successes,
                'preserved_current': preserved, 'skipped_invalid_format': skipped}
    finally:
        db.close()


if __name__ == '__main__':
    from cache_manager_new import CacheManager
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--target', required=True)
    args = parser.parse_args()
    if Path(args.source).resolve() == Path(args.target).resolve():
        parser.error('Source and target must differ')
    rows = read_legacy(args.source)
    CacheManager(args.target)
    source_hash = hashlib.sha256(Path(args.source).read_bytes()).hexdigest()
    print(json.dumps(merge_history(args.target, rows, 'late-2025 sha256:' + source_hash), indent=2))
