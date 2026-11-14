"""Text repair helpers for Windows console/provider mojibake."""

from __future__ import annotations


_MOJIBAKE_MARKERS = (
    "鎵",
    "撳",
    "紑",
    "鎶",
    "栭",
    "煶",
    "骞",
    "舵",
    "悳",
    "绱",
    "㈡",
    "娊",
    "濂",
    "鍔",
    "╂",
    "墜",
    "涓",
    "鍏",
    "璇",
    "勮",
    "瀹",
    "寮",
    "€",
    "�",
)


def looks_like_mojibake(text: str) -> bool:
    """Return True for common UTF-8 Chinese text decoded as GBK/CP936."""
    if not text:
        return False
    hits = sum(1 for marker in _MOJIBAKE_MARKERS if marker in text)
    return hits >= 2 or ("�" in text and any(marker in text for marker in _MOJIBAKE_MARKERS))


def recover_mojibake(text: str) -> str:
    """Best-effort recovery for UTF-8 Chinese text decoded as GBK/CP936."""
    if not text or not looks_like_mojibake(text):
        return text

    candidates: list[str] = []
    for source_encoding in ("gbk", "cp936", "latin1"):
        try:
            repaired = text.encode(source_encoding, errors="strict").decode("utf-8")
        except UnicodeError:
            try:
                repaired = text.encode(source_encoding, errors="ignore").decode(
                    "utf-8", errors="ignore"
                )
            except UnicodeError:
                continue
        if repaired and repaired != text:
            candidates.append(repaired)

    if not candidates:
        return text

    return min(candidates, key=_mojibake_score)


def _mojibake_score(text: str) -> int:
    score = sum(text.count(marker) for marker in _MOJIBAKE_MARKERS)
    score += text.count("�") * 4
    return score
