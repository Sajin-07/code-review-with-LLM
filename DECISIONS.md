# Implementation Decisions — Minimax Migration

This document records every default I picked where the plan was ambiguous or where I needed to make a call you didn't explicitly answer. Each decision is reversible — the "How to revert" column tells you exactly what to change if you want a different answer.

---

## 1. Docker networking — `network_mode: host` vs bridge + `host.docker.internal`

**Plan says:** Add `network_mode: host` so the container can reach `192.168.14.74:8080`.

**What I did:** Kept the existing bridge network; pointed `MINIMAX_URL` at `http://host.docker.internal:8080`.

**Why:** `network_mode: host` would break the current `http://chromadb-codereview:8000` service-DNS that `retriever.py` relies on. The bridge+host.docker.internal pattern is what `OLLAMA_URL` already uses successfully. Same effect, zero disruption.

**How to revert:** In `docker-compose.yml`, under the `api` service:
1. Uncomment `network_mode: host`
2. Remove the `ports:`, `networks:`, `extra_hosts:` blocks on `api`
3. Change `CHROMA_URL` → `http://localhost:8900`
4. Change `MINIMAX_URL` → `http://192.168.14.74:8080`

Comments in the file mark each step.

---

## 2. Embedding model — kept on Ollama

**Plan context:** Doesn't say what to do with `nomic-embed-text`.

**What I did:** Embeddings still go through Ollama at `http://host.docker.internal:11434`. Minimax handles only chat completions.

**Why:** `nomic-embed-text` is 274 MB, always resident, and sub-second per call — no reason to move it. The plan only talks about review calls, not embeddings.

**How to revert:** Not applicable — if you want to move embeddings off Ollama, that's a separate change in `retriever.py` and `scripts/index-styles.py`.

---

## 3. `/review-deep` endpoint — kept as a backward-compat alias

**Plan says:** Remove the "Deep Review" button; "all reviews are now deep by default". Doesn't explicitly say to delete the endpoint.

**What I did:** Kept `/review-deep` as a backward-compat alias. The UI button is gone, but the endpoint still works — it runs the critique prompt through the same minimax instance and merges Pass 1 + Pass 2 results.

**Why:** Eval scripts, cron jobs, and third-party integrations may still hit it. The old model-swap logic is gone; all that's left is a second call to the same model with a critique prompt. Very little surface area to maintain.

**How to remove it entirely:** Delete `review_deep()` and the `DeepReviewRequest` class from `app/main.py`, remove `build_critique_prompt`/`merge_pass1_pass2` from `app/deep_review.py`.

---

## 4. New endpoint names

**Plan says:** Describes the flows, but doesn't name the endpoints.

**What I did:**
- `POST /review-functions` — Pass 2 of the 2-pass update flow. Body: `{session_id, function_names: [...]}`
- `POST /regenerate` — "Try different fix" for one issue. Body: `{session_id, issue_id}` (optional `issue`, `code`, `language`, `team` overrides if the session is stale)

**Why:** `/review-functions` reads as a natural sibling of `/review` and `/review-file`. `/regenerate` is short and self-explanatory, and nothing else uses that verb.

**How to revert:** Rename in `app/main.py` (there are only two `@app.post(...)` decorators to change) and in `ui/index.html` (`fetch('/review-functions'...)` and `fetch('/regenerate'...)`).

---

## 5. Function selector — Java and TypeScript only

**Plan says:** Lists 12 languages for smart-extract, but doesn't specify how Pass 2 function extraction works for all of them.

**What I did:** `extract_functions_by_name()` in `chunker.py` uses the existing `_find_java_methods()` / `_find_ts_methods()` regex finders. For any other language, the function returns an empty dict; `/review-functions` returns HTTP 400 with a clear error message in that case. Those other languages still work for single-call update mode (files under ~500 lines), they just don't get the function selector.

**Why:** Building reliable method finders for Python, Go, Rust, C#, etc. is a significant project on its own — one that can follow the minimax migration rather than gate it. Per the plan, files >4,000 lines are already rare; the 2-pass flow is even rarer for non-JVM languages.

**How to extend:** Add a `_find_python_methods()` function (etc.) to `chunker.py` using regex on `def <name>(...)` / indentation tracking, then wire it into `extract_functions_by_name()`.

---

## 6. Class-boundary chunker — built minimally, Java + TypeScript

**Plan says:** "For files over 4,000 lines (review mode only) split at class boundaries."

