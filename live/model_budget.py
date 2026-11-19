"""Persistent request accounting, with an optional request ceiling."""

import json
import os
import sqlite3
import time
from pathlib import Path


class ModelBudget:
    @staticmethod
    def parse_limit(value):
        if isinstance(value, str) and value.strip().lower() == "unlimited":
            return None
        limit = int(value)
        if limit < 0:
            raise ValueError("request limit must be non-negative or unlimited")
        return limit

    def __init__(self, path=None, limit=None):
        self.path = Path(path or os.getenv("ZSJH_LIVE_MODEL_USAGE_DB", ".zsjh/live-model-usage.sqlite3"))
        self.limit = self.parse_limit(limit if limit is not None else os.getenv("ZSJH_LIVE_MODEL_REQUEST_LIMIT", "6"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS requests(id INTEGER PRIMARY KEY, created REAL, usage TEXT)")
        self.path.chmod(0o600)

    def reserve(self):
        with sqlite3.connect(self.path, timeout=10) as db:
            db.execute("BEGIN IMMEDIATE")
            if self.limit is not None and db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] >= self.limit:
                raise RuntimeError("model request limit reached")
            return db.execute("INSERT INTO requests(created) VALUES(?)", (time.time(),)).lastrowid

    def record(self, request_id, usage):
        with sqlite3.connect(self.path, timeout=10) as db:
            db.execute("UPDATE requests SET usage=? WHERE id=?", (json.dumps(usage), request_id))
