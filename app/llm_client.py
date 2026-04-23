"""
Minimax LLM client (OpenAI-compatible, via llama-server).

Replaces the previous Ollama client.
- Endpoint:     POST {MINIMAX_URL}/v1/chat/completions
- Response:     choices[0].message.content
- Timeout:      180s default (minimax runs with -ngl 11, CPU-heavy)
- max_tokens:   dynamic, computed from code size and review mode
- <think> tags: stripped before JSON parsing (minimax is a thinking model)

All reviews are "deep by default" — the qwen3 <-> deepseek model swap is gone.
VRAM management functions (unload_model / preload_model) have also been removed.
"""

import os
import re
import json
import httpx
import tiktoken

# ---- Configuration ----------------------------------------------------------

MINIMAX_URL = os.getenv("MINIMAX_URL", "http://host.docker.internal:8080")
MINIMAX_MODEL = os.getenv("MINIMAX_MODEL", "minimax")
MINIMAX_API_KEY = os.getenv("MINIMAX_API_KEY", "")

# Kept as exported constants for backward compatibility (main.py, deep_review.py
# still import MODEL_FAST / MODEL_DEEP). After migration both point to the same
# model — there is no fast/deep split anymore.
MODEL_FAST = MINIMAX_MODEL
MODEL_DEEP = MINIMAX_MODEL

# HTTP timeout. Plan mandates >=120s; default 180s per "Important performance note".
DEFAULT_TIMEOUT = float(os.getenv("MINIMAX_TIMEOUT", "2700.0"))  # 45 min (plan: covers worst-case Suggest/Update legitimate wait at ~4.6 tok/s)

_enc = tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    return len(_enc.encode(text))


# ---- Dynamic max_tokens -----------------------------------------------------

def estimate_output_tokens(code_tokens: int, mode: str) -> int:
    """
    Compute a sensible max_tokens for the response based on input size and mode.

    Plan section 3 (thinking-model-aware — floors raised from original plan
    to account for minimax's 2000-3000 token reasoning overhead):
      update  -> min(30_000, max(6_000, int(code_tokens * 1.2)))
      yes     -> min(10_000, max(5_000, int(code_tokens * 0.4)))
      no      -> min(6_000,  max(4_000, int(code_tokens * 0.15)))
    """
    # NOTE on floors: Minimax m2.5 is a thinking model. Its reasoning phase
    # typically consumes 2000-3000 tokens BEFORE the JSON answer begins.
    # Floors below ~4000 tokens tend to truncate the JSON mid-answer.
    if mode == "update":
        return min(30_000, max(6_000, int(code_tokens * 1.2)))
    elif mode == "yes":
        return min(10_000, max(5_000, int(code_tokens * 0.4)))
    else:  # "no" — review only
        return min(6_000, max(4_000, int(code_tokens * 0.15)))


# ---- JSON extraction from messy LLM output ---------------------------------

