"""Repository layer for companion-style categorized user memories."""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any


@dataclass
class MemoryItem:
    category: str
    content: str
    priority: float = 0.6
    pinned: bool = False
    confidence: float = 0.8
    source: str = "summary"
    note: str = ""
    tags: list[str] = field(default_factory=list)
    status: str = "active"
    ttl_days: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    last_used_at: float = 0.0
    use_count: int = 0
    expires_at: float | None = None


class CompanionRepository:
    CATEGORIES = ("profile", "agreement", "event", "fact", "knowledge_ref")

    @staticmethod
    def _activity_ts(item: dict[str, Any]) -> float:
        return float(item.get("last_used_at") or 0) or float(item.get("updated_at") or 0) or float(item.get("created_at") or 0)

    @classmethod
    def _category_forgetting_rule(cls, category: str, config: dict[str, Any]) -> dict[str, Any]:
        forgetting = config.get("forgetting_settings", {})
        return {
            "stale_after_days": int(forgetting.get(f"{category}_stale_after_days", 0)),
            "archive_after_days": int(forgetting.get(f"{category}_archive_after_days", 0)),
            "fallback_ttl_days": int(forgetting.get(f"{category}_ttl_days", 0)),
            "protect_pinned": bool(forgetting.get("protect_pinned", True)),
        }

    def __init__(self, data_dir: str):
        self._db_path = os.path.join(data_dir, "companion_memory.db")
        self._local = threading.local()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(self._db_path, check_same_thread=False)
            self._local.conn.row_factory = sqlite3.Row
            self._local.conn.execute("PRAGMA journal_mode=WAL")
        return self._local.conn

    @contextmanager
    def _cursor(self):
        conn = self._get_conn()
        cur = conn.cursor()
        try:
            yield cur
            conn.commit()
        finally:
            cur.close()

    def _init_db(self) -> None:
        with self._cursor() as c:
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    category TEXT NOT NULL,
                    content TEXT NOT NULL,
                    priority REAL NOT NULL DEFAULT 0.6,
                    pinned INTEGER NOT NULL DEFAULT 0,
                    confidence REAL NOT NULL DEFAULT 0.8,
                    source TEXT NOT NULL DEFAULT 'summary',
                    note TEXT NOT NULL DEFAULT '',
                    tags TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active',
                    ttl_days INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    last_used_at REAL NOT NULL DEFAULT 0,
                    use_count INTEGER NOT NULL DEFAULT 0,
                    expires_at REAL,
                    embedding BLOB,
                    embedding_model TEXT NOT NULL DEFAULT ''
                )
                """
            )
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL
                )
                """
            )
            c.execute("PRAGMA table_info(memory_items)")
            columns = {row[1] for row in c.fetchall()}
            if "embedding" not in columns:
                c.execute("ALTER TABLE memory_items ADD COLUMN embedding BLOB")
            if "embedding_model" not in columns:
                c.execute("ALTER TABLE memory_items ADD COLUMN embedding_model TEXT NOT NULL DEFAULT ''")
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS session_state (
                    session_id TEXT PRIMARY KEY,
                    turn_count INTEGER NOT NULL DEFAULT 0,
                    last_summary_turn INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL
                )
                """
            )

    def add_memory(self, item: MemoryItem) -> int:
        if item.category not in self.CATEGORIES:
            raise ValueError(f"unsupported category: {item.category}")
        if item.ttl_days > 0 and item.expires_at is None:
            item.expires_at = time.time() + item.ttl_days * 86400
        with self._cursor() as c:
            c.execute(
                """
                INSERT INTO memory_items (
                    category, content, priority, pinned, confidence, source, note,
                    tags, status, ttl_days, created_at, updated_at, last_used_at,
                    use_count, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.category,
                    item.content.strip(),
                    item.priority,
                    1 if item.pinned else 0,
                    item.confidence,
                    item.source,
                    item.note,
                    ",".join(item.tags),
                    item.status,
                    item.ttl_days,
                    item.created_at,
                    item.updated_at,
                    item.last_used_at,
                    item.use_count,
                    item.expires_at,
                ),
            )
            item_id = int(c.lastrowid)
        self.log_event("add_memory", f"{item.category}#{item_id}")
        return item_id

    def update_memory(self, item_id: int, **updates: Any) -> bool:
        allowed = {
            "category", "content", "priority", "pinned", "confidence",
            "source", "note", "tags", "status", "ttl_days", "last_used_at",
            "use_count", "expires_at", "embedding", "embedding_model",
        }
        clean: dict[str, Any] = {}
        for key, value in updates.items():
            if key not in allowed or value is None:
                continue
            if key == "tags" and isinstance(value, list):
                value = ",".join(str(x).strip() for x in value if str(x).strip())
            if key == "pinned":
                value = 1 if value else 0
            clean[key] = value
        if "ttl_days" in clean and "expires_at" not in clean:
            ttl_days = int(clean["ttl_days"])
            clean["expires_at"] = time.time() + ttl_days * 86400 if ttl_days > 0 else None
        if not clean:
            return False
        clean["updated_at"] = time.time()
        set_clause = ", ".join(f"{k}=?" for k in clean)
        values = list(clean.values()) + [item_id]
        with self._cursor() as c:
            c.execute(f"UPDATE memory_items SET {set_clause} WHERE id = ?", values)
            ok = c.rowcount > 0
        if ok:
            self.log_event("update_memory", f"id={item_id}")
        return ok

    def get_memory(self, item_id: int) -> dict[str, Any] | None:
        with self._cursor() as c:
            c.execute("SELECT * FROM memory_items WHERE id = ?", (item_id,))
            row = c.fetchone()
        return self._row_to_memory(row) if row else None

    def list_memories(
        self,
        category: str = "",
        status: str = "active",
        limit: int = 200,
        include_expired: bool = False,
    ) -> list[dict[str, Any]]:
        clauses = []
        params: list[Any] = []
        if category:
            clauses.append("category = ?")
            params.append(category)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if not include_expired:
            clauses.append("(expires_at IS NULL OR expires_at > ?)")
            params.append(time.time())
        sql = "SELECT * FROM memory_items"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY pinned DESC, priority DESC, updated_at DESC LIMIT ?"
        params.append(limit)
        with self._cursor() as c:
            c.execute(sql, params)
            rows = c.fetchall()
        results = [self._row_to_memory(row) for row in rows]
        for r in results:
            r.pop("embedding", None)
        return results

    def count_pinned(self, category: str | None = None) -> int:
        with self._cursor() as c:
            if category:
                c.execute(
                    "SELECT COUNT(*) AS cnt FROM memory_items WHERE pinned = 1 AND status != 'archived' AND category = ?",
                    (category,),
                )
            else:
                c.execute(
                    "SELECT COUNT(*) AS cnt FROM memory_items WHERE pinned = 1 AND status != 'archived'"
                )
            row = c.fetchone()
        return int(row["cnt"]) if row else 0

    def list_pinned(self, limit: int = 100, category: str | None = None) -> list[dict[str, Any]]:
        with self._cursor() as c:
            if category:
                c.execute(
                    """
                    SELECT * FROM memory_items
                    WHERE pinned = 1 AND status != 'archived' AND category = ?
                    ORDER BY priority ASC, updated_at ASC
                    LIMIT ?
                    """,
                    (category, limit),
                )
            else:
                c.execute(
                    """
                    SELECT * FROM memory_items
                    WHERE pinned = 1 AND status != 'archived'
                    ORDER BY priority ASC, updated_at ASC
                    LIMIT ?
                    """,
                    (limit,),
                )
            rows = c.fetchall()
        return [self._row_to_memory(row) for row in rows]

    def search_memories(
        self,
        query: str,
        categories: list[str] | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        normalized_query = " ".join(query.strip().lower().split())
        words = [w.strip().lower() for w in query.split() if w.strip()]
        items = self.list_memories(limit=500)
        scored: list[tuple[float, dict[str, Any]]] = []
        for item in items:
            text = f"{item['content']} {item['note']} {' '.join(item['tags'])}".lower()
            if categories and item["category"] not in categories:
                continue
            score = float(item["priority"])
            matched = False
            if normalized_query:
                if item["content"].strip().lower() == normalized_query:
                    score += 2.5
                    matched = True
                elif normalized_query in text:
                    score += 1.6
                    matched = True
            for word in words:
                if word in text:
                    score += 0.45
                    matched = True
            if item["pinned"]:
                score += 0.25
            if item["category"] == "agreement":
                score += 0.1
            if normalized_query and not matched:
                continue
            scored.append((score, item))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [{**item, "score": round(score, 3)} for score, item in scored[:limit]]

    def list_embeddings(
        self,
        categories: list[str] | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        clauses = ["status = 'active'", "embedding IS NOT NULL", "(expires_at IS NULL OR expires_at > ?)"]
        params: list[Any] = [time.time()]
        if categories:
            placeholders = ",".join("?" for _ in categories)
            clauses.append(f"category IN ({placeholders})")
            params.extend(categories)
        sql = (
            "SELECT id, category, content, note, tags, priority, pinned, confidence, "
            "updated_at, last_used_at, use_count, embedding, embedding_model "
            "FROM memory_items WHERE "
            + " AND ".join(clauses)
            + " ORDER BY pinned DESC, priority DESC, updated_at DESC LIMIT ?"
        )
        params.append(limit)
        with self._cursor() as c:
            c.execute(sql, params)
            rows = c.fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["pinned"] = bool(item["pinned"])
            item["tags"] = [x for x in str(item.get("tags", "")).split(",") if x]
            result.append(item)
        return result

    def touch_memories(self, memory_ids: list[int]) -> None:
        if not memory_ids:
            return
        now = time.time()
        with self._cursor() as c:
            c.executemany(
                """
                UPDATE memory_items
                SET last_used_at = ?, use_count = use_count + 1, updated_at = updated_at
                WHERE id = ?
                """,
                [(now, mid) for mid in memory_ids],
            )

    def merge_or_add(
        self,
        category: str,
        content: str,
        priority: float = 0.65,
        confidence: float = 0.8,
        pinned: bool = False,
        source: str = "summary",
        note: str = "",
        ttl_days: int = 0,
        tags: list[str] | None = None,
    ) -> tuple[str, int]:
        normalized = " ".join(content.strip().split())
        existing = self.list_memories(category=category, limit=200, include_expired=True)
        for item in existing:
            if item["content"].strip() == normalized:
                self.update_memory(
                    item["id"],
                    priority=max(item["priority"], priority),
                    confidence=max(item["confidence"], confidence),
                    pinned=item["pinned"] or pinned,
                    note=note or item["note"],
                    ttl_days=ttl_days if ttl_days > 0 else item["ttl_days"],
                    tags=tags or item["tags"],
                    status="active",
                )
                return "updated", item["id"]
        item_id = self.add_memory(
            MemoryItem(
                category=category,
                content=normalized,
                priority=priority,
                pinned=pinned,
                confidence=confidence,
                source=source,
                note=note,
                ttl_days=ttl_days,
                tags=tags or [],
            )
        )
        return "created", item_id

    def run_forgetting(self, config: dict[str, Any]) -> dict[str, int]:
        now = time.time()
        counts = {"stale": 0, "archived": 0, "expired": 0}
        items = self.list_memories(status="", limit=5000, include_expired=True)
        for item in items:
            item_id = int(item["id"])
            status = str(item.get("status", "active"))
            if status == "archived":
                continue
            expires_at = item.get("expires_at")
            if expires_at is not None and float(expires_at) <= now:
                if self.update_memory(item_id, status="archived"):
                    counts["expired"] += 1
                continue

            rule = self._category_forgetting_rule(str(item.get("category", "")), config)
            if rule["protect_pinned"] and bool(item.get("pinned")):
                continue

            activity_ts = self._activity_ts(item)
            ttl_days = int(item.get("ttl_days") or 0) or int(rule["fallback_ttl_days"])
            if ttl_days > 0 and activity_ts and activity_ts <= now - ttl_days * 86400:
                if self.update_memory(item_id, status="archived"):
                    counts["archived"] += 1
                continue

            archive_days = int(rule["archive_after_days"])
            if archive_days > 0 and activity_ts and activity_ts <= now - archive_days * 86400:
                if self.update_memory(item_id, status="archived"):
                    counts["archived"] += 1
                continue

            stale_days = int(rule["stale_after_days"])
            if status == "active" and stale_days > 0 and activity_ts and activity_ts <= now - stale_days * 86400:
                if self.update_memory(item_id, status="stale"):
                    counts["stale"] += 1
        if any(counts.values()):
            self.log_event("forgetting", str(counts))
        return counts

    def explain_forgetting(self, item: dict[str, Any], config: dict[str, Any], now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        rule = self._category_forgetting_rule(str(item.get("category", "")), config)
        protected = bool(item.get("pinned")) and rule["protect_pinned"]
        activity_ts = self._activity_ts(item)
        stale_after_days = int(rule["stale_after_days"])
        archive_after_days = int(rule["archive_after_days"])
        fallback_ttl_days = int(rule["fallback_ttl_days"])
        ttl_days = int(item.get("ttl_days") or 0) or fallback_ttl_days

        def remain_days(target_ts: float | None) -> float | None:
            if not target_ts:
                return None
            return round((target_ts - now) / 86400, 2)

        stale_at = activity_ts + stale_after_days * 86400 if stale_after_days > 0 and activity_ts and not protected else None
        archive_at = activity_ts + archive_after_days * 86400 if archive_after_days > 0 and activity_ts and not protected else None
        ttl_at = activity_ts + ttl_days * 86400 if ttl_days > 0 and activity_ts and not protected else None
        expires_at = float(item["expires_at"]) if item.get("expires_at") is not None else None

        return {
            "protected": protected,
            "activity_at": activity_ts or None,
            "stale_after_days": stale_after_days,
            "archive_after_days": archive_after_days,
            "fallback_ttl_days": fallback_ttl_days,
            "ttl_days_effective": ttl_days,
            "stale_at": stale_at,
            "archive_at": archive_at,
            "ttl_at": ttl_at,
            "expires_at": expires_at,
            "stale_in_days": remain_days(stale_at),
            "archive_in_days": remain_days(archive_at),
            "ttl_in_days": remain_days(ttl_at),
            "expires_in_days": remain_days(expires_at),
        }

    def get_dashboard_stats(self) -> dict[str, Any]:
        stats = {"total": 0, "active": 0, "stale": 0, "archived": 0, "by_category": {}}
        with self._cursor() as c:
            c.execute("SELECT status, COUNT(*) AS cnt FROM memory_items GROUP BY status")
            for row in c.fetchall():
                stats[row["status"]] = row["cnt"]
                stats["total"] += row["cnt"]
            c.execute(
                "SELECT category, COUNT(*) AS cnt FROM memory_items WHERE status='active' GROUP BY category"
            )
            for row in c.fetchall():
                stats["by_category"][row["category"]] = row["cnt"]
        return stats

    def log_event(self, action: str, detail: str) -> None:
        with self._cursor() as c:
            c.execute(
                "INSERT INTO memory_events (action, detail, created_at) VALUES (?, ?, ?)",
                (action, detail, time.time()),
            )

    def list_events(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._cursor() as c:
            c.execute(
                "SELECT id, action, detail, created_at FROM memory_events ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
            rows = c.fetchall()
        return [dict(row) for row in rows]

    def get_session_state(self, session_id: str) -> dict[str, Any]:
        with self._cursor() as c:
            c.execute("SELECT * FROM session_state WHERE session_id = ?", (session_id,))
            row = c.fetchone()
        if not row:
            return {"session_id": session_id, "turn_count": 0, "last_summary_turn": 0, "updated_at": 0.0}
        return dict(row)

    def update_session_state(self, session_id: str, turn_count: int, last_summary_turn: int | None = None) -> None:
        old = self.get_session_state(session_id)
        with self._cursor() as c:
            c.execute(
                """
                INSERT INTO session_state (session_id, turn_count, last_summary_turn, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    turn_count = excluded.turn_count,
                    last_summary_turn = excluded.last_summary_turn,
                    updated_at = excluded.updated_at
                """,
                (
                    session_id,
                    turn_count,
                    old["last_summary_turn"] if last_summary_turn is None else last_summary_turn,
                    time.time(),
                ),
            )

    def reset_session_state(self, session_id: str) -> None:
        with self._cursor() as c:
            c.execute("DELETE FROM session_state WHERE session_id = ?", (session_id,))

    def close(self) -> None:
        if hasattr(self._local, "conn") and self._local.conn:
            self._local.conn.close()
            self._local.conn = None

    @staticmethod
    def _row_to_memory(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["pinned"] = bool(data["pinned"])
        data["tags"] = [x for x in str(data.get("tags", "")).split(",") if x]
        return data
