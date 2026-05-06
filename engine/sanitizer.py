"""Sanitize conversation text before learning or memory extraction."""

from __future__ import annotations

import json
import re
from typing import Any


TOOL_MARKERS = (
    "tool_call",
    "tool_calls",
    "function_call",
    "function result",
    "工具调用",
    "工具结果",
    "调用工具",
    "执行工具",
)


class ConversationSanitizer:
    """Remove internal tool traces from text that may become long-term memory."""

    _fenced_tool_re = re.compile(
        r"```(?:json|tool|function)?\s*[\s\S]*?(?:tool_calls?|function_call|工具调用)[\s\S]*?```",
        re.IGNORECASE,
    )

    @classmethod
    def clean_text(cls, text: str, config: dict | None = None) -> str:
        if not text:
            return ""
        cfg = (config or {}).get("sanitizer_settings", {})
        if not cfg.get("enabled", True):
            return text.strip()
        if cfg.get("strip_fenced_tool_blocks", True):
            text = cls._fenced_tool_re.sub("", text)
        kept = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if cls._looks_like_tool_trace(stripped, cfg):
                continue
            kept.append(line)
        return "\n".join(kept).strip()

    @classmethod
    def clean_turn(
        cls,
        user_text: str,
        assistant_text: str = "",
        config: dict | None = None,
    ) -> tuple[str, str]:
        return cls.clean_text(user_text, config), cls.clean_text(assistant_text, config)

    @classmethod
    def _looks_like_tool_trace(cls, line: str, cfg: dict | None = None) -> bool:
        cfg = cfg or {}
        low = line.lower()
        extra_markers = cfg.get("extra_tool_markers", [])
        if isinstance(extra_markers, str):
            extra_markers = [x.strip() for x in extra_markers.split(",") if x.strip()]
        markers = tuple(str(x).lower() for x in (*TOOL_MARKERS, *extra_markers))
        if any(marker in low for marker in markers):
            return True
        if cfg.get("strip_json_tool_traces", True) and line.startswith(("{", "[")) and cls._json_has_tool_shape(line):
            return True
        role_pattern = cfg.get("tool_role_line_pattern", r"^(tool|function|observation|工具)\s*[:：]")
        if role_pattern and re.match(str(role_pattern), line, re.IGNORECASE):
            return True
        return False

    @staticmethod
    def _json_has_tool_shape(text: str) -> bool:
        try:
            value: Any = json.loads(text)
        except json.JSONDecodeError:
            return False
        encoded = json.dumps(value, ensure_ascii=False).lower()
        return any(marker in encoded for marker in TOOL_MARKERS)
