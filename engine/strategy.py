"""Conversation strategy manager.

This is the human-facing layer of the evolution engine. It keeps the goal plain:
help the agent sound natural, remember durable things, and adapt its response
strategy over time without exposing internal machinery to the user.
"""

from __future__ import annotations

import json
import os
import time


DEFAULT_STATE = {
    "style": "natural_companion",
    "verbosity": "compact",
    "memory_weight": 0.65,
    "skill_proposal_strictness": "normal",
    "last_reason": "initial defaults",
    "updated_at": 0,
}


class ConversationStrategyManager:
    def __init__(self, config: dict, data_dir: str):
        self.config = config
        self.data_dir = data_dir
        self._path = os.path.join(data_dir, "conversation_strategy.json")
        self._state: dict | None = None

    def get_state(self) -> dict:
        if self._state is None:
            self._state = self._load()
        return self._state

    def build_guidance(self) -> str:
        cfg = self.config.get("companion_settings", {})
        if not cfg.get("enabled", True):
            return ""

        state = self.get_state()
        base = str(cfg.get("base_behavior_prompt", "")).strip()
        if not base:
            base = (
                "像熟悉的真人伙伴一样说话：先接住用户当下的意思，再给有用回应。"
                "少用客服腔、报告腔和模板化总结；不要为了显得主动而追问太多。"
                "记住用户长期偏好、项目、关系和明确纠正，但不要主动暴露“我记得”。"
            )

        lines = [
            "[交流策略]",
            base,
            f"当前表达长度倾向: {state.get('verbosity', 'compact')}",
            f"记忆参考权重: {state.get('memory_weight', 0.65)}（相关才用，不相关就忽略）",
            "如果当前用户只是表达情绪或吐槽，优先自然回应，不要立刻变成任务清单。",
        ]
        return "\n".join(lines)

    def optimize_from_stats(self, stats: dict) -> dict:
        cfg = self.config.get("companion_settings", {})
        if not cfg.get("strategy_optimization_enabled", True):
            return {}

        state = dict(self.get_state())
        before = dict(state)
        reasons = []

        capability = stats.get("capability", {})
        reject_threshold = float(cfg.get("high_rejection_rate_threshold", 0.5))
        if capability.get("rejection_rate", 0) > reject_threshold:
            state["skill_proposal_strictness"] = "high"
            reasons.append("skill proposals rejected often")

        knowledge_count = stats.get("knowledge_count", 0)
        knowledge = stats.get("knowledge", {})
        if knowledge_count > 0 and knowledge.get("success_rate", 1.0) >= 0.8:
            state["memory_weight"] = min(
                float(cfg.get("max_memory_weight", 0.8)),
                float(state.get("memory_weight", 0.65)) + float(cfg.get("memory_weight_step", 0.05)),
            )
            reasons.append("memory store appears useful")
        elif knowledge.get("failed", 0) > knowledge.get("completed", 0):
            state["memory_weight"] = max(
                float(cfg.get("min_memory_weight", 0.35)),
                float(state.get("memory_weight", 0.65)) - float(cfg.get("memory_failure_penalty", 0.1)),
            )
            reasons.append("memory extraction has too many failures")

        learning = stats.get("learning", {})
        if learning.get("completed", 0) == 0 and knowledge_count > 10:
            state["verbosity"] = "compact"
            reasons.append("prefer compact defaults until profile stabilizes")

        if state == before:
            return {}

        state["last_reason"] = "; ".join(reasons) or "stats changed"
        state["updated_at"] = time.time()
        self._state = state
        self._save(state)
        return {
            "conversation_strategy": {
                "from": before,
                "to": state,
                "reason": state["last_reason"],
            }
        }

    def _load(self) -> dict:
        defaults = self._configured_defaults()
        if os.path.exists(self._path):
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    merged = dict(defaults)
                    merged.update(data)
                    return merged
            except Exception:
                pass
        return defaults

    def _configured_defaults(self) -> dict:
        cfg = self.config.get("companion_settings", {})
        defaults = dict(DEFAULT_STATE)
        defaults["style"] = cfg.get("default_style", defaults["style"])
        defaults["verbosity"] = cfg.get("default_verbosity", defaults["verbosity"])
        defaults["memory_weight"] = cfg.get("default_memory_weight", defaults["memory_weight"])
        defaults["skill_proposal_strictness"] = cfg.get(
            "default_skill_proposal_strictness",
            defaults["skill_proposal_strictness"],
        )
        return defaults

    def _save(self, state: dict) -> None:
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self._path)
