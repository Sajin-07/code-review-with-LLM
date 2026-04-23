"""
Token routing pre-flight check (Minimax era, v2).

Replaces the old qwen3-calibrated thresholds. Under minimax m2.5:
  - 65,536-token context window
  - ~4.6 tokens/sec generation on this hardware
  - 45-minute HTTP request timeout

Per-mode limits calibrated to what the hardware can legitimately finish
within the timeout. See DECISIONS.md for the math.

Route decisions:
  - send_as_is      : tiny input (<=500 lines equivalent), one LLM call
  - single_call     : medium input fits in context, one LLM call
  - two_pass        : Update mode >500 lines — Pass 1 review, Pass 2 fix
                      selected functions, Python merges
  - chunk_by_class  : >5,000 lines in review-only mode, split by class
  - reject          : over the ceiling for the chosen mode
"""

import tiktoken

_enc = tiktoken.get_encoding("cl100k_base")


# ---- Fixed overhead (system + RAG + call graph + custom rules) ------------
# Matches plan §3: ~700 + ~1,200 + ~150 + ~100 ~= 2,150 tokens
FIXED_OVERHEAD_TOKENS = 2_150


# ---- Per-mode hard limits (in code tokens) --------------------------------
#
# All numbers are CODE tokens (not including overhead, reasoning, or output).
# Approximate lines-to-tokens conversion: 1 line of Java ~= 10 tokens.
#   40,000 code tokens ~= 4,000 lines
#   20,000 code tokens ~= 2,000 lines
#    5,000 code tokens ~=   500 lines

# Review Only: output is just an issue list (~2-5k output tokens).
# Scales slowly with input. Highest input ceiling.
REVIEW_MAX_CODE_TOKENS = 40_000       # ~4,000 lines

# Suggest Code: output is corrected function bodies (~30-50% of broken input).
# Scales moderately. Medium ceiling.
SUGGEST_MAX_CODE_TOKENS = 20_000      # ~2,000 lines

# Auto Update single-call: regenerates the entire file (output ~ input).
# Tightest single-call ceiling.
UPDATE_SINGLE_CALL_MAX = 5_000        # ~500 lines

# Auto Update two-pass: Pass 1 is review-only on the full file, Pass 2 fixes
# user-selected functions (small). Pass 1 needs as much headroom as Review.
UPDATE_TWO_PASS_MAX = 50_000          # ~5,000 lines

# Review-only chunking (class-boundary) kicks in above this.
CHUNK_THRESHOLD = 50_000              # ~5,000 lines

# Absolute ceiling — any input above this is rejected regardless of mode,
# because even Review Only can't fit code + reasoning + output in the 65k window.
ABSOLUTE_CEILING = 60_000             # ~6,000 lines


# ---- Legacy aliases (keep imports from other modules working) --------------
SEND_AS_IS_MAX = UPDATE_SINGLE_CALL_MAX
SINGLE_CALL_MAX = REVIEW_MAX_CODE_TOKENS
CHUNK_MAX = ABSOLUTE_CEILING


def count_tokens(text: str) -> int:
    return len(_enc.encode(text))


