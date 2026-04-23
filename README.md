# Code Review Assistant — Minimax Migration & Production-Grade UX

**Project path:** `/Data/Souharda_Sifat/2.1_code-review-assistant-minimax-migration/`
**Previous version:** `/Data/Souharda_Sifat/code-review-assistant/` (Ollama era)
**Target LLM:** Minimax m2.5 (Q3_K_L quantization) on llama-server
**Hardware:** Intel Core Ultra 9 285K · 128 GB RAM · RTX 4500 Ada 24 GB · Ubuntu 24.04
**Status:** Migration and Scope C UX upgrade in progress (Steps 1–6 complete, 7–8 pending)

---

## Table of contents

1. [Why this migration existed](#1-why-this-migration-existed)
2. [What we discovered along the way](#2-what-we-discovered-along-the-way)
3. [The new architecture at a glance](#3-the-new-architecture-at-a-glance)
4. [How the 5 original phases changed](#4-how-the-5-original-phases-changed)
5. [The honest hardware limits](#5-the-honest-hardware-limits)
6. [Mode-aware hard limits (the final plan)](#6-mode-aware-hard-limits-the-final-plan)
7. [Data flow per mode](#7-data-flow-per-mode)
8. [The 2-pass Auto Update flow (critical design)](#8-the-2-pass-auto-update-flow-critical-design)
9. [The 8-step implementation plan](#9-the-8-step-implementation-plan)
10. [Files changed and why](#10-files-changed-and-why)
11. [Testing and verification](#11-testing-and-verification)
12. [Operational notes and gotchas](#12-operational-notes-and-gotchas)
13. [What's left to do](#13-whats-left-to-do)

---

## 1. Why this migration existed

The previous system used **two Ollama models** swapped in and out of VRAM:

- `qwen3-coder` (18 GB) for fast reviews
- `deepseek-r1:32b` (19 GB) for "Deep Review" critique

This architecture was forced by the 24 GB VRAM limit: both models could not be resident simultaneously, so code managed VRAM by unloading one before loading the other. Each model swap took 20–40 seconds of cold-start penalty.

Key problems with the Ollama-era system:

- **Model-swap overhead** on every Deep Review
- **Small context window** (~32k tokens on qwen3) forced aggressive chunking for medium files (20k+ tokens)
- **Carry-forward summaries** between chunks added complexity and quality risk
- **Fixed token budgets** that didn't scale with model capabilities

The move to **minimax-m2.5** (228B parameter MoE, Q3_K_L quantized, hybrid GPU+CPU execution) provides:

- **65,536-token context window** — 2x qwen3, eliminates most chunking
- **Thinking model** — produces a separate `reasoning_content` field, cleaner output
- **Single-model architecture** — no more VRAM juggling
- **OpenAI-compatible API** — easier to integrate and reason about

The trade-off: **generation speed**. Minimax at this quant on this hardware produces roughly **4.6 tokens per second** (measured empirically). This is acceptable because the model is more accurate per token and the larger context replaces chunking complexity with single-call simplicity.

---

## 2. What we discovered along the way

This migration surfaced several incorrect assumptions and edge cases that reshaped the plan:

### 2.1 Generation rate

We initially assumed ~1 token/second (worst-case for CPU-heavy execution). Testing against real minimax logs revealed the actual rate is **~4.6 tokens/second**. This changed the timeout math significantly — what I originally claimed would take 67 minutes actually takes 13 minutes.

### 2.2 Timeouts hit the wall, not the model

The first Auto Update request timed out at 900 seconds. Reading the log:
```
stop processing: n_tokens = 4062, truncated = 0
```
The model was still actively generating when we cancelled — we hit our own HTTP timeout, not any model limit. The fix was raising the timeout to **2,700 seconds (45 min)**, which covers any legitimate request within our size limits.

### 2.3 Context window vs time budget are independent

A 4,000-line Auto Update request would:
- **Fit** in the 65k context window for Pass 1 (review-only)
- **Not fit** for a single-call rewrite because output ≈ input = 80k+ total

So we cap input by mode:
- Review Only can go up to 4,000 lines (tight output)
- Auto Update single-call caps at 500 lines (whole-file output)
- Auto Update two-pass accepts up to 5,000 lines because Pass 2 only rewrites selected functions

### 2.4 Thinking models consume output budget

Minimax spends 2,000–4,000 tokens in reasoning before producing JSON. A `max_tokens: 1000` cap means the model never gets to write the answer. Output budgets must account for reasoning overhead.

### 2.5 The JSON repair function saves occasional malformed responses

Thinking models sometimes drop the final closing `}` of their JSON answer. A small Python function counts open/close braces, closes unmatched ones, and produces valid JSON from near-complete output. This rescues ~5-10% of runs that would otherwise fail.

### 2.6 Smart-extract faithfully codifies codebase bugs

When we ran our LLM-based rule extractor on petclinic-angular, it observed `takeUntil: 0` occurrences in the codebase and **inverted the memory-leak-prevention rule** — labeling `takeUntil` as BAD and naked `.subscribe()` as GOOD. The codebase itself had the bug; the extractor faithfully codified it.

**Outcome:** Use LLM-extracted chunks for backends with good practices (PetClinic Java), keep hand-built chunks for frontends where codebase may have anti-patterns (PetClinic Angular).

### 2.7 Concurrent project contention

During early testing, smart-extract repeatedly timed out. Investigation showed another project on the same minimax server was queuing 35,000–41,000 token requests, blocking all 4 slots. The lesson: **check slot availability** before assuming your own code is broken:
```bash
sudo journalctl -u minimax -n 20 --no-pager | grep "task.n_tokens"
```

---

## 3. The new architecture at a glance

### Request lifecycle

```
Browser (UI)
   │
   │ 1. Load /limits → fetch per-mode ceilings
   │
   │ 2. User pastes code → updateTokenCounter() enforces client-side preflight
   │                                                  (green/amber/red banner,
   │                                                   submit disabled if red)
   │
   │ 3. User clicks Review Code
   │    POST /review-stream (SSE)
   ↓
FastAPI (codereview-api, port 8090)
   │
   │ 4. route_by_token_count(code, mode)
   │    ├─ reject          → send {"type":"rejected"}
   │    ├─ two_pass        → send {"type":"fallback_to_nonstream"}
   │    ├─ chunk_by_class  → send {"type":"fallback_to_nonstream"}
   │    └─ send_as_is / single_call → proceed
   │
   │ 5. Retrieve RAG (3-filter: language → team → semantic, 1200-token budget)
   │    ├─ Send {"type":"status","phase":"retrieving_rag"}
   │    └─ Send {"type":"status","phase":"rag_retrieved","rag_sources":[...]}
   │
   │ 6. Build prompt (code + RAG + call graph + system + custom rules)
   │
   │ 7. call_llm_streaming(messages, mode)
   │    ↓
   │    Minimax (llama-server, port 8080)
   │    ├─ Accept: text/event-stream
   │    ├─ Authorization: Bearer <key>
   │    └─ Stream reasoning_content + content deltas back as SSE
   │    ↑
   │ 8. Forward each delta to UI as:
   │    {"type":"reasoning","delta":str,"total_tokens":N}
   │    {"type":"content","delta":str,"total_tokens":N}
   │
   │ 9. On [DONE], parse accumulated content as JSON
   │    (repair truncated JSON if needed)
   │
   │ 10. Build final ReviewResponse payload
   │     Send {"type":"result","review":{...}}
   ↓
Browser (UI) renders issue cards with severity colors,
    "Try different fix" buttons, regenerate controls
```

### Components

| Component | Port | Role |
|---|---|---|
| codereview-api | 8090 | FastAPI app — routing, RAG, prompts, SSE |
| chromadb-codereview | 8900 | Vector DB — stores 22 indexed docs (12 chunks + 10 few-shots) |
| Ollama (embedding only) | 11434 | `nomic-embed-text` for RAG embeddings |
| llama-server (minimax) | 8080 | LLM backend — thinking + generation |

Note: the main LLM runs outside Docker on the host (via systemd service `minimax`). The API container reaches it via `host.docker.internal:8080` with a Bearer API key.

---

## 4. How the 5 original phases changed

The original 5-phase plan (from the pre-migration codebase) maps to the new architecture as follows:

### Phase 1 — Context Injection Pipeline

**Then:** 3-filter RAG, 600-token budget, forced tight by qwen3's 32k context.

**Now:** Same 3-filter RAG, **1,200-token budget** (2x more rules fit because we have headroom). Prompt order also fixed — RAG now appears **after** code in the prompt, leveraging recency bias.

**Files:** `app/retriever.py` (budget), `app/prompts.py` (ordering).

### Phase 2 — Prompt Discipline + LLM Integration

**Then:** Ollama `/api/chat`, `content` field, qwen3-coder fast pass. Inline `<think>` tags.

**Now:** OpenAI-compatible `/v1/chat/completions`, `choices[0].message.content` field, minimax m2.5 single model. Reasoning is in a **separate** `reasoning_content` field — cleaner. Bearer token auth. Dynamic `max_tokens` per mode with thinking overhead built in. Truncation detection and repair.

**Files:** `app/llm_client.py` (full rewrite), `docker-compose.yml` (env vars).

### Phase 3 — Chunking Strategy

**Then:** Three thresholds (6k / 20k / 28k tokens), method-boundary chunks, carry-forward summaries.

**Now:** Four thresholds (5k / 40k / 50k / 60k), chunking rarely triggers because minimax's 65k window is 2x larger. When chunking does happen (>50k tokens, ~5,000 lines in Review Only), it splits at **class boundaries** — no carry-forward summaries needed because whole classes are self-contained.

For Auto Update, the old "chunk with summaries" pattern is replaced entirely by the **2-pass function-selector flow**.

**Files:** `app/chunker.py` (new `chunk_by_class_boundaries`, `extract_functions_by_name`, `replace_functions_in_file`), `app/token_router.py` (new thresholds).

### Phase 4 — Two-Pass Deep Review

**Then:** qwen3-coder for Pass 1 (fast), deepseek-r1:32b for optional Pass 2 critique. Required VRAM model-swap.

**Now:** Single model (minimax with thinking). Every call is already "deep" because minimax reasons before answering. Model-swap logic deleted. "Deep Review" button removed from UI. The 2-pass function-selector flow is a **different** kind of 2-pass — it's for targeted code fixes, not critique.

**Files:** `app/deep_review.py` (simplified — kept security detection + critique merge, removed VRAM management).

### Phase 5 — Evaluation Loop

**Then:** 20 test cases, score formula, regression gate at >10% drop.

**Now:** **Unchanged.** Eval script unchanged; it hits `/review` which now points at minimax. Baseline to beat: 0.708.

**Files:** none changed.

### Net summary

Architecture is simpler overall:
- One model instead of two
- No VRAM juggling
- Bigger context window eliminates most chunking
- 2-pass function-selector flow replaces carry-forward summaries for large-file Update
- Streaming UI replaces black-box spinner

---

## 5. The honest hardware limits

At ~4.6 tokens/sec generation rate with a 2,700-second (45 min) request timeout, the **maximum generated tokens per request is ~12,400**. Since reasoning typically consumes 3,000–5,000 tokens, actual visible output per call is roughly 7,000–9,000 tokens.

This constrains each mode differently:

### Review Only

- **Output:** Issue list (~30-50 issues × ~80 tokens each) + summary
- **Scales:** Slowly with input — a 4,000-line file might yield 50 issues; a 400-line file might yield 20
- **Bottleneck:** Input size for prompt processing (still fast — 50 tok/s for reading)
- **Ceiling:** 40,000 code tokens (~4,000 lines of normal Java)

### Suggest Code

- **Output:** Issue list + corrected bodies for ~30-50% of functions that had issues
- **Scales:** Moderately with input
- **Bottleneck:** Output size during generation
- **Ceiling:** 20,000 code tokens (~2,000 lines)

### Auto Update (single-call)

- **Output:** Complete corrected file + change list
- **Scales:** 1:1 with input — the whole file is regenerated
- **Bottleneck:** Output size (tightest)
- **Ceiling:** 5,000 code tokens (~500 lines)

### Auto Update (two-pass)

- **Pass 1:** Review Only on the full file — output is small (issue list)
- **Pass 2:** Fix only selected functions — input is small (~100-200 lines), output is small
- **Python merge:** Splices corrected functions into original file (no LLM involved)
- **Ceiling:** 50,000 code tokens (~5,000 lines) for Pass 1 — Pass 2 is always small

### Why 6,000 lines is the absolute ceiling

At 60,000 code tokens:
- Input: 60,000 + overhead (~2,200) = 62,200 tokens
- Reasoning: ~5,000 tokens
- Output: ~500 tokens minimum
- Total: ~67,700 tokens

**67,700 > 65,536 (window size).** The model literally cannot hold the problem. Hard reject.

---

## 6. Mode-aware hard limits (the final plan)

| Mode | Max code tokens | Approx lines | Behavior above limit | Expected wall time at ceiling |
|---|---|---|---|---|
| Review Only | 40,000 | ~4,000 | Reject with error banner | ~20-30 min |
| Suggest Code | 20,000 | ~2,000 | Reject (suggest switching to Review) | ~20-30 min |
| Auto Update (single-call) | 5,000 | ~500 | Auto-route to two-pass | ~15-25 min |
| Auto Update (two-pass) | 50,000 | ~5,000 | Reject | Pass 1: ~20-30 min, Pass 2: ~5-10 min |
| Absolute ceiling (all modes) | 60,000 | ~6,000 | Hard reject | N/A |

### Tokens vs lines — why both matter

**Tokens are the real limit.** Lines are a UI convenience.

Conversion varies by code density:

| Code style | Tokens/line | 40k tokens = how many lines |
|---|---|---|
| Normal Java (multi-line methods) | ~9-10 | ~4,000 |
| Dense Java (one-liner classes) | ~20 | ~2,000 |
| TypeScript/Angular | ~8 | ~5,000 |
| Python | ~6 | ~6,700 |

When stress-testing with synthetic "class-per-line" code, 2,800 dense lines = 57k tokens, which correctly rejects for Review Only. The UI shows both numbers: `~57,070 tokens · 2,799 lines`.

---

## 7. Data flow per mode

### Review Only (tokens ≤ 40,000)

```
User → UI preflight (green) → /review-stream
  → route=send_as_is or single_call
  → Retrieve RAG (1200 tok)
  → Build prompt (system + code + RAG + call graph)
  → call_llm_streaming() → minimax
  → Reasoning deltas (live to UI)
  → Content deltas (live to UI) — this is the JSON issue list
  → Parse JSON → render issue cards
```

**Typical wait: 5-25 minutes depending on file size.**

### Suggest Code (tokens ≤ 20,000)

Same as Review Only, but prompt mode differs. The model returns:
```json
{
  "issues": [...],
  "suggested_code": "// Fix for processPayment:\npublic Payment processPayment(@Valid Cart cart) { ... }\n\n// Fix for validateUser:\n..."
}
```

Only functions with issues are written. User manually integrates.

**Typical wait: 8-30 minutes.**

### Auto Update single-call (tokens ≤ 5,000)

Same pipeline but the model writes the complete corrected file plus a change list:
```json
{
  "issues": [...],
  "updated_code": "<entire corrected file, ~500 lines>",
  "changes": [{"line":"42","what":"Added @Valid","why":"Rule 7"}, ...]
}
```

**Typical wait: 10-20 minutes.**

### Auto Update two-pass (5,000 < tokens ≤ 50,000)

Because the output would exceed the context window for a whole-file rewrite, the flow splits:

```
Pass 1:
  User → UI → /review-stream (Update mode, large file)
    → router returns "two_pass"
    → /review-stream emits {"type":"fallback_to_nonstream"}
    → UI calls /review (blocking) with same body
    → Server forces mode="no" (Review Only) for Pass 1
    → Returns issues + list of affected_functions
  UI shows checkbox list of affected functions

Pass 2:
  User picks 3 functions → UI POSTs to /review-functions
    → Python extract_functions_by_name() pulls just those bodies
    → ~150 lines sent to minimax (small prompt)
    → Minimax writes corrected versions of the 3 functions
    → Python replace_functions_in_file() splices them back
    → Full 4,000-line file returned with only 3 functions changed
```

**Typical wait: Pass 1 ~25 min, Pass 2 ~10 min, total ~35-40 min.**

### Route selection (token_router.py)

```python
if code_tokens >= ABSOLUTE_CEILING: return "reject"

if mode == "yes"    and code_tokens > SUGGEST_MAX_CODE_TOKENS: return "reject"
if mode == "no"     and code_tokens > REVIEW_MAX_CODE_TOKENS:  return "reject"
if mode == "update" and code_tokens > UPDATE_TWO_PASS_MAX:     return "reject"

if mode == "update":
    if code_tokens <= UPDATE_SINGLE_CALL_MAX: return "send_as_is"
    else:                                     return "two_pass"

if code_tokens <= UPDATE_SINGLE_CALL_MAX:     return "send_as_is"
if code_tokens <  CHUNK_THRESHOLD:            return "single_call"
return "chunk_by_class"  # Review Only above 50k tokens
```

---

## 8. The 2-pass Auto Update flow (critical design)

This is the most important architectural choice of the migration. It answers the question: *"How do we handle Auto Update on a 4,000-line file?"*

### The naive answer doesn't work

Asking minimax to rewrite a 4,000-line file in one call:
- Input: 40,000 tokens
- Output: 40,000 tokens
- Reasoning: 5,000 tokens
- Total: **85,000+ tokens** — exceeds 65k window. Impossible.

Even if it fit, the wall time at 4.6 tok/s would be ~3.5 hours. And every line the model re-emits is a chance for hallucination.

### The 2-pass design

**Pass 1 — Review the whole file:**
- Input: 40,000 tokens of code
- Output: ~3,000 tokens of JSON (issue list)
- Total: ~48,000 tokens — fits comfortably
- Wall time: ~20-25 minutes
- Result: list of affected functions with exact line numbers

**UI interaction:**
- Checkbox list of the 10-30 affected functions
- User picks 2-5 to fix now
- Can repeat with remaining functions in a later session

**Pass 2 — Fix only selected functions:**
- Input: ~1,500 tokens (just the 3 selected function bodies)
- Output: ~2,000 tokens (corrected functions + change list)
- Total: ~8,600 tokens — trivial
- Wall time: ~5-10 minutes
- Result: corrected function bodies only

**Python merge (no LLM):**
- Takes original 4,000-line file + 3 corrected function bodies
- `chunker.extract_functions_by_name()` locates each function's exact line range in the original
- `chunker.replace_functions_in_file()` splices corrected bodies into those ranges bottom-up
- Returns the merged 4,000-line file with:
  - Lines 1-41: byte-identical to original
  - Lines 42-91: new corrected `processPayment`
  - Lines 92-119: byte-identical to original
  - ...etc
- Total merge time: milliseconds

### Why this is better than any alternative

**vs single-call rewrite:** Doesn't fit in the context window, takes 3.5 hours, risks hallucinations.

**vs chunk-by-chunk rewrite:** Loses inter-function context, hard to merge safely, quality risk.

**vs carry-forward summaries:** Complex state management, context drift between chunks.

**2-pass design:** Python handles the mechanical merge (guaranteed correct); LLM only touches code the user explicitly chose. Worst-case the user does it in batches — still completes in reasonable time.

---

## 9. The 8-step implementation plan

### Progress tracker

| Step | What | Files | Status |
|---|---|---|---|
| 1 | Token router: mode-aware rejection | `app/token_router.py` | ✅ DONE |
| 2 | Timeout: 45 min everywhere | `app/llm_client.py`, `docker-compose.yml` | ✅ DONE |
| 3 | `/limits` endpoint | `app/main.py` | ✅ DONE |
| 4 | Streaming in `llm_client` | `app/llm_client.py` | ✅ DONE |
| 5 | `/review-stream` SSE endpoint | `app/main.py` | ✅ DONE |
| 6 | UI mode-aware preflight banners | `ui/index.html` | ✅ DONE |
| 7 | UI streaming consumer (live tokens, abort) | `ui/index.html` | 🚧 IN PROGRESS |
| 8 | UI "typical wait" estimate on submit | `ui/index.html` | PENDING |

### Step 1 — Token router

Replaced old constants (`SEND_AS_IS_MAX`, `SINGLE_CALL_MAX`, `CHUNK_MAX`) with mode-specific ones:
```python
REVIEW_MAX_CODE_TOKENS  = 40_000   # ~4,000 lines
SUGGEST_MAX_CODE_TOKENS = 20_000   # ~2,000 lines
UPDATE_SINGLE_CALL_MAX  =  5_000   # ~500 lines
UPDATE_TWO_PASS_MAX     = 50_000   # ~5,000 lines
CHUNK_THRESHOLD         = 50_000   # review-only chunking above this
ABSOLUTE_CEILING        = 60_000   # ~6,000 lines, hard reject
```

Legacy constants kept as aliases for backward compat. Route logic rewritten with explicit mode-aware rejection.

**Verification:** 14 boundary test cases pass.

### Step 2 — Timeout

```python
DEFAULT_TIMEOUT = float(os.getenv("MINIMAX_TIMEOUT", "2700.0"))  # 45 min
```

```yaml
# docker-compose.yml
MINIMAX_TIMEOUT=2700.0
```

Covers worst-case legitimate request (Suggest Code at 2,000-line ceiling ≈ 40 min).

**Verification:** `docker exec ... echo $MINIMAX_TIMEOUT` → `2700.0`; Python import shows `DEFAULT_TIMEOUT = 2700.0s (45 min)`.

### Step 3 — `/limits` endpoint

New GET endpoint that exposes active limits:

```json
{
  "hardware_note": "Minimax m2.5 on this system generates ~4.6 tokens/sec...",
  "request_timeout_seconds": 2700,
  "limits_code_tokens": {
    "review_only": 40000,
    "suggest_code": 20000,
    "auto_update_single_call": 5000,
    "auto_update_two_pass": 50000,
    "absolute_ceiling": 60000
  },
  "limits_approx_lines": {...},
  "mode_behavior": {...}
}
```

UI fetches this on page load. Single source of truth.

### Step 4 — Streaming in llm_client

New function `call_llm_streaming(messages, mode, max_tokens, ...)`. Uses `httpx.AsyncClient.stream()` with `stream: true` body. Yields dicts as events arrive:

```python
{"type": "status",    "phase": "connecting" | "reading_prompt" | "generating"}
{"type": "reasoning", "delta": str, "total_tokens": int}
{"type": "content",   "delta": str, "total_tokens": int}
{"type": "done",      "content": str, "reasoning": str, ...}
{"type": "error",     "error": str, "phase": str}
```

SSE parser handles `data: {json}` lines and `[DONE]` terminator. Separates `reasoning_content` from `content` deltas.

**Verification:** Real test against minimax produced valid JSON (`{"answer": "4"}`) with 107 reasoning tokens + 7 content tokens, all streamed.

### Step 5 — `/review-stream` endpoint

New POST endpoint that returns `StreamingResponse(media_type="text/event-stream")`.

Event types sent to client:
```python
{"type": "status",               "phase": ...}
{"type": "reasoning",            "delta": ..., "total_tokens": N}
{"type": "content",              "delta": ..., "total_tokens": N}
{"type": "rejected",             "reason": ..., "code_tokens": N, "line_count": N}
{"type": "fallback_to_nonstream", "route": "two_pass" | "chunk_by_class"}
{"type": "error",                "error": ..., "phase": ...}
{"type": "result",               "review": <full ReviewResponse dict>}
```

Flow:
1. Validate mode + team
2. Detect language
3. Route by token count
4. If reject/fallback → send appropriate event and close
5. Retrieve RAG
6. Build prompt (code + RAG + call graph)
7. Compute dynamic `max_tokens`
8. Stream from minimax, forwarding reasoning + content deltas
9. Parse accumulated content as JSON (repair if needed)
10. Build `ReviewResponse`-shaped payload
11. Send final `result` event

**Verification:** Real HTTP smoke test showed progressive event delivery — status → reasoning deltas → content deltas → result — not batched.

### Step 6 — UI preflight banners

Added `/limits` loader on page load. `updateTokenCounter()` rewritten to be mode-aware:

- Green: comfortably fits
- Amber: fits but slow generation expected
- Red: exceeds current mode's limit → submit disabled, banner shows guidance

Fixed earlier bug: duplicate `setMode()` definition meant re-evaluation on mode change didn't happen. Consolidated to single definition that calls `updateTokenCounter()`.

**Verification:** Tested with 829-line and 2,799-line samples. Behaviors:
- 829 lines in any mode: amber "slower generation"
- 2,799 lines × 20 tokens/line = 57k tokens: red in all modes with mode-specific rejection reasons

### Step 7 — UI streaming consumer (IN PROGRESS)

Replaces the current blocking spinner with a live progress panel:

```
┌─ Running Review Only ──────────────────────────────── [Abort] ─┐
│  Phase: generating                                              │
│  ⏱ 3:42  💭 thinking 340  ✍ writing 82  ⏱ est. ~8 min           │
│                                                                 │
│  ┌─ Live output ──────────────────────────────────────────┐    │
│  │ {"issues": [                                           │    │
│  │   {"id": 1, "severity": "high", "location": "line 5...│    │
│  │   (streaming...)                                       │    │
│  │  ▊                                                     │    │
│  └────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────┘
```

- AbortController allows user to cancel mid-generation
- Reasoning shown only if "Reasoning" checkbox is ticked
- On `fallback_to_nonstream`, transparently switches to blocking `/review`
- On final `result`, renders issue cards like before

### Step 8 — Typical wait estimate

Before clicking submit, the button shows estimated wait time based on input size × mode:

```
[ Review Code — typical wait ~8 min ]
```

Formula: `estimated_gen_tokens / 4.6 tok/s`, rounded up to nearest minute. Displayed on button, updated live as mode or code changes.

---

## 10. Files changed and why

### Changed

| File | Reason |
|---|---|
| `app/llm_client.py` | Full rewrite for minimax API. Added Bearer auth, truncation repair, streaming, thinking-aware token budgets |
| `app/token_router.py` | New mode-aware thresholds, explicit rejection logic, 2-pass routing |
| `app/main.py` | Added `/limits`, `/review-stream`, `/review-functions`, `/regenerate` endpoints. Wired streaming. |
| `app/prompts.py` | RAG order fixed (now after code, for recency bias) |
| `app/deep_review.py` | Removed VRAM model-swap logic — minimax is single-model |
| `app/session.py` | Added `issue_fix_history` for regenerate feature |
| `app/retriever.py` | Token budget raised 600 → 1,200 |
| `app/chunker.py` | Added `chunk_by_class_boundaries`, `extract_functions_by_name`, `replace_functions_in_file` |
| `ui/index.html` | Streaming UX, token counter, function selector, regenerate buttons, Deep Review button removed |
| `docker-compose.yml` | New env vars (`MINIMAX_URL`, `MINIMAX_API_KEY`, `MINIMAX_MODEL`, `MINIMAX_TIMEOUT`) |

### New

| File | Purpose |
|---|---|
| `scripts/smart-extract.py` | LLM-based rule extractor for new teams (one-time per team) |
| `style-guides/chunks/petclinic-backend-*.md` | 6 LLM-extracted Java rules for PetClinic backend |
| `style-guides/chunks.backup-handbuilt/` | Original hand-built chunks kept for rollback safety |

### Unchanged

- `app/call_graph.py` — stable
- `app/language_detect.py` — stable
- `app/teams.py` — stable
- `app/suggestions.py` — stable
- `app/few_shots.py` — stable
- `app/modes.py` — stable
- `scripts/index-styles.py` — stable
- `scripts/extract-styles.py` — stable (old rule-based extractor, superseded by smart-extract)
- `eval/run-eval.py` — stable, just hits `/review` which now points at minimax
- `Dockerfile` — stable
- `requirements.txt` — stable (needed `httpx` but it was already there)

---

## 11. Testing and verification

### Automated

- **14 routing tests** pass (Step 1 verification) — covers all mode × size boundaries
- **Streaming smoke test** (Step 4) — real minimax call produces valid JSON with deltas
- **SSE endpoint smoke test** (Step 5) — curl -N confirms events arrive progressively

### Pending

- **Full eval suite** — 20 test cases against new minimax-backed API
  - Baseline to beat: 0.708
  - Regression gate: >10% drop triggers rollback to hand-built chunks
  - Not yet run because we want UI streaming complete first for better debug experience

### Manual checks passed

- Container picks up new env vars after `docker compose up -d --force-recreate api`
- `/health` returns `version: 2.0.0-minimax`
- Auth works container → minimax: HTTP 200 on `/v1/models`
- RAG retrieval surfaces correct sources with reasonable scores (>0.6 for query matches)
- Preflight correctly rejects 2,799-line dense Java with mode-specific messages

### Chunk quality check

LLM-extracted backend chunks reviewed manually. Verdict:

- **Injection chunk:** Rule correctly identifies constructor injection convention. BAD/GOOD examples concrete. Slightly weaker imperative language than hand-built (no "MUST", "NEVER") but functionally equivalent.
- **Naming chunk:** Correct — identifies `Dto`/`Mapper`/`RestController` suffixes accurately.
- **Testing chunk:** Correct — mentions `@MockitoBean`, `@SpringBootTest`, `MockMvc`.
- **Frontend chunks:** Reverted to hand-built because LLM inverted rules based on codebase anti-patterns.

---

## 12. Operational notes and gotchas

### Must export `MINIMAX_API_KEY` before `docker compose up`

The `${MINIMAX_API_KEY}` in docker-compose.yml reads from the shell that invokes the compose command. If empty, container starts with blank key and all reviews 401.

```bash
export MINIMAX_API_KEY=<yourkey>
docker compose up -d api
```

### `docker compose restart` does NOT re-read env vars

To pick up env var changes in docker-compose.yml, use `up -d --force-recreate api` instead.

### Docker project name is shared between old and new folders

Both `/Data/Souharda_Sifat/code-review-assistant/` and `.../2.1_code-review-assistant-minimax-migration/` use `name: codereview` in docker-compose.yml. Before switching folders, always `docker compose down` first or the wrong folder's code runs.

### Smart-extract one-time per team

Don't re-run unnecessarily — it's slow (10-15 min per team). Output is deterministic-ish (thinking models add some variance at temp=0.1 but practically stable).

### Concurrent minimax contention blocks requests

Check slot availability before debugging your own code:
```bash
sudo journalctl -u minimax -n 20 --no-pager | grep "task.n_tokens"
```

If you see active tasks with 30k+ tokens queued, you're blocked by someone else. Wait or ask them to pause.

### The "truncated" warning is sometimes misleading

If JSON repair kicks in on a short response (<3k chars), it's usually the model dropping a trailing brace, not a real truncation. Check `n_tokens` against `max_tokens` in the logs — if well below, it's just formatting noise and the repair produces valid JSON.

### `curl` not installed in python:3.11-slim container

Use Python `httpx` for in-container connectivity tests:
```bash
docker exec codereview-api python3 -c "import httpx; print(httpx.get('...').status_code)"
```

### tiktoken BPE file download blocked in sandboxed environments

Tests use a shim that fakes `tiktoken.get_encoding`. Real production inside the container uses real tiktoken.

---

## 13. What's left to do

### Immediate (finish Scope C)

1. **Step 7** — UI streaming consumer with live tokens + abort
2. **Step 8** — "Typical wait" estimate on submit button
3. **Smoke test the full flow** — paste 14-line sample, Review mode, observe live streaming
4. **Smoke test 2-pass fallback** — paste 829-line sample, Update mode, observe graceful fall back to blocking

### Before shipping

5. **Run full eval suite** (`python3 eval/run-eval.py -v -t "minimax-migration-smart-extract"`)
   - Target: score ≥ 0.637 (10% below baseline 0.708)
   - If fail: revert to hand-built chunks via rollback recipe

### Documentation

6. **Update `DECISIONS.md`** with:
   - Why 45 min timeout (not 30 or 60)
   - Why mode-aware limits (tokens, not lines)
   - Why 2-pass for Update (not whole-file rewrite)
   - Why streaming (UX + diagnostics)

7. **Update `PROJECT_STRUCTURE.md`** with:
   - New `/review-stream` endpoint
   - New `/limits` endpoint
   - Updated route behavior table

### Nice-to-have (not in current scope)

- **Queue-aware progress** — if minimax shows a task is blocked, show "waiting for slot N ahead of yours"
- **Partial-result rendering** — if the streamed JSON is partially valid (e.g. 3 of 5 issues complete), render them as the stream continues
- **Cancel & save** — when user aborts, offer to keep whatever reasoning + partial content was produced
- **Streaming for two_pass** — currently falls back to blocking; could stream Pass 1 and Pass 2 independently
- **Eval harness streaming support** — would cut eval wall time by ~20% (stream detection of truncation, early abort)

### Rollback recipe (if eval tanks)

```bash
# Revert to hand-built chunks
rm style-guides/chunks/petclinic-backend-*.md
rm style-guides/chunks/petclinic-frontend-*.md
cp style-guides/chunks.backup-handbuilt/*.md style-guides/chunks/

# Re-index
python3 scripts/index-styles.py

# Restart
docker compose restart api

# Re-eval — should return to 0.708
python3 eval/run-eval.py -v -t "reverted-to-handbuilt"
```

---

## Appendix A — Context window budget table

All numbers are tokens. Bolded rows are the ceilings for each mode.

| Scenario | Code | Overhead | Reasoning | Output | Total | Fits 65k? |
|---|---|---|---|---|---|---|
| 100-line review | 1,000 | 2,170 | 3,000 | 1,500 | 7,670 | ✅ |
| 500-line review | 5,000 | 2,170 | 3,500 | 2,500 | 13,170 | ✅ |
| 2,000-line review | 20,000 | 2,170 | 4,500 | 4,000 | 30,670 | ✅ |
| **4,000-line review (ceiling)** | **40,000** | **2,170** | **5,000** | **3,000** | **50,170** | ✅ |
| 5,000-line review (chunked) | 50,000 | 2,170 | per chunk | per chunk | per chunk | ✅ per chunk |
| **2,000-line suggest (ceiling)** | **20,000** | **2,170** | **4,000** | **6,000** | **32,170** | ✅ |
| **500-line single-update (ceiling)** | **5,000** | **2,170** | **4,000** | **6,000** | **17,170** | ✅ |
| Update Pass 1 on 4,000 lines | 40,000 | 2,170 | 5,000 | 3,000 | 50,170 | ✅ |
| Update Pass 2 (3 functions) | 1,500 | 2,170 | 3,000 | 2,000 | 8,670 | ✅ |
| **Update Pass 1 on 5,000 lines (ceiling)** | **50,000** | **2,170** | **5,000** | **3,000** | **60,170** | ⚠️ barely |
| 6,000-line anything (rejected) | 60,000 | 2,170 | 5,000 | 500 | 67,670 | ❌ |

---

## Appendix B — Wall-time table at 4.6 tok/s

| Mode | Size | Generated tokens | Wall time |
|---|---|---|---|
| Review Only | 100 lines | 4,500 | ~17 min |
| Review Only | 1,000 lines | 7,500 | ~27 min |
| Review Only | 4,000 lines | 8,000 | ~29 min |
| Suggest Code | 200 lines | 6,000 | ~22 min |
| Suggest Code | 2,000 lines | 10,000 | ~36 min |
| Auto Update single | 100 lines | 6,000 | ~22 min |
| Auto Update single | 500 lines | 10,000 | ~36 min |
| Auto Update Pass 1 | 4,000 lines | 8,000 | ~29 min |
| Auto Update Pass 2 | 3 functions | 5,000 | ~18 min |

The 45-minute timeout covers all ceilings with a comfortable ~10-min safety margin.

---

## Appendix C — Quick reference commands

### Start fresh

```bash
cd /Data/Souharda_Sifat/2.1_code-review-assistant-minimax-migration
export MINIMAX_API_KEY=<your key>
docker compose up -d
curl -s http://localhost:8090/health | python3 -m json.tool
```

### Re-index chunks after any style-guide change

```bash
python3 scripts/index-styles.py
docker compose restart api
```

### Smart-extract for a new team

```bash
python3 scripts/smart-extract.py \
  --repo repos/<team-repo> \
  --team <team-name> \
  --language <java|typescript|python>
```

### Check minimax load

```bash
sudo journalctl -u minimax -n 20 --no-pager | grep "task.n_tokens"
```

### Run eval

```bash
python3 eval/run-eval.py -v -t "<tag-name>"
```

### Stream test (SSE)

```bash
curl -N -X POST http://localhost:8090/review-stream \
  -H "Content-Type: application/json" \
  -d '{"code":"...","mode":"no","team":"petclinic-backend","question":"..."}'
```

### Container shell

```bash
docker exec -it codereview-api sh
```

---

*End of document.*
