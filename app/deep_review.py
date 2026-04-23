"""
Deep-review helpers (Minimax era — single-model).

Before: qwen3-coder (fast) -> swap VRAM -> deepseek-r1:32b (critique)
After:  minimax is always resident; Pass 2 is just a second call to the same
        model with a critique prompt. The model-swap, VRAM juggling, and the
        fast/deep split are all gone.

What's kept:
  - detect_security_patterns()         : heuristic security trigger
  - should_auto_trigger_deep_review()  : when to offer a critique pass
  - build_critique_prompt()            : Pass 2 critique prompt builder
  - merge_pass1_pass2()                : merge validation + new issues

The /review-deep endpoint in main.py remains as a backward-compat alias —
see decisions notes — but the "Deep Review" button is removed from the UI
because every minimax call is already "deep".
"""

import re
import json


# ---- Security pattern detection (auto-trigger rules) -----------------------

SECURITY_PATTERNS = {
    "authentication": [
        r"@Auth", r"authenticate", r"login", r"password", r"credential",
        r"token", r"jwt", r"oauth", r"session", r"SecurityContext",
    ],
    "cryptography": [
        r"encrypt", r"decrypt", r"hash", r"digest", r"cipher",
        r"MessageDigest", r"SecretKey", r"Signature", r"bcrypt",
    ],
    "sql_injection": [
        r"Statement\.", r"executeQuery", r"executeUpdate",
        r"createQuery.*\+", r"\"SELECT.*\+", r"\"INSERT.*\+",
        r"\"UPDATE.*\+", r"\"DELETE.*\+", r"rawQuery",
    ],
    "file_access": [
        r"FileInputStream", r"FileOutputStream", r"File\(",
        r"readFile", r"writeFile", r"fs\.", r"path\.join",
    ],
    "injection": [
        r"Runtime\.exec", r"ProcessBuilder", r"eval\(",
        r"innerHTML", r"document\.write", r"exec\(",
    ],
}


def detect_security_patterns(code: str) -> dict:
    """Scan code for security-sensitive patterns. Returns {category: [matches]}."""
    found = {}
    for category, patterns in SECURITY_PATTERNS.items():
        matches = []
        for p in patterns:
            if re.search(p, code, re.IGNORECASE):
                matches.append(p)
        if matches:
            found[category] = matches
    return found


def should_auto_trigger_deep_review(code: str, pass1_result: dict) -> dict:
    """
    Decide whether to *suggest* a deep-review/critique pass.

    Note: under minimax every call is already deep, so this mostly serves
    as a "you should take a closer look" signal. The /review-deep endpoint
    will still honor a manual trigger.
    """
    reasons = []

    # Security-sensitive code
    security = detect_security_patterns(code)
    if security:
        cats = ", ".join(security.keys())
        reasons.append(f"Security-sensitive patterns detected: {cats}")

    # Pass 1 found zero issues on >50 lines (suspicious)
    line_count = len(code.strip().split("\n"))
    issue_count = len(pass1_result.get("issues", []))
    if line_count > 50 and issue_count == 0:
        reasons.append(
            f"No issues found in {line_count} lines -- suspicious, worth a second look"
        )

    # Pass 1 found only low-severity issues on >100 lines
    if line_count > 100:
        severities = [i.get("severity", "") for i in pass1_result.get("issues", [])]
        if severities and all(s == "low" for s in severities):
            reasons.append(
                f"Only low-severity issues in {line_count} lines -- may be missing deeper problems"
            )

    return {
        "suggest": len(reasons) > 0,
        "reasons": reasons,
        "security_patterns": security,
    }


# ---- Critique prompt for Pass 2 --------------------------------------------

CRITIQUE_SYSTEM_PROMPT = """You are a senior code reviewer performing a second-pass critique. A first-pass review has already been done. Your job is NOT to start from scratch. Instead:

1. VALIDATE: Are any of the first-pass issues incorrect, misleading, or exaggerated?
2. CORRECT: Fix any wrong severity levels or inaccurate explanations
3. DEEPEN: Find significant issues the first pass missed entirely
4. SECURITY: Pay extra attention to security vulnerabilities if security patterns were detected

You follow these rules:
- Constructor injection ONLY -- never field injection
- Dependencies must be private final
- Controllers handle HTTP only -- delegate to services
- Never expose JPA entities -- use DTOs
- Null safety -- validate inputs at method entry
- No hardcoded URLs or magic strings
- Exception handling via @RestControllerAdvice

Respond ONLY in this JSON structure. No preamble. No markdown fences.

{
  "validated_issues": [
    {
      "original_id": 1,
      "status": "confirmed|corrected|removed",
      "corrected_severity": "high|medium|low",
      "correction_note": "why it was corrected or removed (empty if confirmed)"
    }
  ],
  "new_issues": [
    {
      "id": 100,
      "severity": "high|medium|low",
      "location": "line N, method/field name",
      "problem": "one sentence",
      "explanation": "why this is a problem",
      "fix": "corrected code snippet",
      "rule_violated": "which rule"
    }
  ],
  "summary": "one sentence assessment of the code after deep review",
  "security_notes": "any security concerns (empty if none)"
}

CRITICAL: Return ONLY valid JSON. No text before or after."""


