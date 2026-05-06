"""Context injection planner.

This layer turns stored knowledge/profile data into a small, bounded advisory
block. It deliberately avoids dumping raw memory into every request.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class InjectionPlan:
    text: str = ""
    knowledge_count: int = 0
    profile_injected: bool = False
    scores: list[float] = field(default_factory=list)


class ContextInjectionPlanner:
    def __init__(self, config: dict, knowledge_evolver, learning_evolver, strategy_manager=None):
        self.config = config
        self.knowledge_evolver = knowledge_evolver
        self.learning_evolver = learning_evolver
        self.strategy_manager = strategy_manager

    async def build(self, user_message: str) -> InjectionPlan:
        cfg = self.config.get("injection_settings", {})
        plan = InjectionPlan()
        parts: list[str] = []

        if self.strategy_manager:
            strategy_text = self.strategy_manager.build_guidance()
            if strategy_text:
                parts.append(self._trim(strategy_text, cfg.get("strategy_max_chars", 700)))

        if cfg.get("knowledge_injection_enabled", True):
            knowledge_text = await self._build_knowledge_block(user_message, plan)
            if knowledge_text:
                parts.append(knowledge_text)

        if cfg.get("profile_injection_enabled", True):
            profile_text = self.learning_evolver.build_profile_injection()
            if profile_text:
                plan.profile_injected = True
                parts.append("[长期画像]\n" + self._trim(profile_text, cfg.get("profile_max_chars", 400)))

        if not parts:
            return plan

        header = cfg.get(
            "reference_header",
            "[进化引擎参考]\n"
            "以下内容帮助你更像一个长期相处的人来回应；若与当前用户消息无关或冲突，请忽略。",
        )
        text = header + "\n\n" + "\n\n".join(parts)
        plan.text = "\n\n" + self._trim(text, cfg.get("max_injection_chars", 1200))
        return plan

    async def _build_knowledge_block(self, user_message: str, plan: InjectionPlan) -> str:
        cfg = self.config.get("injection_settings", {})
        top_k = cfg.get("knowledge_top_k", 3)
        threshold = cfg.get("knowledge_threshold", 0.6)
        results = await self.knowledge_evolver.search_knowledge(user_message, top_k=top_k)
        filtered = [r for r in results if r.get("score", 0.0) >= threshold]
        plan.knowledge_count = len(filtered)
        plan.scores = [float(r.get("score", 0.0)) for r in results[:5]]
        if not filtered:
            return ""

        max_each = cfg.get("knowledge_max_chars_each", 180)
        lines = []
        for item in filtered:
            content = self._trim(str(item.get("content", "")), max_each)
            if content:
                lines.append(f"- {content}")
        if not lines:
            return ""
        return "[相关知识]\n" + "\n".join(lines)

    @staticmethod
    def _trim(text: str, max_chars: int) -> str:
        text = text.strip()
        if max_chars <= 0 or len(text) <= max_chars:
            return text
        return text[: max_chars - 1].rstrip() + "…"
