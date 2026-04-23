"""
Custom suggestion system.
Users can add their own rules, improvements, and coding standards.
Suggestions are stored in a JSON file and injected into prompts.
"""

import json
import os
import time
from pathlib import Path

SUGGESTIONS_FILE = Path(os.getenv("SUGGESTIONS_FILE", "/app/suggestions/custom-rules.json"))


def _ensure_file():
    SUGGESTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not SUGGESTIONS_FILE.exists():
        SUGGESTIONS_FILE.write_text("[]")


def load_suggestions() -> list[dict]:
    _ensure_file()
    try:
        return json.loads(SUGGESTIONS_FILE.read_text())
    except Exception:
        return []


def save_suggestions(suggestions: list[dict]):
    _ensure_file()
    SUGGESTIONS_FILE.write_text(json.dumps(suggestions, indent=2))


def add_suggestion(
    title: str,
    rule: str,
    language: str = "all",
    category: str = "custom",
    severity: str = "medium",
    example_bad: str = "",
    example_good: str = "",
) -> dict:
    """Add a custom suggestion/rule."""
    suggestions = load_suggestions()

    new_id = f"custom-{len(suggestions)+1:03d}"
    entry = {
        "id": new_id,
        "title": title,
        "rule": rule,
        "language": language,
        "category": category,
        "severity": severity,
        "example_bad": example_bad,
        "example_good": example_good,
        "created_at": time.time(),
        "active": True,
    }
    suggestions.append(entry)
    save_suggestions(suggestions)
    return entry


def remove_suggestion(suggestion_id: str) -> bool:
    suggestions = load_suggestions()
    original_len = len(suggestions)
    suggestions = [s for s in suggestions if s["id"] != suggestion_id]
    if len(suggestions) < original_len:
        save_suggestions(suggestions)
        return True
    return False


def toggle_suggestion(suggestion_id: str) -> dict:
    suggestions = load_suggestions()
    for s in suggestions:
        if s["id"] == suggestion_id:
            s["active"] = not s["active"]
            save_suggestions(suggestions)
            return s
    return {}


def get_active_suggestions(language: str = "all") -> list[dict]:
    suggestions = load_suggestions()
    active = [s for s in suggestions if s.get("active", True)]
    if language != "all":
        active = [s for s in active if s.get("language", "all") in (language, "all")]
    return active


def format_suggestions_for_prompt(language: str) -> str:
    """Format active suggestions as text for injection into the LLM prompt."""
    active = get_active_suggestions(language)
    if not active:
        return ""

    lines = ["[CUSTOM RULES]"]
    for s in active:
        lines.append(f"- {s['title']}: {s['rule']}")
        if s.get("example_bad"):
            lines.append(f"  BAD: {s['example_bad']}")
        if s.get("example_good"):
            lines.append(f"  GOOD: {s['example_good']}")
    return "\n".join(lines)
