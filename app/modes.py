"""
Mode handler for the Code Review Assistant.

Modes:
  "no"     -> Normal evaluation only. Find issues, explain them.
  "yes"    -> Evaluation + code suggestions (like Copilot). Show fixed code.
  "update" -> Directly apply changes. Show what changed and why.

Each mode uses a different system prompt addendum.
"""

MODE_PROMPTS = {
    "no": """
MODE: REVIEW ONLY
Find issues and explain them. Do NOT provide corrected full code.
Only show small fix snippets for each issue.
Focus on: what is wrong, why it matters, which rule is violated.""",

    "yes": """
MODE: REVIEW + SUGGEST
Find issues, explain them, AND provide a complete corrected version of the code.
After the issues list, include a "suggested_code" field with the full corrected code.
Add inline comments explaining each change.

Output must include this extra field:
"suggested_code": "the complete corrected code with // CHANGED: comments"
""",

    "update": """
MODE: DIRECT UPDATE
Find issues, explain them, AND provide the updated code ready to use.
After the issues list, include:
- "updated_code": the complete corrected code (ready to copy-paste)
- "changes": a list of what was changed and why

Output must include these extra fields:
"updated_code": "the complete corrected code",
"changes": [
  {"line": "line number or range", "what": "what changed", "why": "why it was needed"}
]

Keep the original code structure. Only fix the actual issues. Do not rewrite things that are correct.
""",
}


def get_mode_prompt(mode: str) -> str:
    """Get the mode-specific prompt addendum."""
    return MODE_PROMPTS.get(mode, MODE_PROMPTS["no"])


def validate_mode(mode: str) -> str:
    """Validate and normalize mode string."""
    mode = mode.lower().strip()
    if mode in ("yes", "no", "update"):
        return mode
    return "no"