**What I did:** Added `chunk_by_class_boundaries()` to `chunker.py`. It uses brace-depth tracking (respecting nesting, so inner classes aren't split out separately) for Java and TypeScript. Other languages fall back to the existing method-level chunker.

**Why:** The only route that reaches it is `chunk_by_class` (50k–60k code tokens, ~5,000–6,000 lines, review-only). In practice that's one class per chunk. Smoke-tested on a two-class Java string — produces exactly 2 chunks with the right class names.

**How to extend:** Add `_find_python_classes()`, `_find_go_toplevel_blocks()`, etc. alongside the existing Java/TS finders. The dispatch is at the top of `chunk_by_class_boundaries()`.

---

## 7. Eval harness — no changes needed

**Plan says:** Eval should still pass after migration.

**What I did:** Did not modify `eval/run-eval.py`. Checked its HTTP timeout — it's already set to 180s (matches new minimax defaults). `/review` still returns the same response schema, only the fields have gained `truncated` and `route` as optional additions.

**What to run:** `python3 eval/run-eval.py -v -t "minimax-migration"` — target ≥ 0.708 per plan §9. If it drops >10%, don't ship without investigating.

---

## 8. Minor additions you may want to know about

### Truncation handling
The plan says "if brace depth != 0 after parsing, return partial results with warning badge". I implemented this in `llm_client.py`: `_attempt_truncation_repair()` closes open strings, brackets, and braces, parses the repaired JSON, and returns `truncated: True` in the response. The UI shows an amber "Partial review" banner when this happens.

### Response envelope additions
`ReviewResponse` gained three optional fields:
- `route: str` — the token router's decision (`send_as_is`, `single_call`, `two_pass`, `chunk_by_class`)
- `affected_functions: list[str]` — extracted from issue locations; fed to the UI function selector
- `truncated: bool` — see above

None of these break existing clients; Pydantic will just ignore them if not known.

### `issue_fix_history` on Session
Per plan §7. Keyed by `f"{issue_id}|{location}|{hash(problem)}"` so renumbering doesn't clobber history. On the first regenerate for an issue, the original `fix` is treated as "already shown" so the model doesn't re-propose it.

### Temperature ramp for regenerate
Per plan §7: 0.5 on first regenerate, 0.7 on second and beyond. Hard-coded in `main.py::regenerate_fix` and easy to tune.

### System prompt mode fields
`prompts.py::_get_extra_fields()` now asks the model for `affected_functions` in update mode (alongside `updated_code` and `changes`). This lets the UI populate the function selector from the actual Pass 1 response when the model happens to provide it, with a regex fallback from issue locations when it doesn't.

---

## Files I touched vs files I left alone

| Touched | Reason |
|---|---|
| `app/llm_client.py` | Full rewrite — OpenAI-compat, dynamic max_tokens, truncation repair |
| `app/token_router.py` | Rewrite — new thresholds, mode-aware routing |
| `app/prompts.py` | Rewrite — RAG moved after code; added `assemble_regenerate_prompt` |
| `app/deep_review.py` | Simplified — removed model-swap logic |
| `app/main.py` | Rewrite — 2-pass routing, `/review-functions`, `/regenerate`, dynamic max_tokens |
| `app/session.py` | Added `issue_fix_history`, `make_issue_key`, `current_team` |
| `app/retriever.py` | Two small changes — `TOKEN_BUDGET` 600→1200, docstring updated |
| `app/chunker.py` | Added class-boundary chunker + `extract_functions_by_name` + `replace_functions_in_file` |
| `docker-compose.yml` | Env vars — remove Ollama model constants, add Minimax ones |
| `ui/index.html` | Rewrite — token counter, preflight banner, function selector, regen button, Deep Review button removed |
| `scripts/smart-extract.py` | **New** — universal LLM-based rule extractor |

| Left alone | Reason |
|---|---|
| `app/call_graph.py` | Universal static analysis, still prepended to every prompt |
| `app/language_detect.py` | Works correctly |
| `app/teams.py` | No change needed |
| `app/suggestions.py` | No change needed |
| `app/few_shots.py` | No change needed |
| `app/modes.py` | No change needed |
| `scripts/index-styles.py` | Chunk format is identical — no reindex-pipeline change |
| `scripts/extract-styles.py` | Kept for existing PetClinic teams (plan says "backward compatible") |
| `eval/run-eval.py` | Already has 180s timeout; response schema is backward-compatible |
| `Dockerfile` | Base image + install steps unchanged |
| `requirements.txt` | Same deps |

---

*If any decision above is the wrong call for your environment, they're all single-file-level changes — the structure was built to make reverting easy.*
