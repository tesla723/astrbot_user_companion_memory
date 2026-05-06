"""学习进化器：用户画像 + Agent 自画像 合并分析。

画像通过 system prompt 注入叠加层，不修改 AstrBot 原生 Persona。
WebUI 可查看和手动编辑。
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from astrbot.api import logger

from ..engine.analyzer import ConversationAnalyzer
from ..engine.decider import EvolutionDecider
from ..engine.validator import EvolutionValidator
from ..storage.repository import EvolutionRepository
from .base import BaseEvolver


class LearningEvolver(BaseEvolver):

    def __init__(
        self, context, config: dict, data_dir: str,
        repository: EvolutionRepository, analyzer: ConversationAnalyzer,
        decider: EvolutionDecider, validator: EvolutionValidator,
    ):
        super().__init__(context, config, data_dir)
        self.repo = repository
        self.analyzer = analyzer
        self.decider = decider
        self.validator = validator
        self._user_profile: dict | None = None
        self._agent_profile: dict | None = None
        self._conversation_buffer: list[str] = []

    def get_config(self) -> dict:
        return self.config.get("learning_evolver", {})

    def is_enabled(self) -> bool:
        return self.get_config().get("enabled", True)

    def _dbg(self, msg: str, level: str = "verbose") -> None:
        lvls = {"off": 0, "basic": 1, "verbose": 2, "trace": 3}
        cfg_lvl = self.config.get("debug_settings", {}).get("debug_level", "basic")
        if lvls.get(cfg_lvl, 1) >= lvls.get(level, 1):
            logger.info(f"[进化引擎-学习] {msg}")

    # ══════ 画像存取 ══════

    def get_profile(self) -> dict:
        if self._user_profile is None:
            self._user_profile = self.load_profile()
        return self._user_profile

    def get_agent_profile(self) -> dict:
        if self._agent_profile is None:
            self._agent_profile = self._load_agent_profile()
        return self._agent_profile

    def save_profile(self, profile: dict) -> None:
        profile["last_updated"] = time.time()
        path = os.path.join(self.data_dir, "user_profile.json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(profile, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)  # 原子写入
        self._dbg(f"用户画像已保存 size={os.path.getsize(path)}B", "trace")

    def save_agent_profile(self, profile: dict) -> None:
        profile["last_updated"] = time.time()
        path = os.path.join(self.data_dir, "agent_profile.json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(profile, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)  # 原子写入
        self._dbg(f"Agent画像已保存 size={os.path.getsize(path)}B", "trace")

    def _load_agent_profile(self) -> dict:
        path = os.path.join(self.data_dir, "agent_profile.json")
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._dbg(f"Agent画像已加载 keys={list(data.keys())}", "trace")
                return data
            except Exception as e:
                logger.warning(f"[进化引擎-学习] Agent画像读取失败: {e}，使用默认值")
        return {
            "persona_style": "未知",
            "knowledge_domains": [],
            "response_preferences": "未知",
            "conversation_habits": "未知",
            "last_updated": 0,
        }

    # ══════ 入口 ══════

    async def accumulate_and_learn(self, message: str) -> None:
        if not self.is_enabled():
            self._dbg("跳过学习：learning_evolver.disabled", "basic")
            return
        self._conversation_buffer.append(message)
        trigger_mode = self.get_config().get("trigger_mode", "both")
        if trigger_mode not in ("rounds", "both"):
            self._dbg(
                f"仅缓冲不轮数触发 trigger_mode={trigger_mode} buf={len(self._conversation_buffer)}",
                "verbose",
            )
            return
        buf_size = self.get_config().get("learn_buffer_size", 10)
        self._dbg(f"缓冲累积 {len(self._conversation_buffer)}/{buf_size}", "trace")
        if len(self._conversation_buffer) >= buf_size:
            self._dbg(f"达到轮数阈值，开始画像分析 buf={len(self._conversation_buffer)}", "basic")
            await self._learn_from_buffer()

    async def cron_learn(self) -> None:
        trigger_mode = self.get_config().get("trigger_mode", "both")
        if trigger_mode not in ("time", "both"):
            self._dbg(f"跳过定时学习：trigger_mode={trigger_mode}", "trace")
            return
        if self._conversation_buffer:
            self._dbg(f"定时学习触发 buf={len(self._conversation_buffer)}", "basic")
            await self._learn_from_buffer()
        else:
            self._dbg("定时学习检查：缓冲为空", "trace")

    async def _learn_from_buffer(self) -> None:
        if not self._conversation_buffer:
            return
        conversation = "\n".join(self._conversation_buffer)
        self._conversation_buffer = []
        started = time.monotonic()
        event_id = self.decider.log_event(
            "learning",
            "analyze_profiles",
            trigger_source="conversation",
            analysis_summary=conversation[:200],
        )
        self._dbg(f"学习事件已创建 event_id={event_id} buf_chars={len(conversation)}", "basic")

        user_profile = self.get_profile()
        agent_profile = self.get_agent_profile()

        self._dbg(
            f"合并分析开始 conv_len={len(conversation)} "
            f"up_keys={list(user_profile.keys())} ap_keys={list(agent_profile.keys())}",
            "basic",
        )

        provider_id = self.get_config().get("analysis_model", "")
        result = await self.analyzer.analyze_both_profiles(
            user_profile, agent_profile, conversation, provider_id,
        )

        # ── 严格校验 LLM 返回值类型 ──
        if not isinstance(result, dict):
            logger.warning(
                f"[进化引擎-学习] LLM 返回非 dict: type={type(result).__name__} "
                f"raw={str(result)[:200]}"
            )
            self.repo.update_event(
                event_id,
                status="failed",
                error_message=f"LLM 返回非 dict: {type(result).__name__}",
                completed_at=time.time(),
            )
            self._dbg(f"学习失败 event_id={event_id} 原因=non_dict_result", "basic")
            return

        user_delta = result.get("user", {})
        agent_delta = result.get("agent", {})

        self._dbg(
            f"LLM返回 user_delta_type={type(user_delta).__name__} "
            f"agent_delta_type={type(agent_delta).__name__}",
            "verbose",
        )

        if not isinstance(user_delta, dict) or not isinstance(agent_delta, dict):
            logger.warning(
                f"[进化引擎-学习] delta 类型错误 "
                f"user={type(user_delta).__name__} agent={type(agent_delta).__name__}"
            )
            self.repo.update_event(
                event_id,
                status="failed",
                error_message=(
                    f"delta 类型错误 user={type(user_delta).__name__} "
                    f"agent={type(agent_delta).__name__}"
                ),
                completed_at=time.time(),
            )
            self._dbg(
                f"学习失败 event_id={event_id} 原因=delta_type_error "
                f"user={type(user_delta).__name__} agent={type(agent_delta).__name__}",
                "basic",
            )
            return

        self._dbg(
            f"合并分析结果 user_delta={list(user_delta.keys()) or '无'} "
            f"agent_delta={list(agent_delta.keys()) or '无'}",
            "verbose",
        )

        updated_parts: list[str] = []

        # ── 用户画像更新 ──
        if user_delta and self.decider.should_update_profile(user_delta):
            updated = self._safe_merge(self._user_profile or {}, user_delta)
            if updated:
                self._user_profile = updated
                self.save_profile(self._user_profile)
                updated_parts.append(f"user={list(user_delta.keys())}")
                self._dbg(
                    f"用户画像已更新 keys={list(user_delta.keys())} "
                    f"values={_fmt_delta(user_delta)}",
                    "basic",
                )
        elif user_delta:
            updated_parts.append(f"user_skipped={list(user_delta.keys())}")
            self._dbg(
                f"用户画像有增量但跳过更新 keys={list(user_delta.keys())} "
                f"原因=触发限流或策略拒绝",
                "basic",
            )
        else:
            self._dbg("用户画像无变化", "verbose")

        # ── Agent 画像更新 ──
        if agent_delta:
            updated = self._safe_merge(self._agent_profile or {}, agent_delta)
            if updated:
                self._agent_profile = updated
                self.save_agent_profile(self._agent_profile)
                updated_parts.append(f"agent={list(agent_delta.keys())}")
                self._dbg(
                    f"Agent画像已更新 keys={list(agent_delta.keys())} "
                    f"values={_fmt_delta(agent_delta)}",
                    "basic",
                )
        else:
            self._dbg("Agent画像无变化", "verbose")

        detail = ", ".join(updated_parts) if updated_parts else "nochange"
        self.repo.update_event(
            event_id,
            status="completed",
            decision_detail=detail,
            completed_at=time.time(),
        )
        self._dbg(f"学习事件已完成 event_id={event_id} detail={detail}", "basic")
        if user_delta:
            await self.validator.validate_profile_updated(event_id, user_delta)

        ms = (time.monotonic() - started) * 1000
        logger.info(
            f"[进化引擎-学习] 完成 {detail} | {ms:.0f}ms"
        )

    def _safe_merge(self, current: dict, delta: dict) -> dict | None:
        """安全合并增量到画像。返回值类型校验，只允许合法 dict/list/str。"""
        if not isinstance(delta, dict) or not delta:
            return None
        merged = dict(current)
        list_keys = {"tech_stack", "topic_interests", "knowledge_domains"}

        for key, value in delta.items():
            if key in list_keys:
                if isinstance(value, list):
                    merged[key] = list(set(merged.get(key, [])) | set(value))
                elif isinstance(value, str):
                    # LLM 可能返回逗号分隔的字符串
                    merged[key] = list(set(merged.get(key, [])) | {x.strip() for x in value.split(",") if x.strip()})
                else:
                    self._dbg(f"忽略非法list字段 {key}={type(value).__name__}", "basic")
            elif isinstance(value, str):
                if value.strip() and value.strip() != "未知":
                    merged[key] = value.strip()
            else:
                self._dbg(f"忽略非法字段 {key}={type(value).__name__}", "basic")

        merged["last_updated"] = time.time()
        return merged

    # ══════ 注入 ══════

    def build_profile_injection(self) -> str:
        parts = []
        up = self.get_profile()
        ap = self.get_agent_profile()

        user_parts = []
        if up.get("language_style") and up["language_style"] != "未知":
            user_parts.append(f"语言风格: {up['language_style']}")
        if up.get("tech_stack"):
            user_parts.append(f"技术栈: {', '.join(up['tech_stack'][:6])}")
        if up.get("topic_interests"):
            user_parts.append(f"关注领域: {', '.join(up['topic_interests'][:6])}")
        if up.get("memory_preferences") and up["memory_preferences"] != "未知":
            user_parts.append(f"记忆偏好: {up['memory_preferences']}")
        if up.get("relationship_preferences") and up["relationship_preferences"] != "未知":
            user_parts.append(f"相处偏好: {up['relationship_preferences']}")
        if user_parts:
            parts.append("用户画像: " + " | ".join(user_parts))

        agent_parts = []
        if ap.get("persona_style") and ap["persona_style"] != "未知":
            agent_parts.append(f"风格: {ap['persona_style']}")
        if ap.get("knowledge_domains"):
            agent_parts.append(f"知识领域: {', '.join(ap['knowledge_domains'][:6])}")
        if ap.get("response_preferences") and ap["response_preferences"] != "未知":
            agent_parts.append(f"回复偏好: {ap['response_preferences']}")
        if ap.get("conversation_habits") and ap["conversation_habits"] != "未知":
            agent_parts.append(f"对话习惯: {ap['conversation_habits']}")
        if agent_parts:
            parts.append("Agent画像: " + " | ".join(agent_parts))

        return "\n".join(parts) if parts else ""


def _fmt_delta(delta: dict) -> str:
    """格式化 delta 用于 debug 日志显示。"""
    items = []
    for k, v in delta.items():
        if isinstance(v, list):
            items.append(f"{k}={v[:3]}")
        else:
            items.append(f"{k}={str(v)[:40]}")
    return " | ".join(items)
