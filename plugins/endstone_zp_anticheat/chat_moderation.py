from __future__ import annotations

import re
import time
import unicodedata
from collections import defaultdict, deque
from typing import Any, Iterable


DEFAULT_INSULT_TERMS = (
    "傻逼", "傻比", "傻屄", "煞笔", "傻b", "sb",
    "脑残", "脑瘫", "智障", "废物", "你是垃圾", "你真垃圾", "垃圾玩家", "狗东西",
    "狗杂种", "王八蛋", "婊子", "贱人", "臭婊子",
    "操你妈", "草泥马", "cnm", "nmsl", "去死",
)


def normalize_chat_text(value: Any) -> str:
    """Normalize common spacing/punctuation/width obfuscation for phrase matching."""
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = "".join(
        char for char in text
        if not unicodedata.category(char).startswith("C")
        and not char.isspace()
        and not unicodedata.category(char).startswith(("P", "S"))
    )
    return re.sub(r"(.)\1{2,}", r"\1\1", text)


def detect_insult_terms(message: Any, terms: Iterable[str] = DEFAULT_INSULT_TERMS) -> tuple[str, ...]:
    normalized = normalize_chat_text(message)
    if not normalized:
        return ()
    matches: list[str] = []
    for term in terms:
        candidate = normalize_chat_text(term)
        if not candidate:
            continue
        if candidate.isascii() and candidate.isalnum():
            matched = re.search(r"(?<![a-z0-9])" + re.escape(candidate) + r"(?![a-z0-9])", normalized) is not None
        else:
            matched = candidate in normalized
        if matched and term not in matches:
            matches.append(str(term))
    return tuple(matches)


class RecentChatStore:
    """Bounded in-memory chat history; no chat text is written to disk."""

    def __init__(
        self,
        window_seconds: float = 600.0,
        max_messages_per_player: int = 50,
        clock=time.time,
    ) -> None:
        self.window_seconds = max(1.0, float(window_seconds))
        self.max_messages_per_player = max(1, int(max_messages_per_player))
        self._clock = clock
        self._messages: dict[str, deque[dict[str, Any]]] = defaultdict(deque)

    def _prune(self, player_uuid: str, now: float) -> deque[dict[str, Any]]:
        queue = self._messages[str(player_uuid)]
        cutoff = now - self.window_seconds
        while queue and (queue[0]["timestamp"] <= cutoff or len(queue) > self.max_messages_per_player):
            queue.popleft()
        return queue

    def record(self, player_uuid: str, message: Any, now: float | None = None) -> None:
        key = str(player_uuid or "")
        text = str(message or "").strip()
        if not key or not text:
            return
        current = self._clock() if now is None else float(now)
        queue = self._prune(key, current)
        queue.append({"timestamp": current, "message": text[:300]})
        while len(queue) > self.max_messages_per_player:
            queue.popleft()

    def recent(self, player_uuid: str, now: float | None = None) -> list[dict[str, Any]]:
        current = self._clock() if now is None else float(now)
        queue = self._prune(str(player_uuid), current)
        return [dict(item) for item in queue]

    def find_insults(
        self,
        player_uuid: str,
        terms: Iterable[str] = DEFAULT_INSULT_TERMS,
        now: float | None = None,
    ) -> list[dict[str, Any]]:
        matches: list[dict[str, Any]] = []
        for entry in self.recent(player_uuid, now=now):
            found = detect_insult_terms(entry["message"], terms)
            if found:
                matches.append({**entry, "matched_terms": list(found)})
        return matches
