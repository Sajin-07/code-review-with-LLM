"""
Team configuration.
Defines teams, their repos, and conventions.
Add new teams here and they appear in the UI automatically.
"""

import json
import os
from pathlib import Path

TEAMS_FILE = Path(os.getenv("TEAMS_FILE", "/app/config/teams.json"))

DEFAULT_TEAMS = [
    {
        "id": "petclinic-backend",
        "name": "PetClinic Backend",
        "description": "Spring Boot REST API team",
        "languages": ["java"],
        "repo": "spring-petclinic-rest",
    },
    {
        "id": "petclinic-frontend",
        "name": "PetClinic Frontend",
        "description": "Angular frontend team",
        "languages": ["typescript"],
        "repo": "spring-petclinic-angular",
    },
    {
        "id": "shared",
        "name": "Company-Wide (Shared)",
        "description": "Rules that apply to all teams",
        "languages": ["all"],
        "repo": "",
    },
]


def _ensure_file():
    TEAMS_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not TEAMS_FILE.exists():
        TEAMS_FILE.write_text(json.dumps(DEFAULT_TEAMS, indent=2))


def load_teams() -> list[dict]:
    _ensure_file()
    try:
        return json.loads(TEAMS_FILE.read_text())
    except Exception:
        return DEFAULT_TEAMS


def save_teams(teams: list[dict]):
    _ensure_file()
    TEAMS_FILE.write_text(json.dumps(teams, indent=2))


def add_team(team_id: str, name: str, description: str = "",
             languages: list[str] = None, repo: str = "") -> dict:
    teams = load_teams()
    # Check if team already exists
    for t in teams:
        if t["id"] == team_id:
            return t  # already exists
    new_team = {
        "id": team_id,
        "name": name,
        "description": description,
        "languages": languages or ["all"],
        "repo": repo,
    }
    teams.append(new_team)
    save_teams(teams)
    return new_team


def remove_team(team_id: str) -> bool:
    if team_id in ("shared",):
        return False  # cannot remove shared
    teams = load_teams()
    original = len(teams)
    teams = [t for t in teams if t["id"] != team_id]
    if len(teams) < original:
        save_teams(teams)
        return True
    return False


def get_team(team_id: str) -> dict:
    for t in load_teams():
        if t["id"] == team_id:
            return t
    return {"id": team_id, "name": team_id, "description": "", "languages": ["all"], "repo": ""}