def build_critique_prompt(
    code: str,
    language: str,
    pass1_result: dict,
    rag_context: str = "",
    security_patterns: dict = None,
) -> list[dict]:
    """Build the Pass 2 critique prompt (same model as Pass 1 — minimax)."""
    pass1_json = json.dumps(pass1_result, indent=2, default=str)

    security_note = ""
    if security_patterns:
        security_note = "\n[SECURITY ALERT]: The following security-sensitive patterns were detected:\n"
        for cat, patterns in security_patterns.items():
            security_note += f"  - {cat}: {', '.join(patterns)}\n"
        security_note += "Pay EXTRA attention to these areas.\n"

    # Same recency-biased order as assemble_prompt: code first, rules last
    user_prompt = f"""[LANGUAGE]: {language}

[ORIGINAL CODE]:
{code}

[FIRST-PASS REVIEW]:
{pass1_json}
{security_note}
[STYLE CONTEXT]:
{rag_context}

Review the first-pass analysis. Validate, correct, or deepen it. Find anything missed. Respond in the JSON format specified."""

    return [
        {"role": "system", "content": CRITIQUE_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]


# ---- Merge Pass 1 + Pass 2 results -----------------------------------------

def merge_pass1_pass2(pass1_result: dict, pass2_result: dict) -> dict:
    """
    Merge first-pass and second-pass results.
      - confirmed -> kept as-is
      - corrected -> updated severity / explanation note
      - removed   -> dropped (false positive per Pass 2)
      - new       -> appended from Pass 2
    """
    original_issues = pass1_result.get("issues", [])
    validated = pass2_result.get("validated_issues", [])
    new_issues = pass2_result.get("new_issues", [])

    validation_map = {v.get("original_id"): v for v in validated}

    merged = []
    for issue in original_issues:
        val = validation_map.get(issue.get("id"))

        if val is None:
            merged.append(issue)
        elif val.get("status") == "confirmed":
            merged.append(issue)
        elif val.get("status") == "corrected":
            if val.get("corrected_severity"):
                issue["severity"] = val["corrected_severity"]
            if val.get("correction_note"):
                issue["explanation"] = (
                    issue.get("explanation", "")
                    + f" [Deep review: {val['correction_note']}]"
                )
            merged.append(issue)
        elif val.get("status") == "removed":
            continue

    # Append new issues found in Pass 2
    next_id = max((i.get("id", 0) for i in merged), default=0) + 1
    for new_issue in new_issues:
        new_issue["id"] = next_id
        new_issue["rule_violated"] = new_issue.get(
            "rule_violated", "identified in deep review"
        )
        merged.append(new_issue)
        next_id += 1

    # Sort by severity, renumber
    severity_order = {"high": 0, "medium": 1, "low": 2}
    merged.sort(key=lambda x: severity_order.get(x.get("severity", "low"), 3))
    for i, issue in enumerate(merged):
        issue["id"] = i + 1

    # Summary
    high = sum(1 for i in merged if i.get("severity") == "high")
    med = sum(1 for i in merged if i.get("severity") == "medium")
    low = sum(1 for i in merged if i.get("severity") == "low")

    removed_count = sum(1 for v in validated if v.get("status") == "removed")
    new_count = len(new_issues)
    corrected_count = sum(1 for v in validated if v.get("status") == "corrected")

    summary = pass2_result.get(
        "summary", f"{len(merged)} issues after deep review ({high} high, {med} med, {low} low)"
    )
    if removed_count or new_count or corrected_count:
        summary += (
            f" [Deep review: {removed_count} removed, "
            f"{corrected_count} corrected, {new_count} new]"
        )

    security_notes = pass2_result.get("security_notes", "")

    violations = set(pass1_result.get("style_violations", []))
    for issue in new_issues:
        rv = issue.get("rule_violated", "")
        if rv:
            violations.add(rv)

    return {
        "issues": merged,
        "summary": summary,
        "style_violations": list(violations),
        "security_notes": security_notes,
        "deep_review_stats": {
            "confirmed": sum(1 for v in validated if v.get("status") == "confirmed"),
            "corrected": corrected_count,
            "removed": removed_count,
            "new_found": new_count,
            "total_after_merge": len(merged),
        },
    }