def route_by_token_count(
    code: str,
    system_prompt_tokens: int = 700,
    rag_tokens: int = 1_200,
    mode: str = "no",
) -> dict:
    """
    Determine how to handle the input.

    Args:
        code: the submitted source code
        system_prompt_tokens: approximate system prompt size (default 700)
        rag_tokens: approximate RAG context size (default 1200)
        mode: "no" | "yes" | "update"

    Returns:
        {
            "route":        "send_as_is" | "single_call" | "two_pass" |
                            "chunk_by_class" | "reject",
            "code_tokens":  int,
            "total_tokens": int,
            "line_count":   int,
            "reason":       str,
        }
    """
    code_tokens = count_tokens(code)
    line_count = len(code.split("\n"))
    question_tokens = 20
    total = system_prompt_tokens + rag_tokens + code_tokens + question_tokens

    # ---- Absolute ceiling (applies to all modes) ----
    if code_tokens >= ABSOLUTE_CEILING:
        return {
            "route": "reject",
            "code_tokens": code_tokens,
            "total_tokens": total,
            "line_count": line_count,
            "reason": (
                f"Input too large (~{code_tokens:,} code tokens, "
                f"{line_count:,} lines). Absolute max is "
                f"~{ABSOLUTE_CEILING:,} tokens (~6,000 lines). "
                f"Paste a single class or file."
            ),
        }

    # ---- Mode-specific rejection ----
    if mode == "yes" and code_tokens > SUGGEST_MAX_CODE_TOKENS:
        return {
            "route": "reject",
            "code_tokens": code_tokens,
            "total_tokens": total,
            "line_count": line_count,
            "reason": (
                f"Input too large for Suggest Code mode "
                f"(~{code_tokens:,} code tokens, {line_count:,} lines). "
                f"Suggest mode accepts up to ~{SUGGEST_MAX_CODE_TOKENS:,} "
                f"tokens (~2,000 lines). Switch to Review Only for "
                f"larger files, or paste a single class."
            ),
        }

    if mode == "no" and code_tokens > REVIEW_MAX_CODE_TOKENS:
        return {
            "route": "reject",
            "code_tokens": code_tokens,
            "total_tokens": total,
            "line_count": line_count,
            "reason": (
                f"Input too large for Review Only mode "
                f"(~{code_tokens:,} code tokens, {line_count:,} lines). "
                f"Review accepts up to ~{REVIEW_MAX_CODE_TOKENS:,} tokens "
                f"(~4,000 lines). Paste a single class or file."
            ),
        }

    if mode == "update" and code_tokens > UPDATE_TWO_PASS_MAX:
        return {
            "route": "reject",
            "code_tokens": code_tokens,
            "total_tokens": total,
            "line_count": line_count,
            "reason": (
                f"Input too large for Auto Update mode "
                f"(~{code_tokens:,} code tokens, {line_count:,} lines). "
                f"Auto Update accepts up to ~{UPDATE_TWO_PASS_MAX:,} "
                f"tokens (~5,000 lines). Paste a single class, or use "
                f"Review Only to inspect this file first."
            ),
        }

    # ---- Route selection for accepted inputs ----

    # Auto Update: split by size
    if mode == "update":
        if code_tokens <= UPDATE_SINGLE_CALL_MAX:
            return {
                "route": "send_as_is",
                "code_tokens": code_tokens,
                "total_tokens": total,
                "line_count": line_count,
                "reason": (
                    f"{code_tokens} code tokens ({line_count} lines) -- "
                    f"single-call Auto Update."
                ),
            }
        # > 500 lines in Update mode -> 2-pass function-selector flow
        return {
            "route": "two_pass",
            "code_tokens": code_tokens,
            "total_tokens": total,
            "line_count": line_count,
            "reason": (
                f"{code_tokens} code tokens ({line_count} lines) in Auto "
                f"Update -- using 2-pass flow (review, pick functions, fix)."
            ),
        }

    # Review Only or Suggest Code under the small threshold
    if code_tokens <= UPDATE_SINGLE_CALL_MAX:
        return {
            "route": "send_as_is",
            "code_tokens": code_tokens,
            "total_tokens": total,
            "line_count": line_count,
            "reason": (
                f"{code_tokens} code tokens ({line_count} lines) -- "
                f"well within single-call budget."
            ),
        }

    # Review/Suggest in the medium band
    if code_tokens < CHUNK_THRESHOLD:
        return {
            "route": "single_call",
            "code_tokens": code_tokens,
            "total_tokens": total,
            "line_count": line_count,
            "reason": (
                f"{code_tokens} code tokens ({line_count} lines) -- "
                f"single call fits in minimax's 65k window."
            ),
        }

    # Review Only in the 50k-60k band -> chunk by class
    return {
        "route": "chunk_by_class",
        "code_tokens": code_tokens,
        "total_tokens": total,
        "line_count": line_count,
        "reason": (
            f"{code_tokens} code tokens ({line_count} lines) -- "
            f"too large for a single call. Review-only, chunked "
            f"at class boundary."
        ),
    }
