"""Rules for deciding whether an interaction represents genuine study."""

from __future__ import annotations

import re


_NON_ACADEMIC_ONLY = {
    "hi", "hello", "hey", "good morning", "good afternoon", "good evening",
    "thanks", "thank you", "ok", "okay", "test", "testing",
}
_ACADEMIC_TERMS = {
    "analyse", "analysis", "calculate", "code", "concept", "define", "derive",
    "design", "difference", "equation", "exam", "explain", "formula", "function",
    "how", "law", "meaning", "method", "module", "problem", "program", "proof",
    "question", "solve", "summarise", "summarize", "theorem", "topic", "why",
    "circuit", "control", "current", "data", "engineering", "feedback", "machine",
    "mathematics", "microprocessor", "power", "probability", "statistics",
    "system", "technical", "voltage",
}


def is_academic_question(value, *, has_module_context=False):
    """Reject greetings/spam while accepting real curriculum study requests."""
    text = " ".join((value or "").strip().lower().split())
    if len(text) < 8 or text in _NON_ACADEMIC_ONLY:
        return False
    if re.fullmatch(r"(.)\1{5,}", text.replace(" ", "")):
        return False
    words = set(re.findall(r"[a-z][a-z0-9+#-]*", text))
    hits = len(words & _ACADEMIC_TERMS)
    question_shape = "?" in text or bool(words & {
        "analyse", "calculate", "define", "derive", "design", "explain",
        "how", "solve", "summarise", "summarize", "what", "why",
    })
    if has_module_context:
        return len(words) >= 3 and (question_shape or hits >= 1)
    return len(words) >= 4 and hits >= 2 and question_shape
