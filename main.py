"""User-focused categorized memory plugin for AstrBot."""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections import deque
from collections.abc import AsyncGenerator
from datetime import datetime
from typing import Any

import numpy as np
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.event.filter import PermissionType, permission_type
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.provider.provider import EmbeddingProvider

from .engine.analyzer import ConversationAnalyzer
from .engine.sanitizer import ConversationSanitizer
from .storage.repository import CompanionRepository, MemoryItem
from .webui.server import WebUIServer


@register(
    "user_companion_memory",
    "tesla",
    "用户分类记忆插件——按类别记住用户信息、约定、事件、知识索引并每回合注入",
    "1.0.5",
    "https://github.com/tesla/astrbot_user_companion_memory",
)
class UserCompanionMemoryPlugin(Star):
    def __init__(self, context: Context, config: dict[str, Any]):
        super().__init__(context)
        self.context = context
        self._raw_config = config
        self.data_dir = str(StarTools.get_data_dir())
        os.makedirs(self.data_dir, exist_ok=True)
        self.config = self._load_runtime_config()
        self.repo = CompanionRepository(self.data_dir)
        self.analyzer = ConversationAnalyzer(context, self.config)
        self.webui: WebUIServer | None = None
        self._initialized = False
        self._started_at = time.time()
        self._turn_buffers: dict[str, deque[dict[str, str]]] = {}
        self._request_dedup: dict[tuple[str, str], float] = {}
        self._background_tasks: set[asyncio.Task] = set()
        self._last_forgetting_run_at: float = 0.0
        self._embedding_provider: EmbeddingProvider | None = None
        self._embedding_provider_initialized = False
        self._embedding_cache: dict[str, bytes] = {}

    def _dbg(self, msg: str, level: str = "verbose") -> None:
        levels = {"off": 0, "basic": 1, "verbose": 2, "trace": 3}
        cfg_level = self.config.get("debug_settings", {}).get("debug_level", "basic")
        if levels.get(cfg_level, 1) >= levels.get(level, 1):
            logger.info(f"[用户记忆] {msg}")

    def _load_runtime_config(self) -> dict[str, Any]:
        merged = dict(self._raw_config)
        runtime_path = os.path.join(self.data_dir, "runtime_config.json")
        if os.path.exists(runtime_path):
            try:
                with open(runtime_path, "r", encoding="utf-8") as f:
                    runtime_cfg = json.load(f)
                for section, values in runtime_cfg.items():
                    if section == "last_updated":
                        continue
                    if isinstance(values, dict) and isinstance(merged.get(section), dict):
                        merged[section].update(values)
                    else:
                        merged[section] = values
            except Exception as e:
                logger.warning(f"[用户记忆] 读取运行时配置失败: {e}")
        return merged

    def update_runtime_config(self, updates: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "memory_settings", "injection_settings", "forgetting_settings",
            "prompt_settings", "tool_settings", "debug_settings", "webui_settings",
        }
        runtime_path = os.path.join(self.data_dir, "runtime_config.json")
        existing: dict[str, Any] = {}
        if os.path.exists(runtime_path):
            try:
                with open(runtime_path, "r", encoding="utf-8") as f:
                    existing = json.load(f)
            except Exception:
                existing = {}
        for section, values in updates.items():
            if section not in allowed or not isinstance(values, dict):
                continue
            target = self.config.setdefault(section, {})
            target.update(values)
            existing.setdefault(section, {}).update(values)
        existing["last_updated"] = time.time()
        with open(runtime_path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
        os.replace(runtime_path + ".tmp", runtime_path)
        return self.config

    async def initialize(self) -> None:
        if self.config.get("webui_settings", {}).get("enabled", True):
            self.webui = WebUIServer(self, self.config)
            await self.webui.start()
        self._register_tools()
        self._initialized = True
        logger.info("[用户记忆] 就绪")

    async def terminate(self) -> None:
        for task in list(self._background_tasks):
            if not task.done():
                task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        if self.webui:
            await self.webui.stop()
        self.repo.close()

    @filter.on_llm_request(priority=0)
    async def on_llm_request(self, event: AstrMessageEvent, req: Any) -> None:
        if not self._initialized:
            return
        session_id = event.unified_msg_origin or "unknown"
        user_text = ConversationSanitizer.clean_text(event.message_str or "", self.config).strip()
        if not user_text:
            return
        self._capture_user_turn(session_id, user_text)
        await self._maybe_summarize(session_id)
        await self._run_forgetting_if_needed()
        built = await self._build_injection(user_text)
        if isinstance(built, tuple) and len(built) == 2:
            injection, used_ids = built
        elif isinstance(built, str):
            injection, used_ids = built, []
            self._dbg("注入构建返回了字符串，已自动兼容为无ID模式", "basic")
        else:
            self._dbg(f"注入构建返回异常类型 type={type(built).__name__} value={str(built)[:120]}", "basic")
            injection, used_ids = "", []
        if not injection:
            self._dbg(f"注入跳过 session={session_id} 无匹配记忆", "basic")
            return
        req.system_prompt = (getattr(req, "system_prompt", "") or "") + injection
        self.repo.touch_memories(used_ids)
        self._dbg(
            f"注入完成 session={session_id} ids={used_ids[:8]} chars={len(injection)}",
            "basic",
        )

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp: Any) -> None:
        if not self._initialized:
            return
        session_id = event.unified_msg_origin or "unknown"
        assistant_text = self._extract_response_text(resp)
        if assistant_text:
            self._attach_assistant_reply(session_id, assistant_text)
            self._dbg(f"补全助手回复 session={session_id} len={len(assistant_text)}", "trace")

    def _capture_user_turn(self, session_id: str, user_text: str) -> None:
        now = time.time()
        key = (session_id, user_text)
        for old_key, seen in list(self._request_dedup.items()):
            if now - seen > 600:
                del self._request_dedup[old_key]
        if now - self._request_dedup.get(key, 0.0) < 5:
            return
        self._request_dedup[key] = now

        state = self.repo.get_session_state(session_id)
        turn_count = int(state["turn_count"]) + 1
        self.repo.update_session_state(session_id, turn_count)
        max_turns = int(self.config.get("memory_settings", {}).get("max_buffer_turns", 30))
        buf = self._turn_buffers.setdefault(session_id, deque(maxlen=max_turns))
        buf.append({"user": user_text, "assistant": ""})
        self._dbg(f"记录用户回合 session={session_id} turn={turn_count} buf={len(buf)}", "basic")

    def _attach_assistant_reply(self, session_id: str, assistant_text: str) -> None:
        buf = self._turn_buffers.setdefault(session_id, deque(maxlen=int(self.config.get("memory_settings", {}).get("max_buffer_turns", 30))))
        if not buf:
            return
        buf[-1]["assistant"] = assistant_text

    async def _maybe_summarize(self, session_id: str) -> None:
        cfg = self.config.get("memory_settings", {})
        every_turns = int(cfg.get("summary_every_turns", 6))
        if every_turns <= 0:
            return
        state = self.repo.get_session_state(session_id)
        pending_turns = int(state["turn_count"]) - int(state["last_summary_turn"])
        if pending_turns < every_turns:
            self._dbg(
                f"总结未触发 session={session_id} pending={pending_turns}/{every_turns}",
                "trace",
            )
            return
        conversation = self._render_buffer(session_id)
        if not conversation.strip():
            return
        self._dbg(
            f"开始轮数总结 session={session_id} turns={pending_turns} chars={len(conversation)}",
            "basic",
        )
        provider_id = self.config.get("memory_settings", {}).get("summary_model", "")
        items = await self.analyzer.extract_memories(
            conversation,
            self.repo.list_memories(limit=120, include_expired=True),
            provider_id=provider_id,
        )
        created = 0
        updated = 0
        for item in items:
            status, item_id = await self._store_memory(
                category=item["category"],
                content=item["content"],
                priority=item["priority"],
                confidence=item["confidence"],
                pinned=bool(item["pinned"]),
                source="round_summary",
                note=item["note"],
                ttl_days=item["ttl_days"],
            )
            if status == "created":
                created += 1
            else:
                updated += 1
        self.repo.update_session_state(
            session_id,
            int(state["turn_count"]),
            last_summary_turn=int(state["turn_count"]),
        )
        self.repo.log_event("round_summary", f"session={session_id} created={created} updated={updated}")
        logger.info(
            f"[用户记忆] 轮数总结完成 session={session_id} created={created} updated={updated}"
        )

    async def _run_forgetting_if_needed(self) -> None:
        forgetting = self.config.get("forgetting_settings", {})
        interval_hours = float(forgetting.get("run_interval_hours", 12))
        if interval_hours <= 0:
            return
        now = time.time()
        if self._last_forgetting_run_at <= 0:
            self._last_forgetting_run_at = now
            return
        if now - self._last_forgetting_run_at >= interval_hours * 3600:
            counts = self.repo.run_forgetting(self.config)
            self._last_forgetting_run_at = now
            self._dbg(f"遗忘扫描 {counts}", "basic")

    def _pinned_limit(self) -> int:
        return int(self.config.get("memory_settings", {}).get("pinned_limit_per_category", 3))

    def _normalize_pinned_request(
        self,
        requested: bool,
        source: str,
        category: str = "",
        content: str = "",
        new_priority: float = 0.0,
        current_item_id: int | None = None,
    ) -> bool:
        if not requested:
            return False
        limit = self._pinned_limit()
        if limit <= 0:
            return requested
        current_pinned = self.repo.count_pinned(category=category or None)
        if current_item_id:
            current = self.repo.get_memory(current_item_id)
            if current and current.get("pinned"):
                return True
        if current_pinned >= limit:
            return self._rotate_pinned_if_needed(
                source=source,
                category=category,
                content=content,
                new_priority=new_priority,
                current_item_id=current_item_id,
            )
        return True

    def _rotate_pinned_if_needed(
        self,
        source: str,
        category: str,
        content: str,
        new_priority: float,
        current_item_id: int | None = None,
    ) -> bool:
        pinned_items = self.repo.list_pinned(limit=self._pinned_limit(), category=category or None)
        if not pinned_items:
            return False
        lowest = pinned_items[0]
        if current_item_id and int(lowest["id"]) == int(current_item_id):
            return True
        lowest_priority = float(lowest.get("priority", 0.0))
        if new_priority > lowest_priority:
            self.repo.update_memory(int(lowest["id"]), pinned=False)
            self._dbg(
                f"置顶轮换 source={source} new_p={new_priority} kick=#{lowest['id']} old_p={lowest_priority} "
                f"new_category={category} content={content[:60]}",
                "basic",
            )
            return True
        self._dbg(
            f"置顶保留 source={source} new_p={new_priority} <= old_p={lowest_priority} "
            f"category={category} content={content[:60]}",
            "basic",
        )
        return False

    async def _build_injection(self, user_text: str) -> tuple[str, list[int]]:
        cfg = self.config.get("injection_settings", {})
        header = str(cfg.get("reference_header", "[用户相关记忆参考]")).strip()
        max_chars = int(cfg.get("max_injection_chars", 1200))
        category_limits = {
            "agreement": int(cfg.get("agreement_limit", 3)),
            "profile": int(cfg.get("profile_limit", 4)),
            "event": int(cfg.get("event_limit", 2)),
            "fact": int(cfg.get("fact_limit", 3)),
            "knowledge_ref": int(cfg.get("knowledge_ref_limit", 2)),
        }
        lines = [header, "以下信息只作为与你当前用户相关的低冲突参考；相关时自然使用，不必刻意复述。"]
        used_ids: list[int] = []

        category_order = ("agreement", "profile", "event", "fact", "knowledge_ref")
        labels = {
            "agreement": "约定", "profile": "用户信息", "event": "近期事件",
            "fact": "相关事实", "knowledge_ref": "知识索引",
        }
        self._dbg(f"注入检索 query={user_text[:60]}", "basic")
        for category in category_order:
            limit = category_limits[category]
            items = await self._search_memories(user_text, [category], limit * 2)
            chosen = items[:limit]
            if not chosen:
                self._dbg(f"注入分类未命中 category={category}", "trace")
                continue
            self._dbg(
                f"注入分类命中 category={category} chosen="
                + ", ".join(
                    f"#{item['id']}|p={item.get('priority', 0)}|pin={'Y' if item.get('pinned') else 'N'}|{item['content'][:36]}"
                    for item in chosen
                ),
                "verbose",
            )
            lines.append(f"{labels[category]}:")
            for item in chosen:
                t = datetime.fromtimestamp(item.get("updated_at", 0)).strftime("%m-%d %H:%M") if item.get("updated_at") else ""
                note = f" ({item['note']})" if item.get("note") else ""
                lines.append(f"- [{t}] {item['content']}{note}")
                used_ids.append(int(item["id"]))
        text = "\n".join(lines).strip()
        raw_len = len(text)
        truncated = False
        if len(text) > max_chars:
            text = text[:max_chars].rsplit("\n", 1)[0]
            truncated = True
        if len(lines) <= 2:
            self._dbg("注入结果为空：没有任何分类通过最终选择", "basic")
            return ("", [])
        self._dbg(
            f"注入构建完成 ids={used_ids} raw_chars={raw_len} final_chars={len(text)} truncated={truncated}",
            "basic",
        )
        if self.config.get("debug_settings", {}).get("log_injection_detail", False):
            self._dbg(f"注入全文预览:\n{text[:500]}", "basic")
        return ("\n\n" + text + "\n", used_ids)

    async def _search_memories(
        self,
        query: str,
        categories: list[str] | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        embedding_enabled = bool(self.config.get("memory_settings", {}).get("embedding_enabled", True))
        if not embedding_enabled:
            self._dbg("向量检索已关闭，当前配置下不执行任何文本兜底搜索", "basic")
            return []
        await self._ensure_embeddings_current(categories)
        results = await self._search_memories_by_embedding(query, categories, limit)
        for r in results:
            r.pop("embedding", None)
        return results

    def _init_embedding_provider(self) -> None:
        if self._embedding_provider_initialized:
            return
        self._embedding_provider_initialized = True
        emb_id = self.config.get("memory_settings", {}).get("embedding_model", "")
        if emb_id:
            provider = self.context.get_provider_by_id(emb_id)
            if provider and isinstance(provider, EmbeddingProvider):
                self._embedding_provider = provider
                return
        try:
            providers = self.context.get_all_embedding_providers()
            if providers:
                self._embedding_provider = providers[0]
                return
        except Exception:
            pass
        logger.warning("[用户记忆] 无可用 Embedding Provider，向量检索将不可用")

    async def _get_embedding_bytes(self, text: str) -> bytes | None:
        key = text.strip()
        if not key:
            return None
        if key in self._embedding_cache:
            return self._embedding_cache[key]
        self._init_embedding_provider()
        if self._embedding_provider:
            try:
                vec = await self._embedding_provider.get_embedding(key)
                if vec:
                    emb = np.array(vec, dtype=np.float32).tobytes()
                    self._embedding_cache[key] = emb
                    if len(self._embedding_cache) > 512:
                        oldest = next(iter(self._embedding_cache))
                        del self._embedding_cache[oldest]
                    return emb
            except Exception as e:
                self._dbg(f"Embedding 调用失败: {e}", "basic")
        return None

    async def _refresh_memory_embedding(self, item_id: int, content: str) -> None:
        emb = await self._get_embedding_bytes(content)
        if emb is None:
            return
        model_name = self._get_embedding_model_name()
        self.repo.update_memory(item_id, embedding=emb, embedding_model=model_name)

    def _get_embedding_model_name(self) -> str:
        if self._embedding_provider:
            provider_config = getattr(self._embedding_provider, "provider_config", None)
            if isinstance(provider_config, dict):
                return str(provider_config.get("id", "embedding"))
            return str(getattr(provider_config, "id", "embedding"))
        return str(self.config.get("memory_settings", {}).get("embedding_model", "") or "")

    async def _ensure_embeddings_current(self, categories: list[str] | None) -> None:
        self._init_embedding_provider()
        model_name = self._get_embedding_model_name().strip()
        if not self._embedding_provider or not model_name:
            return
        active_items = self.repo.list_memories(limit=500, include_expired=False)
        refreshed = 0
        for item in active_items:
            if categories and item.get("category") not in categories:
                continue
            current_model = str(item.get("embedding_model", "") or "").strip()
            if current_model == model_name:
                continue
            await self._refresh_memory_embedding(int(item["id"]), str(item.get("content", "")))
            refreshed += 1
        if refreshed:
            self._dbg(
                f"已按当前向量模型重建记忆 embedding model={model_name} categories={categories or ['all']} refreshed={refreshed}",
                "basic",
            )

    async def _search_memories_by_embedding(
        self,
        query: str,
        categories: list[str] | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        query_emb = await self._get_embedding_bytes(query)
        if query_emb is None:
            return []
        query_model = self._get_embedding_model_name().strip()
        entries = self.repo.list_embeddings(categories, limit=500)
        if not entries:
            return []
        query_vec = np.frombuffer(query_emb, dtype=np.float32)
        scored: list[dict[str, Any]] = []
        for item in entries:
            emb = item.get("embedding")
            if not emb:
                continue
            item_model = str(item.get("embedding_model", "") or "").strip()
            if query_model and item_model and item_model != query_model:
                continue
            item_vec = np.frombuffer(emb, dtype=np.float32)
            if item_vec.shape != query_vec.shape:
                continue
            dot = float(np.dot(query_vec, item_vec))
            norm = float(np.linalg.norm(query_vec) * np.linalg.norm(item_vec))
            score = (dot / norm) if norm > 0 else 0.0
            normalized_query = " ".join(query.strip().lower().split())
            item_content = str(item.get("content", "")).strip().lower()
            if normalized_query and item_content == normalized_query:
                score += 0.35
            if item.get("pinned"):
                score += 0.12
            score += float(item.get("priority", 0.0)) * 0.15
            scored.append({**item, "score": round(score, 4)})
        scored.sort(key=lambda x: x["score"], reverse=True)
        threshold = float(self.config.get("memory_settings", {}).get("embedding_threshold", 0.35))
        filtered = [item for item in scored if item["score"] >= threshold]
        self._dbg(
            f"embedding检索 query={query[:40]} categories={categories or ['all']} hits={len(filtered)}/{len(scored)} threshold={threshold}",
            "verbose",
        )
        return filtered[:limit]

    async def _store_memory(
        self,
        *,
        category: str,
        content: str,
        priority: float,
        confidence: float,
        pinned: bool,
        source: str,
        note: str,
        ttl_days: int,
    ) -> tuple[str, int]:
        existing = self.repo.list_memories(category=category, limit=500, include_expired=True)
        existing_item = next((item for item in existing if item["content"].strip() == " ".join(content.strip().split())), None)
        current_item_id = int(existing_item["id"]) if existing_item else None
        effective_priority = max(float(existing_item.get("priority", 0.0)), priority) if existing_item else priority
        effective_pinned = self._normalize_pinned_request(
            pinned,
            source=source,
            category=category,
            content=content,
            current_item_id=current_item_id,
        )
        status, item_id = self.repo.merge_or_add(
            category=category,
            content=content,
            priority=effective_priority,
            confidence=confidence,
            pinned=effective_pinned,
            source=source,
            note=note,
            ttl_days=ttl_days,
        )
        await self._refresh_memory_embedding(item_id, content)
        return status, item_id

    def _render_buffer(self, session_id: str) -> str:
        buf = self._turn_buffers.get(session_id)
        if not buf:
            return ""
        parts = []
        for turn in list(buf):
            parts.append(f"用户: {turn.get('user', '')}")
            if turn.get("assistant"):
                parts.append(f"助手: {turn.get('assistant', '')}")
        return "\n".join(parts)

    def _register_tools(self) -> None:
        class AddUserMemoryTool:
            name = "add_user_memory"
            description = "主动将与当前用户直接相关的短记忆写入分类记忆库。适合记录偏好、约定、近期事件、事实和知识存放位置。"
            parameters = {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "enum": ["profile", "agreement", "event", "fact", "knowledge_ref"]},
                    "content": {"type": "string"},
                    "priority": {"type": "number"},
                    "pinned": {"type": "boolean"},
                    "ttl_days": {"type": "integer"},
                    "note": {"type": "string"},
                },
                "required": ["category", "content"],
            }

            def __init__(self, plugin):
                self.plugin = plugin
                self.active = True
                self.handler = self.run
                self.is_background_task = False

            async def run(self, event=None, **kw) -> str:
                if not self.plugin.config.get("tool_settings", {}).get("allow_agent_add", True):
                    return "agent主动写入已关闭"
                category = str(kw.get("category", "")).strip()
                content = " ".join(str(kw.get("content", "")).split())
                if not category or not content:
                    return "缺少 category 或 content"
                status, item_id = await self.plugin._store_memory(
                    category=category,
                    content=content[:240],
                    priority=float(kw.get("priority", self.plugin.config.get("tool_settings", {}).get("default_tool_priority", 0.75))),
                    confidence=0.95,
                    pinned=bool(kw.get("pinned", False)),
                    source="agent_tool",
                    note=str(kw.get("note", "")).strip()[:240],
                    ttl_days=int(kw.get("ttl_days", 0)),
                )
                logger.info(f"[用户记忆] agent_tool {status} {category}#{item_id} {content}")
                return f"{status}: {category}#{item_id}"

        class SearchUserMemoryTool:
            name = "search_user_memory"
            description = "搜索当前用户的分类记忆。"
            parameters = {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            }

            def __init__(self, plugin):
                self.plugin = plugin
                self.active = True
                self.handler = self.run
                self.is_background_task = False

            async def run(self, event=None, **kw) -> str:
                query = str(kw.get("query", "")).strip()
                if not query:
                    return "缺少 query"
                results = await self.plugin._search_memories(query, None, 8)
                if not results:
                    return "无相关记忆"
                return "\n".join(
                    f"- [{item['category']}] {item['content']} (score={item['score']})"
                    for item in results
                )

        self.context.add_llm_tools(AddUserMemoryTool(self))
        self.context.add_llm_tools(SearchUserMemoryTool(self))

    @filter.command_group("ucm")
    def ucm(self):
        pass

    @permission_type(PermissionType.ADMIN)
    @ucm.command("status", priority=10)
    async def cmd_status(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        stats = self.repo.get_dashboard_stats()
        yield event.plain_result(
            "\n".join(
                [
                    "用户分类记忆插件 v1.0.3",
                    f"运行分钟: {int((time.time() - self._started_at) / 60)}",
                    f"总条目: {stats['total']} | active={stats['active']} stale={stats['stale']} archived={stats['archived']}",
                    "分类: " + ", ".join(f"{k}={v}" for k, v in stats["by_category"].items()) if stats["by_category"] else "分类: 无",
                ]
            )
        )

    @permission_type(PermissionType.ADMIN)
    @ucm.command("add", priority=10)
    async def cmd_add(self, event: AstrMessageEvent, category: str, content: str) -> AsyncGenerator[MessageEventResult, None]:
        status, item_id = await self._store_memory(
            category=category,
            content=content,
            priority=0.7,
            confidence=0.9,
            pinned=False,
            source="manual_command",
            note="",
            ttl_days=0,
        )
        yield event.plain_result(f"{status}: {category}#{item_id}")

    @permission_type(PermissionType.ADMIN)
    @ucm.command("list", priority=10)
    async def cmd_list(self, event: AstrMessageEvent, category: str = "") -> AsyncGenerator[MessageEventResult, None]:
        items = self.repo.list_memories(category=category, limit=20, include_expired=True)
        if not items:
            yield event.plain_result("无记忆")
            return
        yield event.plain_result(
            "\n".join(f"#{item['id']} [{item['category']}] {item['content']} ({item['status']})" for item in items)
        )

    @permission_type(PermissionType.ADMIN)
    @ucm.command("forget", priority=10)
    async def cmd_forget(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        counts = self.repo.run_forgetting(self.config)
        yield event.plain_result(f"遗忘扫描完成: {counts}")

    @staticmethod
    def _extract_response_text(resp: Any) -> str:
        for attr in ("completion_text", "result", "text", "message"):
            value = getattr(resp, attr, None)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return resp.strip() if isinstance(resp, str) else ""
