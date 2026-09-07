"""Private cohort collection, independent of Salesforce field completeness and cache TTL."""
import csv
import io
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo


def postal_code(value):
    value = re.sub(r'\s+', '', (value or '').upper())
    if re.fullmatch(r'[ABCEGHJ-NPRSTVXY][0-9][ABCEGHJ-NPRSTV-Z][0-9][ABCEGHJ-NPRSTV-Z][0-9]', value):
        return value[:3] + ' ' + value[3:]
    return None


class CohortQueue:
    def __init__(self, db_path):
        self.db_path = db_path
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS capture_history (
                    postal_code TEXT PRIMARY KEY, captured_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS donor_cohort (
                    postal_code TEXT PRIMARY KEY, account_rows INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cohort_attempts (
                    postal_code TEXT PRIMARY KEY, day TEXT NOT NULL,
                    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    result_json TEXT
                );
                CREATE TABLE IF NOT EXISTS cohort_metadata (
                    id INTEGER PRIMARY KEY CHECK(id=1), source_rows INTEGER NOT NULL,
                    excluded_rows INTEGER NOT NULL
                );
            ''')
            # Seed all historical successes, including expired cache and retained events.
            for query in (
                "SELECT postal_code, cached_at FROM postal_code_cache WHERE status='success'",
                "SELECT postal_code, MIN(requested_at) FROM lookup_events WHERE status='success' GROUP BY postal_code",
            ):
                for raw, captured in db.execute(query).fetchall():
                    code = postal_code(raw)
                    if code:
                        db.execute('INSERT OR IGNORE INTO capture_history VALUES (?, ?)', (code, str(captured)))

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def import_accounts(self, stream):
        reader = csv.DictReader(stream)
        required = {'Billing Country Code', 'Billing Zip/Postal Code'}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError('Salesforce billing country and postal-code columns are required')
        counts, total, excluded = {}, 0, 0
        for row in reader:
            total += 1
            code = postal_code(row['Billing Zip/Postal Code'])
            if (row.get('Billing Country Code') or '').strip().upper() != 'CA' or not code:
                excluded += 1
                continue
            counts[code] = counts.get(code, 0) + 1
        if not counts:
            raise ValueError('No eligible Canadian billing postal codes; existing cohort preserved')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            # Exact membership replacement; retain capture and attempt history permanently.
            db.execute('DELETE FROM donor_cohort')
            db.executemany('INSERT INTO donor_cohort VALUES (?, ?)', counts.items())
            db.execute('INSERT OR REPLACE INTO cohort_metadata VALUES (1, ?, ?)', (total, excluded))
        return self.summary()

    def claim(self, day=None):
        day = day or datetime.now(ZoneInfo('America/Vancouver')).date().isoformat()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT COUNT(*) FROM cohort_attempts WHERE day=?', (day,)).fetchone()[0] >= 10:
                return None
            row = db.execute('''
                SELECT c.postal_code FROM donor_cohort c
                LEFT JOIN capture_history h USING(postal_code)
                LEFT JOIN cohort_attempts a USING(postal_code)
                WHERE h.postal_code IS NULL AND a.postal_code IS NULL
                ORDER BY c.account_rows DESC, c.postal_code LIMIT 1
            ''').fetchone()
            if row is None:
                # A quota/network rejection is not enrichment. Recover these only
                # after untouched codes, at most once per subsequent calendar day.
                row = db.execute("""
                    SELECT c.postal_code FROM donor_cohort c
                    JOIN cohort_attempts a USING(postal_code)
                    LEFT JOIN capture_history h USING(postal_code)
                    WHERE h.postal_code IS NULL AND a.day < ?
                    AND json_extract(a.result_json, '$.retryable') = 1
                    ORDER BY a.day, c.account_rows DESC, c.postal_code LIMIT 1
                """, (day,)).fetchone()
                if row is None:
                    return None
                db.execute("UPDATE cohort_attempts SET day=?, started_at=CURRENT_TIMESTAMP, result_json=NULL WHERE postal_code=?", (day, row[0]))
                return row[0]
            db.execute('INSERT INTO cohort_attempts (postal_code, day) VALUES (?, ?)', (row[0], day))
            return row[0]

    def finish(self, code, result):
        with self.connect() as db:
            db.execute('UPDATE cohort_attempts SET result_json=? WHERE postal_code=?', (json.dumps(result), code))
            if result.get('status') == 'success':
                db.execute("INSERT OR IGNORE INTO capture_history VALUES (?, CURRENT_TIMESTAMP)", (code,))

    def entries(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute('''
                SELECT c.postal_code, c.account_rows, h.captured_at, a.started_at,
                    CASE WHEN h.postal_code IS NOT NULL THEN 'captured'
                         WHEN a.result_json IS NOT NULL THEN 'failed'
                         WHEN a.postal_code IS NOT NULL THEN 'incomplete'
                         ELSE 'pending' END AS coverage,
                    a.result_json
                FROM donor_cohort c LEFT JOIN capture_history h USING(postal_code)
                LEFT JOIN cohort_attempts a USING(postal_code)
                ORDER BY c.account_rows DESC, c.postal_code
            ''')]

    def summary(self):
        rows = self.entries()
        with self.connect() as db:
            meta = db.execute('SELECT source_rows, excluded_rows FROM cohort_metadata').fetchone()
        return dict(meta or {}, configured=meta is not None, unique_codes=len(rows),
                    eligible_accounts=sum(row['account_rows'] for row in rows),
                    **{key: sum(row['coverage'] == key for row in rows)
                       for key in ('captured', 'pending', 'failed', 'incomplete')})

    def export_csv(self):
        stream = io.StringIO()
        writer = csv.DictWriter(stream, fieldnames=['postal_code', 'account_rows', 'coverage', 'captured_at', 'started_at'])
        writer.writeheader()
        for row in self.entries():
            writer.writerow({key: row[key] for key in writer.fieldnames})
        return stream.getvalue()


if __name__ == '__main__':
    import argparse
    from cache_manager_new import CacheManager
    parser = argparse.ArgumentParser(description='Import a private Salesforce account export; never scrape or update Salesforce.')
    parser.add_argument('csv_file')
    parser.add_argument('--db', required=True)
    args = parser.parse_args()
    CacheManager(args.db)
    with open(args.csv_file, encoding='utf-8-sig', newline='') as stream:
        print(json.dumps(CohortQueue(args.db).import_accounts(stream), indent=2))
