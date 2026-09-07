#!/usr/bin/env python3
"""
Cache Manager for PRIZM API data - schema with queryable columns and lookup events.
Stores postal code data locally to reduce API calls to the PRIZM upstream services
and to support dashboard/reporting metrics.
"""

import csv
import io
import json
import logging
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from segment_net_worth import average_household_net_worth, average_household_net_worth_amount

logger = logging.getLogger(__name__)


class CacheManager:
    """Manages local caching of PRIZM postal code data with individual columns."""

    def __init__(self, db_path: str = None, cache_duration_days: int = None):
        self.db_path = db_path or os.environ.get("PRIZM_CACHE_DB_PATH", "prizm_cache_v2.db")
        self.cache_duration_days = cache_duration_days or int(os.environ.get("PRIZM_CACHE_DURATION_DAYS", "90"))
        self._init_database()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _normalize_postal_code(self, postal_code: str) -> str:
        compact = (postal_code or "").strip().upper().replace(" ", "")
        if len(compact) == 6 and re.fullmatch(r"[A-Z]\d[A-Z]\d[A-Z]\d", compact):
            return f"{compact[:3]} {compact[3:]}"
        return (postal_code or "").strip().upper()

    def _parse_currency_to_int(self, value: Optional[Any]) -> Optional[Any]:
        """Convert simple currency strings to integers; keep ranges as text."""
        if value is None or value == "" or value == "Unknown":
            return None
        if isinstance(value, (int, float)):
            return int(value)

        cleaned = re.sub(r"[$,\s]", "", str(value))
        try:
            return int(float(cleaned))
        except (ValueError, TypeError):
            return str(value).strip()

    def _format_currency_from_int(self, value: Optional[Any]) -> Optional[str]:
        """Convert integer 95199 to '$95,199'; return range labels unchanged."""
        if value is None:
            return None
        if isinstance(value, str):
            return value if value.startswith("$") else f"${value}"
        return f"${int(value):,}"

    def _normalize_status(self, status: Optional[str]) -> str:
        """Normalize status values to 'success', 'error', or 'invalid'."""
        if not status:
            return "error"
        status_lower = status.lower()
        if "success" in status_lower:
            return "success"
        if "invalid" in status_lower and "format" in status_lower:
            return "invalid"
        if status_lower == "invalid":
            return "invalid"
        return "error"

    def _column_names(self, cursor: sqlite3.Cursor, table: str) -> set[str]:
        cursor.execute(f"PRAGMA table_info({table})")
        return {row[1] for row in cursor.fetchall()}

    def _add_missing_columns(self, cursor: sqlite3.Cursor, table: str, columns: Dict[str, str]) -> None:
        existing = self._column_names(cursor, table)
        for name, definition in columns.items():
            if name not in existing:
                cursor.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
                logger.info("Added %s.%s column", table, name)

    def _migrate_json_cache_rows(self, cursor: sqlite3.Cursor) -> None:
        """Backfill queryable columns from older cache rows that stored JSON in data."""
        columns = self._column_names(cursor, "postal_code_cache")
        if "data" not in columns:
            return

        cursor.execute(
            """
            SELECT postal_code, data
            FROM postal_code_cache
            WHERE data IS NOT NULL
            AND (segment_number IS NULL OR status IS NULL OR status = 'error')
            """
        )
        rows = cursor.fetchall()
        for row in rows:
            postal_code = row[0]
            try:
                data = json.loads(row[1])
            except (TypeError, json.JSONDecodeError):
                continue
            if "prizm_code" in data and "segment_number" not in data:
                data["segment_number"] = data["prizm_code"]
            avg_income = self._parse_currency_to_int(data.get("average_household_income"))
            avg_net_worth = self._parse_currency_to_int(data.get("average_household_net_worth"))
            avg_net_worth_amount = self._parse_currency_to_int(data.get("average_household_net_worth_amount"))
            if isinstance(avg_net_worth_amount, str):
                avg_net_worth_amount = None
            cursor.execute(
                """
                UPDATE postal_code_cache
                SET segment_number = ?, segment_name = ?, segment_description = ?, who_they_are = ?,
                    average_household_income = ?, education = ?, urbanity = ?,
                    average_household_net_worth = ?, average_household_net_worth_amount = ?,
                    occupation = ?, diversity = ?, family_life = ?, tenure = ?, home_type = ?,
                    income_level = ?, lifestage = ?, social_group = ?, official_language = ?,
                    population = ?, households = ?, percent_total_households = ?,
                    latitude = ?, longitude = ?, geography_json = ?, attributes_json = ?,
                    message = ?, geocoder_found = ?, status = ?
                WHERE postal_code = ?
                """,
                (
                    self._blank_to_none(data.get("segment_number")),
                    self._blank_to_none(data.get("segment_name")),
                    self._blank_to_none(data.get("segment_description")),
                    self._blank_to_none(data.get("who_they_are")),
                    avg_income,
                    self._blank_to_none(data.get("education")),
                    self._blank_to_none(data.get("urbanity")),
                    avg_net_worth,
                    avg_net_worth_amount,
                    self._blank_to_none(data.get("occupation")),
                    self._blank_to_none(data.get("diversity")),
                    self._blank_to_none(data.get("family_life")),
                    self._blank_to_none(data.get("tenure")),
                    self._blank_to_none(data.get("home_type")),
                    self._blank_to_none(data.get("income_level")),
                    self._blank_to_none(data.get("lifestage")),
                    self._blank_to_none(data.get("social_group")),
                    self._blank_to_none(data.get("official_language")),
                    self._blank_to_none(data.get("population")),
                    self._blank_to_none(data.get("households")),
                    self._blank_to_none(data.get("percent_total_households")),
                    data.get("latitude"),
                    data.get("longitude"),
                    json.dumps(data.get("geography")) if data.get("geography") else None,
                    json.dumps(data.get("attributes")) if data.get("attributes") else None,
                    self._blank_to_none(data.get("message")),
                    data.get("geocoder_found"),
                    self._normalize_status(data.get("status")),
                    postal_code,
                ),
            )
        if rows:
            logger.info("Backfilled %s legacy JSON cache rows", len(rows))

    def _init_database(self):
        """Initialize the SQLite database with required tables and migrations."""
        try:
            db_dir = os.path.dirname(os.path.abspath(self.db_path))
            if db_dir:
                os.makedirs(db_dir, exist_ok=True)

            with self._connect() as conn:
                cursor = conn.cursor()

                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS postal_code_cache (
                        postal_code TEXT PRIMARY KEY,
                        segment_number TEXT,
                        segment_name TEXT,
                        segment_description TEXT,
                        who_they_are TEXT,
                        average_household_income INTEGER,
                        education TEXT,
                        urbanity TEXT,
                        average_household_net_worth INTEGER,
                        average_household_net_worth_amount INTEGER,
                        occupation TEXT,
                        diversity TEXT,
                        family_life TEXT,
                        tenure TEXT,
                        home_type TEXT,
                        income_level TEXT,
                        lifestage TEXT,
                        social_group TEXT,
                        official_language TEXT,
                        population TEXT,
                        households TEXT,
                        percent_total_households TEXT,
                        latitude REAL,
                        longitude REAL,
                        geography_json TEXT,
                        attributes_json TEXT,
                        message TEXT,
                        geocoder_found BOOLEAN,
                        status TEXT NOT NULL DEFAULT 'error',
                        confirmed BOOLEAN DEFAULT 0,
                        cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        expires_at TIMESTAMP NOT NULL,
                        html_content TEXT,
                        CONSTRAINT chk_status CHECK (status IN ('success', 'error', 'invalid'))
                    )
                    """
                )

                self._add_missing_columns(
                    cursor,
                    "postal_code_cache",
                    {
                        "historical_source": "TEXT",
                        "segment_number": "TEXT",
                        "segment_name": "TEXT",
                        "segment_description": "TEXT",
                        "who_they_are": "TEXT",
                        "average_household_income": "INTEGER",
                        "education": "TEXT",
                        "urbanity": "TEXT",
                        "average_household_net_worth": "INTEGER",
                        "average_household_net_worth_amount": "INTEGER",
                        "occupation": "TEXT",
                        "diversity": "TEXT",
                        "family_life": "TEXT",
                        "tenure": "TEXT",
                        "home_type": "TEXT",
                        "income_level": "TEXT",
                        "lifestage": "TEXT",
                        "social_group": "TEXT",
                        "official_language": "TEXT",
                        "population": "TEXT",
                        "households": "TEXT",
                        "percent_total_households": "TEXT",
                        "latitude": "REAL",
                        "longitude": "REAL",
                        "geography_json": "TEXT",
                        "attributes_json": "TEXT",
                        "message": "TEXT",
                        "geocoder_found": "BOOLEAN",
                        "status": "TEXT DEFAULT 'error'",
                        "confirmed": "BOOLEAN DEFAULT 0",
                        "cached_at": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
                        "expires_at": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
                        "html_content": "TEXT",
                    },
                )

                cursor.execute("CREATE INDEX IF NOT EXISTS idx_expires_at ON postal_code_cache (expires_at)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_status ON postal_code_cache (status)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_confirmed ON postal_code_cache (confirmed)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_segment_number ON postal_code_cache (segment_number)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_cached_at ON postal_code_cache (cached_at)")
                self._migrate_json_cache_rows(cursor)

                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS lookup_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        postal_code TEXT,
                        status TEXT NOT NULL DEFAULT 'error',
                        source TEXT NOT NULL DEFAULT 'unknown',
                        endpoint TEXT,
                        batch_id TEXT,
                        message TEXT,
                        from_cache BOOLEAN DEFAULT 0,
                        duration_ms INTEGER
                    )
                    """
                )
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_lookup_events_requested_at ON lookup_events (requested_at)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_lookup_events_status ON lookup_events (status)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_lookup_events_source ON lookup_events (source)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_lookup_events_postal_code ON lookup_events (postal_code)")

                cursor.executescript("""
                    CREATE TABLE IF NOT EXISTS capture_history (
                        postal_code TEXT PRIMARY KEY, captured_at TEXT NOT NULL
                    );
                    INSERT OR IGNORE INTO capture_history
                        SELECT postal_code, COALESCE(cached_at, CURRENT_TIMESTAMP)
                        FROM postal_code_cache WHERE status='success';
                    CREATE TRIGGER IF NOT EXISTS remember_prizm_capture_insert
                    AFTER INSERT ON postal_code_cache WHEN NEW.status='success'
                    BEGIN
                        INSERT OR IGNORE INTO capture_history VALUES
                            (NEW.postal_code, COALESCE(NEW.cached_at, CURRENT_TIMESTAMP));
                    END;
                    CREATE TRIGGER IF NOT EXISTS remember_prizm_capture_update
                    AFTER UPDATE ON postal_code_cache WHEN NEW.status='success'
                    BEGIN
                        INSERT OR IGNORE INTO capture_history VALUES
                            (NEW.postal_code, COALESCE(NEW.cached_at, CURRENT_TIMESTAMP));
                    END;
                """)

                conn.commit()
                logger.info("Cache database initialized at %s", self.db_path)

        except sqlite3.Error as e:
            logger.error("Failed to initialize cache database: %s", e)
            raise

    def get_cached_data(self, postal_code: str) -> Optional[Dict[Any, Any]]:
        """Retrieve cached data for a postal code if it exists and hasn't expired."""
        try:
            postal_code = self._normalize_postal_code(postal_code)

            with self._connect() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT *
                    FROM postal_code_cache
                    WHERE postal_code = ? AND expires_at > datetime('now')
                    """,
                    (postal_code,),
                )
                row = cursor.fetchone()

                if not row:
                    logger.info("Cache miss for postal code %s", postal_code)
                    return None

                cached_data = self._row_to_response_dict(row)
                logger.info("Cache hit for postal code %s (cached at %s)", postal_code, row["cached_at"])
                cached_data["_cache_info"] = {
                    "cached_at": row["cached_at"],
                    "from_cache": True,
                    "has_html": bool(row["html_content"]),
                    "confirmed": bool(row["confirmed"]),
                }
                return cached_data

        except sqlite3.Error as e:
            logger.error("Error retrieving cached data for %s: %s", postal_code, e)
            return None

    def _row_to_response_dict(self, row: sqlite3.Row) -> Dict[str, Any]:
        geography = json.loads(row["geography_json"]) if row["geography_json"] else None
        attributes = json.loads(row["attributes_json"]) if row["attributes_json"] else None

        data = {
            "postal_code": row["postal_code"],
            "segment_number": row["segment_number"],
            "segment_name": row["segment_name"],
            "segment_description": row["segment_description"],
            "who_they_are": row["who_they_are"],
            "average_household_income": self._format_currency_from_int(row["average_household_income"]),
            "education": row["education"],
            "urbanity": row["urbanity"],
            "average_household_net_worth": self._format_currency_from_int(row["average_household_net_worth"]),
            "average_household_net_worth_amount": row["average_household_net_worth_amount"],
            "occupation": row["occupation"],
            "diversity": row["diversity"],
            "family_life": row["family_life"],
            "tenure": row["tenure"],
            "home_type": row["home_type"],
            "income_level": row["income_level"],
            "lifestage": row["lifestage"],
            "social_group": row["social_group"],
            "official_language": row["official_language"],
            "population": row["population"],
            "households": row["households"],
            "percent_total_households": row["percent_total_households"],
            "latitude": row["latitude"],
            "longitude": row["longitude"],
            "geography": geography,
            "attributes": attributes,
            "message": row["message"],
            "geocoder_found": bool(row["geocoder_found"]) if row["geocoder_found"] is not None else None,
            "status": row["status"],
        }
        return {k: v for k, v in data.items() if v is not None}

    def cache_data(
        self,
        postal_code: str,
        data: Dict[Any, Any],
        custom_duration_days: Optional[int] = None,
        html_content: Optional[str] = None,
    ) -> bool:
        """Cache data for a postal code."""
        try:
            postal_code = self._normalize_postal_code(postal_code)
            data_to_cache = data.copy()
            data_to_cache.pop("_cache_info", None)

            duration_days = custom_duration_days if custom_duration_days is not None else self.cache_duration_days
            expires_at = datetime.now() + timedelta(days=duration_days)

            if "prizm_code" in data_to_cache and "segment_number" not in data_to_cache:
                data_to_cache["segment_number"] = data_to_cache["prizm_code"]

            avg_income = self._parse_currency_to_int(data_to_cache.get("average_household_income"))
            avg_net_worth = self._parse_currency_to_int(data_to_cache.get("average_household_net_worth"))
            avg_net_worth_amount = self._parse_currency_to_int(data_to_cache.get("average_household_net_worth_amount"))
            if isinstance(avg_net_worth_amount, str):
                avg_net_worth_amount = None

            status = self._normalize_status(data_to_cache.get("status"))
            geography = data_to_cache.get("geography")
            attributes = data_to_cache.get("attributes")

            with self._connect() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    INSERT OR REPLACE INTO postal_code_cache (
                        postal_code, segment_number, segment_name, segment_description,
                        who_they_are, average_household_income, education, urbanity,
                        average_household_net_worth, average_household_net_worth_amount,
                        occupation, diversity, family_life, tenure, home_type,
                        income_level, lifestage, social_group, official_language,
                        population, households, percent_total_households,
                        latitude, longitude, geography_json, attributes_json,
                        message, geocoder_found, status, confirmed, expires_at, html_content
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        postal_code,
                        self._blank_to_none(data_to_cache.get("segment_number")),
                        self._blank_to_none(data_to_cache.get("segment_name")),
                        self._blank_to_none(data_to_cache.get("segment_description")),
                        self._blank_to_none(data_to_cache.get("who_they_are")),
                        avg_income,
                        self._blank_to_none(data_to_cache.get("education")),
                        self._blank_to_none(data_to_cache.get("urbanity")),
                        avg_net_worth,
                        avg_net_worth_amount,
                        self._blank_to_none(data_to_cache.get("occupation")),
                        self._blank_to_none(data_to_cache.get("diversity")),
                        self._blank_to_none(data_to_cache.get("family_life")),
                        self._blank_to_none(data_to_cache.get("tenure")),
                        self._blank_to_none(data_to_cache.get("home_type")),
                        self._blank_to_none(data_to_cache.get("income_level")),
                        self._blank_to_none(data_to_cache.get("lifestage")),
                        self._blank_to_none(data_to_cache.get("social_group")),
                        self._blank_to_none(data_to_cache.get("official_language")),
                        self._blank_to_none(data_to_cache.get("population")),
                        self._blank_to_none(data_to_cache.get("households")),
                        self._blank_to_none(data_to_cache.get("percent_total_households")),
                        data_to_cache.get("latitude"),
                        data_to_cache.get("longitude"),
                        json.dumps(geography) if geography else None,
                        json.dumps(attributes) if attributes else None,
                        self._blank_to_none(data_to_cache.get("message")),
                        data_to_cache.get("geocoder_found"),
                        status,
                        False,
                        expires_at,
                        html_content,
                    ),
                )
                conn.commit()
                logger.info(
                    "Cached data for postal code %s (expires: %s, duration: %s days, has_html: %s)",
                    postal_code,
                    expires_at,
                    duration_days,
                    html_content is not None,
                )
                return True

        except sqlite3.Error as e:
            logger.error("Error caching data for %s: %s", postal_code, e)
            return False

    def _blank_to_none(self, value: Any) -> Optional[Any]:
        if value is None:
            return None
        if isinstance(value, str) and value.strip() == "":
            return None
        return value

    def record_lookup_event(
        self,
        postal_code: str,
        status: str,
        source: str,
        endpoint: Optional[str] = None,
        batch_id: Optional[str] = None,
        message: Optional[str] = None,
        from_cache: bool = False,
        duration_ms: Optional[int] = None,
    ) -> None:
        """Record a lookup attempt for dashboard/reporting metrics."""
        try:
            with self._connect() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO lookup_events (
                        postal_code, status, source, endpoint, batch_id, message, from_cache, duration_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        self._normalize_postal_code(postal_code),
                        self._normalize_status(status),
                        source,
                        endpoint,
                        batch_id,
                        message,
                        bool(from_cache),
                        duration_ms,
                    ),
                )
                conn.commit()
        except sqlite3.Error as e:
            logger.warning("Failed to record lookup event for %s: %s", postal_code, e)

    def confirm_data(self, postal_code: str) -> bool:
        try:
            postal_code = self._normalize_postal_code(postal_code)
            with self._connect() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    UPDATE postal_code_cache
                    SET confirmed = 1
                    WHERE postal_code = ? AND expires_at > datetime('now')
                    """,
                    (postal_code,),
                )
                conn.commit()
                return cursor.rowcount > 0
        except sqlite3.Error as e:
            logger.error("Error confirming data for %s: %s", postal_code, e)
            return False

    def unconfirm_data(self, postal_code: str) -> bool:
        try:
            postal_code = self._normalize_postal_code(postal_code)
            with self._connect() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    UPDATE postal_code_cache
                    SET confirmed = 0
                    WHERE postal_code = ? AND expires_at > datetime('now')
                    """,
                    (postal_code,),
                )
                conn.commit()
                return cursor.rowcount > 0
        except sqlite3.Error as e:
            logger.error("Error unconfirming data for %s: %s", postal_code, e)
            return False

    def get_unconfirmed_entries(self, limit: int = 100) -> List[Dict[str, Any]]:
        try:
            with self._connect() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT postal_code, segment_number, segment_name, cached_at
                    FROM postal_code_cache
                    WHERE confirmed = 0 AND expires_at > datetime('now') AND status = 'success'
                    ORDER BY cached_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
                return [dict(row) for row in cursor.fetchall()]
        except sqlite3.Error as e:
            logger.error("Error getting unconfirmed entries: %s", e)
            return []

    def get_cached_html(self, postal_code: str) -> Optional[str]:
        try:
            postal_code = self._normalize_postal_code(postal_code)
            with self._connect() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT html_content
                    FROM postal_code_cache
                    WHERE postal_code = ? AND expires_at > datetime('now')
                    """,
                    (postal_code,),
                )
                row = cursor.fetchone()
                return row["html_content"] if row and row["html_content"] else None
        except sqlite3.Error as e:
            logger.error("Error retrieving cached HTML for %s: %s", postal_code, e)
            return None

    def cleanup_expired_cache(self) -> int:
        try:
            with self._connect() as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM postal_code_cache WHERE expires_at <= datetime('now')")
                deleted_count = cursor.rowcount
                conn.commit()
                return deleted_count
        except sqlite3.Error as e:
            logger.error("Error cleaning up expired cache: %s", e)
            return 0

    def get_cache_stats(self) -> Dict[str, Any]:
        try:
            with self._connect() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) FROM postal_code_cache")
                total_entries = cursor.fetchone()[0]

                cursor.execute("SELECT COUNT(*) FROM postal_code_cache WHERE expires_at > datetime('now')")
                valid_entries = cursor.fetchone()[0]

                cursor.execute(
                    "SELECT COUNT(*) FROM postal_code_cache WHERE expires_at > datetime('now') AND confirmed = 1"
                )
                confirmed_entries = cursor.fetchone()[0]

                cursor.execute(
                    """
                    SELECT status, COUNT(*)
                    FROM postal_code_cache
                    WHERE expires_at > datetime('now')
                    GROUP BY status
                    """
                )
                status_counts = {row[0]: row[1] for row in cursor.fetchall()}

                cursor.execute(
                    """
                    SELECT MIN(cached_at), MAX(cached_at)
                    FROM postal_code_cache
                    WHERE expires_at > datetime('now')
                    """
                )
                oldest, newest = cursor.fetchone()

                cursor.execute("SELECT COUNT(*) FROM lookup_events")
                lookup_events = cursor.fetchone()[0]

                db_size = os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0

                return {
                    "total_entries": total_entries,
                    "valid_entries": valid_entries,
                    "expired_entries": total_entries - valid_entries,
                    "confirmed_entries": confirmed_entries,
                    "unconfirmed_entries": valid_entries - confirmed_entries,
                    "status_breakdown": status_counts,
                    "oldest_entry": oldest,
                    "newest_entry": newest,
                    "database_size_bytes": db_size,
                    "cache_duration_days": self.cache_duration_days,
                    "lookup_events": lookup_events,
                }
        except sqlite3.Error as e:
            logger.error("Error getting cache stats: %s", e)
            return {}

    def list_cache_entries(
        self,
        status: Optional[str] = None,
        search: Optional[str] = None,
        limit: int = 500,
        offset: int = 0,
        include_expired: bool = False,
    ) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 5000))
        offset = max(0, int(offset))
        conditions = [] if include_expired else ["expires_at > datetime('now')"]
        params: list[Any] = []

        if status:
            conditions.append("status = ?")
            params.append(self._normalize_status(status))
        if search:
            like = f"%{search.strip().upper()}%"
            conditions.append("(postal_code LIKE ? OR segment_name LIKE ? OR segment_number LIKE ? OR message LIKE ?)")
            params.extend([like, like, like, like])

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        query = f"""
            SELECT *
            FROM postal_code_cache
            {where_clause}
            ORDER BY cached_at DESC, postal_code ASC
            LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])

        try:
            with self._connect() as conn:
                cursor = conn.cursor()
                cursor.execute(query, params)
                return [self._dashboard_row(row) for row in cursor.fetchall()]
        except sqlite3.Error as e:
            logger.error("Error listing cache entries: %s", e)
            return []

    def _dashboard_row(self, row: sqlite3.Row) -> Dict[str, Any]:
        data = self._row_to_response_dict(row)
        amount = average_household_net_worth_amount(data.get("segment_number"))
        if amount is not None:
            if not data.get("average_household_net_worth"):
                data["average_household_net_worth"] = average_household_net_worth(data.get("segment_number"))
            if data.get("average_household_net_worth_amount") is None:
                data["average_household_net_worth_amount"] = amount
        data["net_worth_source"] = "Historical segment reference" if data.get("average_household_net_worth") else "Unavailable"
        data["data_source"] = ("Historical import: " + row["historical_source"]
                               if row["historical_source"] and row["cached_at"] is None else "Current dataset")
        data.update(
            {
                "cached_at": row["cached_at"],
                "expires_at": row["expires_at"],
                "confirmed": bool(row["confirmed"]),
            }
        )
        return data

    def get_daily_cache_counts(self, days: int = 14) -> List[Dict[str, Any]]:
        try:
            with self._connect() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT date(cached_at) day,
                           COUNT(*) total,
                           SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) successful,
                           SUM(CASE WHEN status != 'success' THEN 1 ELSE 0 END) failed
                    FROM postal_code_cache
                    WHERE cached_at >= datetime('now', ?)
                    GROUP BY date(cached_at)
                    ORDER BY day DESC
                    """,
                    (f"-{int(days)} days",),
                )
                return [dict(row) for row in cursor.fetchall()]
        except sqlite3.Error as e:
            logger.error("Error getting daily cache counts: %s", e)
            return []

    def get_lookup_event_summary(self, days: int = 7) -> Dict[str, Any]:
        try:
            with self._connect() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT COUNT(*) lookups,
                           SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) successful,
                           SUM(CASE WHEN status != 'success' THEN 1 ELSE 0 END) failed,
                           SUM(CASE WHEN from_cache = 1 THEN 1 ELSE 0 END) cache_hits,
                           SUM(CASE WHEN source = 'upstream' THEN 1 ELSE 0 END) upstream_attempts,
                           COUNT(DISTINCT postal_code) unique_postal_codes,
                           SUM(CASE WHEN source = 'upstream' AND status = 'success' THEN 1 ELSE 0 END) upstream_successful,
                           SUM(CASE WHEN source = 'upstream' AND status != 'success' THEN 1 ELSE 0 END) upstream_failed,
                           SUM(CASE WHEN source = 'upstream' AND status = 'success' AND NOT EXISTS (
                               SELECT 1 FROM lookup_events prior
                               WHERE prior.postal_code = lookup_events.postal_code
                               AND prior.status = 'success' AND prior.id < lookup_events.id
                           ) THEN 1 ELSE 0 END) newly_captured
                    FROM lookup_events
                    WHERE requested_at >= datetime('now', ?)
                    """,
                    (f"-{int(days)} days",),
                )
                row = dict(cursor.fetchone())

                cursor.execute(
                    """
                    SELECT date(requested_at) day,
                           COUNT(*) lookups,
                           SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) successful,
                           SUM(CASE WHEN status != 'success' THEN 1 ELSE 0 END) failed,
                           SUM(CASE WHEN from_cache = 1 THEN 1 ELSE 0 END) cache_hits,
                           SUM(CASE WHEN source = 'upstream' THEN 1 ELSE 0 END) upstream_attempts,
                           COUNT(DISTINCT postal_code) unique_postal_codes,
                           SUM(CASE WHEN source = 'upstream' AND status = 'success' THEN 1 ELSE 0 END) upstream_successful,
                           SUM(CASE WHEN source = 'upstream' AND status != 'success' THEN 1 ELSE 0 END) upstream_failed,
                           SUM(CASE WHEN source = 'upstream' AND status = 'success' AND NOT EXISTS (
                               SELECT 1 FROM lookup_events prior
                               WHERE prior.postal_code = lookup_events.postal_code
                               AND prior.status = 'success' AND prior.id < lookup_events.id
                           ) THEN 1 ELSE 0 END) newly_captured
                    FROM lookup_events
                    WHERE requested_at >= datetime('now', ?)
                    GROUP BY date(requested_at)
                    ORDER BY day DESC
                    """,
                    (f"-{int(days)} days",),
                )
                row["by_day"] = [dict(r) for r in cursor.fetchall()]
                return {k: (v or 0) for k, v in row.items()}
        except sqlite3.Error as e:
            logger.error("Error getting lookup event summary: %s", e)
            return {}

    def get_dashboard_summary(self) -> Dict[str, Any]:
        return {
            "cache_stats": self.get_cache_stats(),
            "daily_cache_counts": self.get_daily_cache_counts(30),
            "lookup_events_7d": self.get_lookup_event_summary(7),
            "recent_failures": self.list_lookup_events(failures_only=True, days=7, limit=50),
        }

    def list_lookup_events(self, failures_only=False, days=7, limit=500, offset=0):
        conditions = ["requested_at >= datetime('now', ?)"]
        if failures_only:
            conditions.append("status != 'success'")
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM lookup_events WHERE {' AND '.join(conditions)} "
                "ORDER BY requested_at DESC, id DESC LIMIT ? OFFSET ?",
                (f"-{max(1, min(int(days), 3650))} days", max(1, min(int(limit), 5000)), max(0, int(offset))),
            ).fetchall()
            return [dict(row) for row in rows]

    @staticmethod
    def _csv_value(value):
        if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')):
            return "'" + value
        return value

    def export_failures_csv(self, days=7):
        output = io.StringIO()
        fields = ['postal_code', 'requested_at', 'status', 'source', 'message', 'endpoint', 'batch_id']
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        offset = 0
        while True:
            rows = self.list_lookup_events(failures_only=True, days=days, limit=5000, offset=offset)
            for row in rows:
                writer.writerow({field: self._csv_value(row.get(field, '')) for field in fields})
            if len(rows) < 5000:
                break
            offset += len(rows)
        return output.getvalue()

    def export_cache_csv(self, include_expired: bool = False) -> str:
        output = io.StringIO()
        fieldnames = [
            "postal_code",
            "status",
            "message",
            "segment_number",
            "segment_name",
            "segment_description",
            "who_they_are",
            "average_household_income",
            "average_household_net_worth",
            "average_household_net_worth_amount",
            "net_worth_source",
            "data_source",
            "education",
            "urbanity",
            "occupation",
            "diversity",
            "family_life",
            "tenure",
            "home_type",
            "income_level",
            "lifestage",
            "social_group",
            "official_language",
            "population",
            "households",
            "percent_total_households",
            "latitude",
            "longitude",
            "cached_at",
            "expires_at",
            "confirmed",
        ]
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        offset = 0
        while True:
            rows = self.list_cache_entries(limit=5000, offset=offset, include_expired=include_expired)
            for row in rows:
                writer.writerow({field: self._csv_value(row.get(field, "")) for field in fieldnames})
            if len(rows) < 5000:
                break
            offset += len(rows)
        return output.getvalue()

    def clear_cache(self) -> bool:
        try:
            with self._connect() as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM postal_code_cache")
                conn.commit()
                return True
        except sqlite3.Error as e:
            logger.error("Error clearing cache: %s", e)
            return False

    def is_cached(self, postal_code: str) -> bool:
        return self.get_cached_data(postal_code) is not None

    def delete_cached_data(self, postal_code: str) -> bool:
        try:
            postal_code = self._normalize_postal_code(postal_code)
            with self._connect() as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM postal_code_cache WHERE postal_code = ?", (postal_code,))
                conn.commit()
                return cursor.rowcount > 0
        except sqlite3.Error as e:
            logger.error("Error deleting cached data for %s: %s", postal_code, e)
            return False


# Global cache manager instance
cache_manager = CacheManager()
