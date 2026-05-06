"""决策器模块：基于分析结果 + 阈值/策略 → 决定是否触发进化操作。

所有决策都有日志记录，支持回滚审计。
"""

from __future__ import annotations

import json
import time
from typing import Any

from astrbot.api import logger

from ..storage.repository import EvolutionArtifact, EvolutionEvent, EvolutionRepository


class EvolutionDecider:
    """根据分析结果 + 配置策略做进化决策。"""

    def __init__(self, config: dict, repository: EvolutionRepository):
        self.config = config
        self.repo = repository

    def _dbg(self, msg: str, level: str = "verbose") -> None:
        lvls = {"off": 0, "basic": 1, "verbose": 2, "trace": 3}
        cfg_lvl = self.config.get("debug_settings", {}).get("debug_level", "basic")
        if lvls.get(cfg_lvl, 1) >= lvls.get(level, 1):
            logger.debug(f"[进化引擎-决策器] {msg}")

    def _log_decisions_enabled(self) -> bool:
        return self.config.get("debug_settings", {}).get("log_decisions", True)

    # ── 知识进化决策 ──

    def should_store_knowledge(self, items: list[dict]) -> list[dict]:
        k_cfg = self.config.get("knowledge_evolver", {})
        if not k_cfg.get("enabled", True):
            self._dbg("知识存储：已禁用", "basic")
            return []

        min_conf = k_cfg.get("min_confidence", 0.7)
        max_per_day = k_cfg.get("max_per_day", 20)
        today_count = self.repo.count_knowledge_created_today()
        remaining = max_per_day - today_count

        if self._log_decisions_enabled():
            self._dbg(
                f"知识存储决策: 候选={len(items)} min_conf={min_conf} "
                f"今日已存={today_count}/{max_per_day} 剩余={remaining}",
                "basic",
            )

        if remaining <= 0:
            self._dbg("知识存储：今日已达上限，全部丢弃", "basic")
            return []

        accepted = [it for it in items if it.get("confidence", 0) >= min_conf]
        dropped = len(items) - len(accepted)
        if dropped > 0 and self._log_decisions_enabled():
            self._dbg(f"低置信度过滤: 丢弃 {dropped} 条 (阈值={min_conf})", "basic")

        if len(accepted) > remaining:
            accepted.sort(key=lambda x: x.get("confidence", 0), reverse=True)
            overflow = len(accepted) - remaining
            if self._log_decisions_enabled():
                self._dbg(f"超限截断: 丢弃末尾 {overflow} 条", "basic")
            accepted = accepted[:remaining]

        return accepted

    # ── 能力进化决策 ──

    def should_propose_skill(
        self, patterns: list[dict], pending_patterns: set[str]
    ) -> list[dict]:
        c_cfg = self.config.get("capability_evolver", {})
        if not c_cfg.get("enabled", True):
            self._dbg("Skill提案：已禁用", "basic")
            return []

        min_occurrences = c_cfg.get("min_pattern_occurrences", 5)
        max_per_week = c_cfg.get("max_skills_per_week", 2)
        this_week = self.repo.count_events_this_week(evolver_type="capability")

        if self._log_decisions_enabled():
            self._dbg(
                f"Skill提案决策: 候选={len(patterns)} 本周已提案={this_week}/{max_per_week} "
                f"冷却/审批中={len(pending_patterns)}",
                "basic",
            )

        if this_week >= max_per_week:
            self._dbg("Skill提案：本周已达上限", "basic")
            return []

        proposals = []
        skipped_pending = 0
        skipped_freq = 0
        for p in patterns:
            name = p.get("pattern_name", "")
            if name in pending_patterns:
                skipped_pending += 1
                continue
            freq = p.get("frequency", "")
            if freq in ("high", "medium"):
                proposals.append(p)
            else:
                skipped_freq += 1

        if self._log_decisions_enabled():
            self._dbg(
                f"Skill提案过滤: 跳过冷却={skipped_pending} 跳过低频率={skipped_freq} "
                f"剩余={len(proposals)}",
                "verbose",
            )

        return proposals[: max_per_week - this_week]

    # ── 学习进化决策 ──

    def should_update_profile(self, profile_delta: dict) -> bool:
        l_cfg = self.config.get("learning_evolver", {})
        if not l_cfg.get("enabled", True):
            self._dbg("画像更新：已禁用", "basic")
            return False
        if not profile_delta or len(profile_delta) == 0:
            return False

        max_updates = l_cfg.get("max_profile_updates_per_day", 3)
        today = self.repo.count_events_today(evolver_type="learning")

        if self._log_decisions_enabled():
            self._dbg(
                f"画像更新决策: 变化维度={list(profile_delta.keys())} "
                f"今日已更新={today}/{max_updates}",
                "verbose",
            )

        if today >= max_updates:
            self._dbg("画像更新：今日已达上限", "basic")
            return False

        return True

    # ── 元进化决策 ──

    def should_adjust_thresholds(self, stats: dict) -> dict:
        m_cfg = self.config.get("meta_evolver", {})
        if not m_cfg.get("enabled", True) or not m_cfg.get("auto_adjust_thresholds", True):
            return {}

        self._dbg(f"元进化评估开始: 统计各进化器表现", "basic")
        adjustments = {}

        k_stats = stats.get("knowledge", {})
        if k_stats.get("deletion_rate", 0) > 0.3:
            old = self.config.get("knowledge_evolver", {}).get("min_confidence", 0.7)
            new = min(0.9, old + 0.05)
            adjustments["knowledge_evolver.min_confidence"] = {
                "from": old, "to": new,
                "reason": f"知识删除率 {k_stats['deletion_rate']:.1%} > 30%，提高置信度阈值",
            }
            self._dbg(f"阈值调整: knowledge.min_confidence {old}→{new}", "basic")

        c_stats = stats.get("capability", {})
        if c_stats.get("rejection_rate", 0) > 0.7:
            old = self.config.get("capability_evolver", {}).get("min_pattern_occurrences", 5)
            new = old + 2
            adjustments["capability_evolver.min_pattern_occurrences"] = {
                "from": old, "to": new,
                "reason": f"Skill提案拒绝率 {c_stats['rejection_rate']:.1%} > 70%，提高触发阈值",
            }
            self._dbg(f"阈值调整: capability.min_pattern_occurrences {old}→{new}", "basic")

        if self._log_decisions_enabled() and not adjustments:
            self._dbg("元进化评估：无需调整", "basic")

        return adjustments

    # ── 日志记录 ──

    def log_event(
        self,
        evolver_type: str,
        action: str,
        trigger_source: str = "",
        analysis_summary: str = "",
        decision_detail: str = "",
    ) -> int:
        event = EvolutionEvent(
            evolver_type=evolver_type,
            action=action,
            status="pending",
            trigger_source=trigger_source,
            analysis_summary=analysis_summary,
            decision_detail=decision_detail,
            created_at=time.time(),
        )
        event_id = self.repo.create_event(event)
        self._dbg(f"事件创建 id={event_id} type={evolver_type} action={action}", "trace")
        return event_id

    def log_artifact(
        self,
        event_id: int,
        artifact_type: str,
        content: str,
        status: str = "active",
        meta: dict | None = None,
    ) -> int:
        artifact = EvolutionArtifact(
            event_id=event_id,
            artifact_type=artifact_type,
            content=content,
            status=status,
            metadata_json=json.dumps(meta or {}, ensure_ascii=False),
            created_at=time.time(),
        )
        artifact_id = self.repo.create_artifact(artifact)
        self._dbg(f"产物创建 id={artifact_id} type={artifact_type} status={status}", "trace")
        return artifact_id
