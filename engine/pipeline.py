"""Conversation pipeline for evolution modules.

The plugin should observe complete turns first, then fan out normalized context
to individual evolvers. This keeps knowledge/capability/learning from each
building a different, lossy view of the same conversation.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .sanitizer import ConversationSanitizer


@dataclass
class ConversationTurn:
    session_id: str
    user_text: str
    assistant_text: str = ""
    created_at: float = field(default_factory=time.time)


class EvolutionPipeline:
    """Normalize one chat turn and dispatch it to evolvers."""

    def __init__(self, config: dict, knowledge_evolver, learning_evolver, capability_evolver):
        self.config = config
        self.knowledge_evolver = knowledge_evolver
        self.learning_evolver = learning_evolver
        self.capability_evolver = capability_evolver
        self._sessions: dict[str, list[ConversationTurn]] = {}

    def _dbg(self, msg: str, level: str = "verbose") -> None:
        from astrbot.api import logger

        lvls = {"off": 0, "basic": 1, "verbose": 2, "trace": 3}
        cfg_lvl = self.config.get("debug_settings", {}).get("debug_level", "basic")
        if lvls.get(cfg_lvl, 1) >= lvls.get(level, 1):
            logger.info(f"[进化引擎-管线] {msg}")

    async def process_turn(
        self,
        session_id: str,
        user_text: str,
        assistant_text: str = "",
    ) -> None:
        session_id = session_id or "unknown"
        user_text, assistant_text = ConversationSanitizer.clean_turn(
            user_text or "",
            assistant_text or "",
            self.config,
        )
        self._dbg(
            f"处理回合 session={session_id} user_len={len(user_text)} assistant_len={len(assistant_text)}",
            "basic",
        )
        if not user_text:
            self._dbg(f"跳过空用户消息 session={session_id}", "basic")
            return

        turn = ConversationTurn(
            session_id=session_id,
            user_text=user_text,
            assistant_text=assistant_text,
        )
        self._remember(turn)
        self._dbg(
            f"会话已缓存 session={session_id} turns={len(self._sessions.get(session_id, []))}",
            "verbose",
        )

        # Knowledge and capability need task/evidence context, not just isolated
        # user utterances. Learning still receives user-only text to avoid
        # mistaking assistant wording for user preferences.
        formatted_turn = self._format_turn(turn)
        await self.knowledge_evolver.on_conversation_turn(session_id, formatted_turn)
        await self.learning_evolver.accumulate_and_learn(user_text)
        await self.capability_evolver.accumulate_conversation(formatted_turn)
        self._dbg(f"分发完成 session={session_id}", "trace")

    def _remember(self, turn: ConversationTurn) -> None:
        self._prune_old()
        turns = self._sessions.setdefault(turn.session_id, [])
        turns.append(turn)
        max_turns = self.config.get("architecture_settings", {}).get(
            "max_session_turns", 50
        )
        if max_turns > 0 and len(turns) > max_turns:
            del turns[:-max_turns]

    def _prune_old(self) -> None:
        max_age_hours = self.config.get("time_settings", {}).get(
            "conversation_max_age_hours", 6
        )
        if max_age_hours <= 0:
            return
        cutoff = time.time() - max_age_hours * 3600
        for sid in list(self._sessions.keys()):
            kept = [t for t in self._sessions[sid] if t.created_at >= cutoff]
            if kept:
                self._sessions[sid] = kept
            else:
                del self._sessions[sid]

    @staticmethod
    def _format_turn(turn: ConversationTurn) -> str:
        parts = [f"用户: {turn.user_text}"]
        if turn.assistant_text:
            parts.append(f"助手: {turn.assistant_text}")
        return "\n".join(parts)