def _extract_json(raw: str) -> tuple[dict, bool]:
    """
    Extract valid JSON from model output.
    Handles: <think> blocks, markdown fences, preamble text.

    Returns (parsed_dict, was_truncated).
    was_truncated = True when brace depth never returned to zero — the response
    hit max_tokens mid-JSON. The UI surfaces this as a "partial review" warning
    rather than a hard failure.
    """
    # 1. Strip minimax thinking tags
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
    # Unclosed <think> (timeout mid-reasoning) — drop everything from the tag on
    raw = re.sub(r"<think>.*$", "", raw, flags=re.DOTALL)
    raw = raw.strip()

    # 2. Strip markdown fences
    raw = re.sub(r"```json\s*", "", raw)
    raw = re.sub(r"```\s*", "", raw)
    raw = raw.strip()

    # 3. Try direct parse
    try:
        return json.loads(raw), False
    except json.JSONDecodeError:
        pass

    # 4. Find outermost { ... } by brace depth, respecting strings
    start = raw.find("{")
    if start == -1:
        raise ValueError(f"No JSON object found in response: {raw[:200]}")

    depth = 0
    end = -1
    in_string = False
    escape = False
    for i in range(start, len(raw)):
        ch = raw[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break

    truncated = depth != 0
    candidate = raw[start:end] if end != -1 else raw[start:]

    # 5. If truncated, attempt a repair (close open braces/brackets)
    if truncated:
        repaired = _attempt_truncation_repair(candidate)
        try:
            return json.loads(repaired), True
        except json.JSONDecodeError:
            raise ValueError(
                f"Response truncated mid-JSON (depth={depth} at end). "
                f"Raw length={len(raw)}. Tail: ...{raw[-200:]!r}"
            )

    try:
        return json.loads(candidate), False
    except json.JSONDecodeError as e:
        raise ValueError(f"Failed to parse extracted JSON: {e}\nRaw: {candidate[:500]}")


def _attempt_truncation_repair(candidate: str) -> str:
    """
    Best-effort repair for output that hit max_tokens mid-JSON.
    Closes any unterminated string, then pads with the right number of
    closing brackets/braces to balance depth.
    """
    s = candidate.rstrip().rstrip(",")

    in_string = False
    escape = False
    brace_depth = 0
    bracket_depth = 0
    for ch in s:
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            brace_depth += 1
        elif ch == "}":
            brace_depth -= 1
        elif ch == "[":
            bracket_depth += 1
        elif ch == "]":
            bracket_depth -= 1

    if in_string:
        s += '"'
    s += "]" * max(0, bracket_depth)
    s += "}" * max(0, brace_depth)
    return s


# ---- Response validation ---------------------------------------------------

def _validate_response(data: dict) -> dict:
    """Ensure the response matches our schema. Fill missing fields with defaults."""
    if "issues" not in data:
        data["issues"] = []
    if "summary" not in data:
        data["summary"] = "Review complete."
    if "style_violations" not in data:
        data["style_violations"] = []
    if "language" not in data:
        data["language"] = "unknown"

    validated_issues = []
    for i, issue in enumerate(data["issues"]):
        if not isinstance(issue, dict):
            continue
        validated_issues.append({
            "id": issue.get("id", i + 1),
            "severity": issue.get("severity", "medium"),
            "location": issue.get("location", "unknown"),
            "problem": issue.get("problem", "Issue detected"),
            "explanation": issue.get("explanation", ""),
            "fix": issue.get("fix", ""),
            "rule_violated": issue.get("rule_violated", "unspecified"),
        })
    data["issues"] = validated_issues
    return data


# ---- Deprecated VRAM-management shims (no-ops) ------------------------------
#
# The old two-model (qwen3-coder / deepseek-r1:32b) swap is gone. Minimax is
# always resident. These async shims remain so existing callers in
# main.py / deep_review.py don't break — they simply do nothing now.

async def unload_model(model: str = ""):
    return None


async def preload_model(model: str = ""):
    return None


# ---- Main LLM call ---------------------------------------------------------

async def call_llm(
    messages: list[dict],
    model: str = None,
    timeout: float = None,
    max_tokens: int = None,
    mode: str = "no",
    temperature: float = 0.1,
) -> dict:
    """
    Call minimax via the OpenAI-compatible /v1/chat/completions endpoint
    and return parsed, validated JSON.

    Args:
        messages:     [{role, content}, ...]
        model:        override the configured model name (rarely useful)
        timeout:      per-request HTTP timeout; defaults to DEFAULT_TIMEOUT
        max_tokens:   explicit cap for the response; if None, computed from
                      mode + code size via estimate_output_tokens()
        mode:         "no" | "yes" | "update" — used only when max_tokens is None
        temperature:  sampling temperature (0.1 for normal review, 0.5-0.7
                      when regenerating alternative fixes — see main.py)

    Returns:
        {
            "result":        validated dict,
            "raw":           str (full content from the model),
            "model":         str,
            "input_tokens":  int,
            "output_tokens": int,
            "truncated":     bool (True if response hit max_tokens mid-JSON),
            "error":         str | None,
        }
    """
    if model is None:
        model = MINIMAX_MODEL
    if timeout is None:
        timeout = DEFAULT_TIMEOUT

    # Compute input tokens from all message content
    input_text = " ".join(m.get("content", "") for m in messages)
    input_tokens = count_tokens(input_text)

    # Determine max_tokens if not given.
    # Approximation: input minus non-code overhead (~2150 tokens per plan §3).
    if max_tokens is None:
        code_tokens = max(0, input_tokens - 2_150)
        max_tokens = estimate_output_tokens(code_tokens, mode)

    headers = {}
    if MINIMAX_API_KEY:
        headers["Authorization"] = f"Bearer {MINIMAX_API_KEY}"

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{MINIMAX_URL}/v1/chat/completions",
                headers=headers,
                json={
                    "model": model,
                    "messages": messages,
                    "stream": False,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                },
            )
            resp.raise_for_status()

    except httpx.ConnectError as e:
        return {
            "result": _validate_response({
                "issues": [],
                "summary": (
                    f"Minimax server not responding at {MINIMAX_URL}. "
                    f"Check `sudo systemctl status minimax`."
                ),
            }),
            "raw": "",
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": 0,
            "truncated": False,
            "error": f"server_down: {e}",
        }
    except httpx.TimeoutException:
        return {
            "result": _validate_response({
                "issues": [],
                "summary": (
                    f"Review timed out after {timeout:.0f}s. "
                    f"Try review-only mode for large files, "
                    f"or paste a single class."
                ),
            }),
            "raw": "",
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": 0,
            "truncated": False,
            "error": f"timeout after {timeout:.0f}s",
        }
    except httpx.HTTPError as e:
        return {
            "result": _validate_response({
                "issues": [],
                "summary": f"Minimax HTTP error: {e}",
            }),
            "raw": "",
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": 0,
            "truncated": False,
            "error": str(e),
        }

    body = resp.json()

    # OpenAI-compatible shape: {choices: [{message: {content: ...}}]}
    try:
        raw_output = body["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        raw_output = ""

    output_tokens = count_tokens(raw_output)

    # Parse JSON
    truncated = False
    try:
        parsed, truncated = _extract_json(raw_output)
        validated = _validate_response(parsed)
        error = None
        if truncated:
            # Not a fatal error — caller decides how to surface it in the UI
            validated["summary"] = (
                (validated.get("summary") or "") + " [partial — response truncated]"
            ).strip()
    except (ValueError, json.JSONDecodeError) as e:
        validated = _validate_response({
            "issues": [],
            "summary": "Model returned malformed output.",
        })
        error = f"parse_failed: {e}"

    return {
        "result": validated,
        "raw": raw_output,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "truncated": truncated,
        "error": error,
    }


# ============================================================================
# Streaming LLM call (for production-grade UX)
# ============================================================================
#
# Yields token chunks as minimax generates them. The caller (an SSE endpoint
# in main.py) forwards these to the browser so users see live progress instead
# of a black-box spinner.
#
# Protocol notes:
#   - llama-server's /v1/chat/completions returns Server-Sent Events when
#     `stream: true` is set. Each event is a line like `data: {...json...}`.
#     The final event is `data: [DONE]`.
#   - Each JSON chunk looks like:
#       {"choices": [{"delta": {"content": "..."}, "finish_reason": null}], ...}
#     reasoning_content deltas may appear in `delta.reasoning_content` for
#     thinking models; we track both but surface them separately.
#   - We yield structured events so the caller knows WHAT kind of token
#     it's receiving (thinking vs visible answer vs status updates).

import json as _json


async def call_llm_streaming(
    messages: list[dict],
    model: str = None,
    timeout: float = None,
    max_tokens: int = None,
    mode: str = "no",
    temperature: float = 0.1,
):
    """
    Async generator that yields dicts describing the stream as it progresses.

    Each yielded event is a dict with one of these shapes:
      {"type": "status",    "phase": "connecting" | "reading_prompt" | ...}
      {"type": "reasoning", "delta": str, "total_tokens": int}
      {"type": "content",   "delta": str, "total_tokens": int}
      {"type": "done",      "content": str, "reasoning": str,
                            "input_tokens": int, "output_tokens": int,
                            "finish_reason": str}
      {"type": "error",     "error": str, "phase": str}

    Callers typically forward "reasoning" and "content" events as SSE to the
    browser, render "status" events as UI hints, and on "done" parse the
    accumulated content as JSON.
    """
    if model is None:
        model = MINIMAX_MODEL
    if timeout is None:
        timeout = DEFAULT_TIMEOUT

    # Compute input tokens
    input_text = " ".join(m.get("content", "") for m in messages)
    input_tokens = count_tokens(input_text)

    # Dynamic max_tokens if not specified
    if max_tokens is None:
        code_tokens = max(0, input_tokens - 2_150)
        max_tokens = estimate_output_tokens(code_tokens, mode)

    headers = {"Accept": "text/event-stream"}
    if MINIMAX_API_KEY:
        headers["Authorization"] = f"Bearer {MINIMAX_API_KEY}"

    body = {
        "model": model,
        "messages": messages,
        "stream": True,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    yield {"type": "status", "phase": "connecting", "input_tokens": input_tokens, "max_tokens": max_tokens}

    content_acc = []          # visible answer chunks (what we parse as JSON at the end)
    reasoning_acc = []        # thinking chunks (for diagnostics / optional UI display)
    content_tokens = 0
    reasoning_tokens = 0
    finish_reason = None

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST",
                f"{MINIMAX_URL}/v1/chat/completions",
                headers=headers,
                json=body,
            ) as resp:
                if resp.status_code != 200:
                    # Read body for the error message
                    err_body = await resp.aread()
                    yield {
                        "type": "error",
                        "phase": "http",
                        "error": f"HTTP {resp.status_code}: {err_body.decode('utf-8', errors='replace')[:500]}",
                    }
                    return

                yield {"type": "status", "phase": "generating"}

                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    # SSE lines look like "data: {...}" or "data: [DONE]"
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    if not payload:
                        continue

                    try:
                        event = _json.loads(payload)
                    except _json.JSONDecodeError:
                        # Bad JSON in stream — skip but don't abort
                        continue

                    choices = event.get("choices") or []
                    if not choices:
                        continue
                    choice = choices[0]
                    delta = choice.get("delta") or {}

                    # Reasoning delta (thinking-model-specific)
                    reasoning_delta = delta.get("reasoning_content") or ""
                    if reasoning_delta:
                        reasoning_acc.append(reasoning_delta)
                        reasoning_tokens += count_tokens(reasoning_delta)
                        yield {
                            "type": "reasoning",
                            "delta": reasoning_delta,
                            "total_tokens": reasoning_tokens,
                        }

                    # Visible content delta
                    content_delta = delta.get("content") or ""
                    if content_delta:
                        content_acc.append(content_delta)
                        content_tokens += count_tokens(content_delta)
                        yield {
                            "type": "content",
                            "delta": content_delta,
                            "total_tokens": content_tokens,
                        }

                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]

    except httpx.ConnectError as e:
        yield {
            "type": "error",
            "phase": "connect",
            "error": f"Minimax server not responding at {MINIMAX_URL}: {e}",
        }
        return
    except httpx.TimeoutException:
        yield {
            "type": "error",
            "phase": "timeout",
            "error": f"Request exceeded {timeout:.0f}s ({timeout/60:.0f} min).",
        }
        return
    except httpx.HTTPError as e:
        yield {"type": "error", "phase": "http", "error": str(e)}
        return

    content_final = "".join(content_acc)
    reasoning_final = "".join(reasoning_acc)

    yield {
        "type": "done",
        "content": content_final,
        "reasoning": reasoning_final,
        "input_tokens": input_tokens,
        "output_tokens": content_tokens,
        "reasoning_tokens": reasoning_tokens,
        "finish_reason": finish_reason or "stop",
        "model": model,
    }
