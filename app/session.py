"""
Chat session manager.

In-memory sessions, keyed by short UUID. Each session tracks:
  - messages         : chat history
  - current_code     : most recent code submitted for review
  - current_language : detected language
  - last_review      : merged review result (for follow-ups)
  - last_preview     : pending preview before user approval (update mode)
  - issue_fix_history: {issue_key: [fix_attempt_1, fix_attempt_2, ...]}
                       Used by the "Try different fix" regenerate flow (plan §7)
                       so we can tell the model "do not repeat any of these".

Sessions expire after `max_age` seconds (default 2 hours).
"""

import uuid
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Message:
    role: str          # "user" | "assistant" | "system"
    content: str
    timestamp: float = 0.0
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = time.time()

    def to_dict(self) -> dict:
        return {
            "role": self.role,
            "content": self.content,
            "timestamp": self.timestamp,
            "metadata": self.metadata,
        }


@dataclass
class Session:
    session_id: str
    created_at: float = 0.0
    messages: list[Message] = field(default_factory=list)
    current_code: str = ""
    current_language: str = ""
    current_team: str = ""
    last_review: Optional[dict] = None
    last_preview: Optional[dict] = None
    # Map of issue-key -> list of fix strings already tried. The key is
    # whatever make_issue_key() returns (see below) — usually
    # f"{issue_id}:{location}:{problem-hash}" so we're robust to renumbering.
    issue_fix_history: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.created_at:
            self.created_at = time.time()

    def add_message(self, role: str, content: str, metadata: dict = None) -> Message:
        msg = Message(role=role, content=content, metadata=metadata or {})
        self.messages.append(msg)
        return msg

    def get_history(self, limit: int = 50) -> list[dict]:
        return [m.to_dict() for m in self.messages[-limit:]]

    # ---- Fix history (for regenerate) --------------------------------------

    def record_fix_attempt(self, issue_key: str, fix: str) -> None:
        """Append a fix attempt to the history for this issue."""
        if not fix:
            return
        self.issue_fix_history.setdefault(issue_key, []).append(fix)

    def get_fix_history(self, issue_key: str) -> list[str]:
        return list(self.issue_fix_history.get(issue_key, []))

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "created_at": self.created_at,
            "message_count": len(self.messages),
            "current_language": self.current_language,
            "current_team": self.current_team,
            "has_code": bool(self.current_code),
            "has_review": self.last_review is not None,
            "has_preview": self.last_preview is not None,
            "issues_with_regens": len(self.issue_fix_history),
        }


def make_issue_key(issue: dict) -> str:
    """
    Stable identifier for an issue across regenerates.
    We avoid depending on `id` alone because merges and repasses can renumber.
    """
    loc = (issue.get("location") or "").strip()
    problem = (issue.get("problem") or "").strip()
    # Small digest keeps keys short
    h = str(abs(hash(problem)) % 10_000_000)
    return f"{issue.get('id', '?')}|{loc}|{h}"


class SessionManager:
    """In-memory session storage. Sessions expire after max_age seconds."""

    def __init__(self, max_age: int = 3600):
        self._sessions: dict[str, Session] = {}
        self._max_age = max_age

    def create_session(self) -> Session:
        sid = str(uuid.uuid4())[:8]
        session = Session(session_id=sid)
        self._sessions[sid] = session
        self._cleanup()
        return session

    def get_session(self, session_id: str) -> Optional[Session]:
        session = self._sessions.get(session_id)
        if session and (time.time() - session.created_at) > self._max_age:
            del self._sessions[session_id]
            return None
        return session

    def get_or_create(self, session_id: str = "") -> Session:
        if session_id:
            existing = self.get_session(session_id)
            if existing:
                return existing
        return self.create_session()

    def list_sessions(self) -> list[dict]:
        self._cleanup()
        return [s.to_dict() for s in self._sessions.values()]

    def _cleanup(self):
        now = time.time()
        expired = [
            sid for sid, s in self._sessions.items()
            if (now - s.created_at) > self._max_age
        ]
        for sid in expired:
            del self._sessions[sid]


# Global instance — 2-hour session TTL, unchanged
sessions = SessionManager(max_age=7200)
