"""
Prompt assembly with mode support, custom suggestions, and multi-language.

Plan §3 fix: RAG context now comes AFTER the code, immediately before the
question. LLMs are recency-biased — what they read last they follow most
closely — so rules belong at the end, not buried above the code.
"""

from app.modes import get_mode_prompt, validate_mode
from app.suggestions import format_suggestions_for_prompt

SYSTEM_PROMPT_BASE = """You are a senior code reviewer for a software company. Your job is to find bugs, style violations, and improvement opportunities in submitted code.

You follow these universal rules:
1. Constructor injection ONLY -- never field injection (@Autowired on fields) or setter injection
2. Dependencies must be private final
3. Controllers handle HTTP only -- no business logic. Delegate to services
4. Services own business logic and transactions
5. Never expose JPA entities in API responses -- use DTOs
6. Return ResponseEntity<T> with explicit HTTP status codes
7. Use @Valid on @RequestBody for input validation
8. Exception handling via @RestControllerAdvice -- no generic catch(Exception) in controllers
9. Null safety -- validate inputs at method entry with Objects.requireNonNull or @NotNull
10. Method names must be intention-revealing: verb + noun (findOwnerById, not get)
11. No hardcoded URLs or magic strings -- use constants or environment config
12. Angular: ngOnInit for initialization, constructor for DI only
13. Angular: unsubscribe from Observables -- use takeUntil(destroy$) pattern
14. Angular: no nested subscribes -- use RxJS operators (switchMap, mergeMap)
15. Avoid any type in TypeScript -- define interfaces for all data structures

For non-Java/TypeScript languages, apply equivalent best practices:
- Python: type hints, avoid bare except, use context managers, PEP 8
- Go: error handling (no ignored errors), defer for cleanup, effective Go style
- Rust: proper error handling with Result, avoid unwrap in production, ownership rules
- C#: async/await patterns, IDisposable, dependency injection via constructor
- General: meaningful names, single responsibility, input validation, error handling

IMPORTANT: When a [CALL GRAPH] is provided, use it to detect cross-method bugs.

{mode_prompt}

Respond ONLY in this JSON structure. No preamble. No markdown fences. No explanation outside the JSON.

{{
  "language": "detected language",
  "issues": [
    {{
      "id": 1,
      "severity": "high|medium|low",
      "location": "line N, method/field name",
      "problem": "one sentence",
      "explanation": "why this is a problem",
      "fix": "corrected code snippet",
      "rule_violated": "which rule"
    }}
  ],
  "summary": "one sentence overall assessment",
  "style_violations": ["list of style rules broken"],
  "method_summary": "2-3 sentences describing what this method does and its contracts"
  {extra_fields}
}}

If the code has no issues, return an empty issues array with a positive summary.
CRITICAL: Return ONLY valid JSON. No text before or after the JSON object."""


# Mode-specific extra fields that get injected into the JSON schema
def _get_extra_fields(mode: str) -> str:
    if mode == "yes":
        return ',\n  "suggested_code": "complete corrected code with // CHANGED: comments"'
    elif mode == "update":
        return (
            ',\n  "updated_code": "complete corrected code",'
            '\n  "changes": [{"line": "N", "what": "description", "why": "reason"}],'
            '\n  "affected_functions": ["list of function/method names that were changed"]'
        )
    return ""


def assemble_prompt(
    language: str,
    code: str,
    question: str,
    rag_context: str,
    previous_summaries: str = "",
    mode: str = "no",
    show_reasoning: bool = False,
) -> list[dict]:
    """
    Build messages array for the LLM with mode and suggestion support.

    User message order (plan §3 recency-bias fix):
      [LANGUAGE]
      [METHODS ALREADY REVIEWED]   (carry-forward, when chunking)
      [CALL GRAPH]                  (already embedded into `code` by chunker
                                     when applicable -- left as-is)
      [CODE]
      [STYLE CONTEXT]               <-- now LAST content block
      [CUSTOM RULES]
      [QUESTION]                    <-- actual ask closes the message
    """
    mode = validate_mode(mode)
    mode_prompt = get_mode_prompt(mode)
    extra_fields = _get_extra_fields(mode)

    system = SYSTEM_PROMPT_BASE.format(mode_prompt=mode_prompt, extra_fields=extra_fields)

    if show_reasoning:
        system += "\n\nInclude your reasoning process in a 'reasoning' field in the JSON."

    # Build user prompt in the new (recency-biased) order
    user_parts = [f"[LANGUAGE]: {language}"]

    if previous_summaries:
        user_parts.append(f"\n[METHODS ALREADY REVIEWED]\n{previous_summaries}")

    # Code first (may include [CALL GRAPH] header already prepended by chunker)
    user_parts.append(f"\n[CODE]:\n{code}")

    # RAG context now comes AFTER the code
    if rag_context:
        user_parts.append(f"\n[STYLE CONTEXT]:\n{rag_context}")

    # Custom rules come after RAG context (still close to the question)
    custom = format_suggestions_for_prompt(language)
    if custom:
        user_parts.append(f"\n{custom}")

    # Question closes the message — maximum recency weight on the actual ask
    user_parts.append(f"\n[QUESTION]: {question}")
    user_parts.append("\nRespond in the JSON format specified in your instructions.")

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n".join(user_parts)},
    ]


def assemble_regenerate_prompt(
    language: str,
    team: str,
    issue: dict,
    original_code: str,
    previous_fixes: list[str],
    rag_context: str,
) -> list[dict]:
    """
    Prompt for the "Try different fix" feature (plan §7).

    Very small request (~200-500 input tokens) — only the specific function,
    the issue, and the previous fix(es) to avoid. Does NOT re-run the full
    review pipeline.
    """
    system = """You are a senior code reviewer generating an alternative fix for a specific issue.

A developer has reviewed your previous suggestion and wants to see a different approach. Your job:

1. Propose a DIFFERENT fix for the same issue
2. Do NOT repeat any of the previous attempts listed
3. Maintain all company coding rules (see [STYLE CONTEXT])
4. Consider alternative valid approaches (e.g. Lombok, a different design pattern, a helper method)
5. Briefly explain why this alternative is valid

Respond ONLY in this JSON structure. No preamble. No markdown fences.

{
  "issue_id": <the issue id>,
  "alternative_fix": "the new code fix",
  "approach_name": "short label for this approach (e.g. 'Lombok @RequiredArgsConstructor')",
  "why_valid": "one or two sentences explaining why this satisfies the rule",
  "rule_maintained": "which rule this still follows"
}

CRITICAL: Return ONLY valid JSON. No text before or after."""

    prev_fixes_block = "\n\n".join(
        f"[PREVIOUS FIX #{i+1} -- DO NOT REPEAT]:\n{fix}"
        for i, fix in enumerate(previous_fixes)
    ) if previous_fixes else "(no previous fixes — this is the first regenerate)"

    user = f"""[LANGUAGE]: {language}
[TEAM]: {team}
[ISSUE #{issue.get('id', '?')}]: {issue.get('problem', '')}
[LOCATION]: {issue.get('location', '')}
[RULE VIOLATED]: {issue.get('rule_violated', '')}
[EXPLANATION]: {issue.get('explanation', '')}

[ORIGINAL CODE]:
{original_code}

{prev_fixes_block}

[STYLE CONTEXT]:
{rag_context}

[INSTRUCTION]:
Generate a DIFFERENT fix for this issue that still satisfies the rule.
Do not repeat any of the previous fixes shown above.
Respond in the JSON format specified."""

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
