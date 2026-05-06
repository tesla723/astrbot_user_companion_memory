"""知识进化器：提取 → 向量化 → 去重 → 存储 + 注入匹配。"""
from __future__ import annotations

import json
import time
import numpy as np
from astrbot.api import logger
from astrbot.core.provider.provider import EmbeddingProvider
from ..engine.analyzer import ConversationAnalyzer
from ..engine.decider import EvolutionDecider
from ..engine.scheduler import EvolutionScheduler
from ..engine.validator import EvolutionValidator
from ..storage.repository import EvolutionRepository, KnowledgeEntry
from .base import BaseEvolver


class KnowledgeEvolver(BaseEvolver):
    MAX_EMBEDDING_CACHE = 500

    def __init__(self, context, config, data_dir, repository, analyzer, decider, scheduler, validator):
        super().__init__(context, config, data_dir)
        self.repo = repository
        self.analyzer = analyzer
        self.decider = decider
        self.scheduler = scheduler
        self.validator = validator
        self._conversation_buffer: dict[str, list[str]] = {}
        self._embedding_cache: dict[str, bytes] = {}
        self._embedding_provider: EmbeddingProvider | None = None
        self._embedding_provider_initialized = False

    def get_config(self) -> dict:
        return self.config.get("knowledge_evolver", {})

    def is_enabled(self) -> bool:
        return self.get_config().get("enabled", True)

    def _dbg(self, msg: str, level: str = "verbose") -> None:
        lvls = {"off": 0, "basic": 1, "verbose": 2, "trace": 3}
        if lvls.get(self.config.get("debug_settings", {}).get("debug_level", "basic"), 1) >= lvls.get(level, 1):
            logger.debug(f"[进化引擎-知识] {msg}")

    # ── 入口 ──

    async def on_conversation_turn(self, session_id: str, message: str) -> None:
        if not self.is_enabled():
            return
        trigger_mode = self.get_config().get("trigger_mode", "both")
        if session_id not in self._conversation_buffer:
            self._conversation_buffer[session_id] = []
        self._conversation_buffer[session_id].append(message)
        if trigger_mode not in ("rounds", "both"):
            return
        buf_size = self.get_config().get("conversation_buffer_size", 5)
        if len(self._conversation_buffer[session_id]) >= buf_size:
            self._dbg(f"轮数触发提取 session={session_id[:20]}...", "basic")
            await self._analyze_and_store(session_id)
            self._conversation_buffer[session_id] = []

    async def cron_scan(self) -> None:
        """定时扫描（兜底 + 时间触发共用此入口）。"""
        total = sum(len(v) for v in self._conversation_buffer.values())
        self._dbg(f"定时扫描 | 会话数={len(self._conversation_buffer)} 总消息={total}", "basic")
        for session_id in list(self._conversation_buffer.keys()):
            if self._conversation_buffer[session_id]:
                await self._analyze_and_store(session_id)
                self._conversation_buffer[session_id] = []

    # ── 核心 ──

    async def _analyze_and_store(self, session_id: str) -> None:
        buffer = self._conversation_buffer.get(session_id, [])
        conversation = "\n".join(buffer)
        if not conversation.strip():
            return
        started = time.monotonic()
        event_id = self.decider.log_event("knowledge", "extract", trigger_source=session_id, analysis_summary=conversation[:200])
        provider_id = self.get_config().get("extraction_model", "")
        items = await self.analyzer.extract_knowledge(conversation, provider_id)
        if not items:
            self.repo.update_event(event_id, status="completed", decision_detail="无可提取知识", completed_at=time.time())
            return
        accepted = self.decider.should_store_knowledge(items)
        if not accepted:
            self.repo.update_event(event_id, status="completed", decision_detail=f"过滤后无合格条目({len(items)}条)", completed_at=time.time())
            return
        stored, dup = 0, 0
        for item in accepted:
            if await self._is_duplicate(item["content"]):
                dup += 1; continue
            emb = await self._get_embedding(item["content"])
            entry = KnowledgeEntry(content=item["content"], category=item.get("category", "general"), source_conversation_id=session_id, confidence=item.get("confidence", 0.0), embedding=emb, embedding_model=self._get_embedding_model_name())
            self.repo.insert_knowledge(entry)
            stored += 1
        self.repo.update_event(event_id, status="completed", decision_detail=f"写入{stored}/{len(accepted)}条(去重跳过{dup})", completed_at=time.time())
        if stored > 0:
            self.decider.log_artifact(event_id, "knowledge_entry", json.dumps({"stored": stored, "dup": dup, "session": session_id}, ensure_ascii=False))
        logger.info(f"[进化引擎-知识] +{stored}条 | 跳过重复{dup} | {(time.monotonic()-started)*1000:.0f}ms")

    # ── 去重 ──

    async def _is_duplicate(self, content: str) -> bool:
        threshold = self.get_config().get("dedup_threshold", 0.85)
        existing = self.repo.get_all_embeddings()
        active_entries = self.repo.list_active_knowledge(limit=500)
        if any(e["content"].strip() == content.strip() for e in active_entries):
            return True
        if not existing:
            return False
        new_emb = await self._get_embedding(content)
        if new_emb is None:
            return False
        new_vec = np.frombuffer(new_emb, dtype=np.float32)
        for entry in existing:
            if entry["embedding"]:
                exist_vec = np.frombuffer(entry["embedding"], dtype=np.float32)
                if exist_vec.shape != new_vec.shape:
                    continue
                dot = np.dot(new_vec, exist_vec)
                norm = np.linalg.norm(new_vec) * np.linalg.norm(exist_vec)
                if norm > 0 and float(dot / norm) >= threshold:
                    return True
        return False

    # ── Embedding（参考 livingmemory 模式）──

    def _init_embedding_provider(self) -> None:
        if self._embedding_provider_initialized:
            return
        self._embedding_provider_initialized = True
        emb_id = self.config.get("memory_settings", {}).get("embedding_model", "")
        if emb_id:
            provider = self.context.get_provider_by_id(emb_id)
            if provider and isinstance(provider, EmbeddingProvider):
                self._embedding_provider = provider; return
        try:
            all_emb = self.context.get_all_embedding_providers()
            if all_emb:
                self._embedding_provider = all_emb[0]; return
        except Exception:
            pass
        logger.warning("[进化引擎-知识] 无可用 Embedding Provider，降级 SHA256")

    async def _get_embedding(self, text: str) -> bytes | None:
        if text in self._embedding_cache:
            return self._embedding_cache[text]
        self._init_embedding_provider()
        if self._embedding_provider:
            try:
                vec = await self._embedding_provider.get_embedding(text)
                if vec and len(vec) > 0:
                    emb_bytes = np.array(vec, dtype=np.float32).tobytes()
                    self._cache_embedding(text, emb_bytes)
                    return emb_bytes
            except Exception as e:
                self._dbg(f"Embedding 调用失败: {e}", "basic")
        import hashlib
        h = hashlib.sha256(text.encode()).digest()
        emb_bytes = np.frombuffer(h[:128], dtype=np.float32).copy().astype(np.float32).tobytes()
        self._cache_embedding(text, emb_bytes)
        return emb_bytes

    def _cache_embedding(self, text: str, emb_bytes: bytes) -> None:
        # LRU: if cache is full, remove oldest entry
        if len(self._embedding_cache) >= self.MAX_EMBEDDING_CACHE:
            oldest = next(iter(self._embedding_cache))
            del self._embedding_cache[oldest]
        self._embedding_cache[text] = emb_bytes

    def _get_embedding_model_name(self) -> str:
        if self._embedding_provider:
            provider_config = getattr(self._embedding_provider, "provider_config", None)
            if isinstance(provider_config, dict):
                return provider_config.get("id", "embedding")
            return getattr(provider_config, "id", "embedding")
        return self.config.get("memory_settings", {}).get("embedding_model", "") or "fallback_hash"

    # ── 搜索注入 ──

    async def search_knowledge(self, query: str, top_k: int = 5) -> list[dict]:
        entries = self.repo.list_active_knowledge(limit=500)
        if not entries:
            return []
        query_emb = await self._get_embedding(query)
        if query_emb is None:
            return [{"id": e["id"], "content": e["content"], "score": 0.5} for e in entries if query.lower() in e["content"].lower()][:top_k]
        query_vec = np.frombuffer(query_emb, dtype=np.float32)
        scored = []
        for e in entries:
            if e["embedding"]:
                ev = np.frombuffer(e["embedding"], dtype=np.float32)
                if ev.shape != query_vec.shape:
                    self._dbg(
                        f"跳过维度不一致知识 id={e['id']} query={query_vec.shape} entry={ev.shape}",
                        "verbose",
                    )
                    continue
                d = np.dot(query_vec, ev)
                n = np.linalg.norm(query_vec) * np.linalg.norm(ev)
                scored.append({"id": e["id"], "content": e["content"], "score": float(d / n) if n > 0 else 0.0})
            elif query.lower() in e["content"].lower():
                scored.append({"id": e["id"], "content": e["content"], "score": 0.3})
        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:top_k]
