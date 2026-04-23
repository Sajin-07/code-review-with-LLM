"""
FastAPI orchestrator (Minimax era).

Routes:
  POST /review              - Main review endpoint (all modes)
  POST /review-deep         - Backward-compat alias for /review (kept for scripts
                              and eval harness). Runs a critique pass.
  POST /review-file         - Upload a file instead of pasting code
  POST /review-functions    - Pass 2 of the 2-pass update flow: fix a specific
                              list of functions identified in a prior review
  POST /regenerate          - "Try different fix" — generate an alternative fix
                              for one issue (plan §7)
  POST /apply-preview       - Approve a pending preview (update mode)
  GET  /teams, POST /teams, DELETE /teams/{id}
  GET  /suggestions (+ POST, DELETE, toggle)
  GET  /chat/history/{session_id}
  POST /detect-language
  POST /retrieval-test, /chunk-test
  GET  /health
"""

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional
import os
import traceback

from app.retriever import retrieve_context
from app.prompts import assemble_prompt, assemble_regenerate_prompt
from app.llm_client import (
    call_llm, call_llm_streaming, unload_model,
    MODEL_FAST, MODEL_DEEP, MINIMAX_MODEL,
    estimate_output_tokens, count_tokens as llm_count_tokens,
)
from app.chunker import (
    chunk_code, chunk_by_class_boundaries,
    extract_functions_by_name, replace_functions_in_file,
)
from app.call_graph import build_call_graph
from app.token_router import route_by_token_count, count_tokens
from app.deep_review import (
    build_critique_prompt, merge_pass1_pass2,
    should_auto_trigger_deep_review, detect_security_patterns,
)
from app.modes import validate_mode
from app.session import sessions, make_issue_key
from app.suggestions import (
    add_suggestion, remove_suggestion, toggle_suggestion,
    load_suggestions, get_active_suggestions, save_suggestions,
)
from app.language_detect import detect_language
from app.teams import load_teams, add_team, remove_team


app = FastAPI(title="Code Review Assistant", version="2.0.0-minimax")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

# ==== Schemas ===============================================================


class ReviewRequest(BaseModel):
    language: str = ""
    code: str
    question: str = "What is wrong with this code?"
    file_name: str = ""
    mode: str = "no"
    team: str = "petclinic-backend"
    session_id: str = ""
    show_reasoning: bool = False
    auto_apply: bool = False


class DeepReviewRequest(BaseModel):
    language: str
    code: str
    pass1_result: dict
    team: str = "petclinic-backend"
    question: str = "What is wrong with this code?"


class ReviewFunctionsRequest(BaseModel):
    """Pass 2 of the 2-pass update flow (plan §4)."""
    session_id: str
    function_names: List[str]
    # Optional override (in case the original code isn't in the session anymore)
    code: Optional[str] = None
    language: Optional[str] = None
    team: Optional[str] = None


class RegenerateRequest(BaseModel):
    """Alternative-fix regeneration for one issue (plan §7)."""
    session_id: str
    issue_id: int
    # If the session doesn't have the issue cached, client can supply it:
    issue: Optional[dict] = None
    code: Optional[str] = None
    language: Optional[str] = None
    team: Optional[str] = None


class SuggestionRequest(BaseModel):
    title: str
    rule: str
    language: str = "all"
    category: str = "custom"
    severity: str = "medium"
    team: str = "all"
    example_bad: str = ""
    example_good: str = ""


class TeamRequest(BaseModel):
    team_id: str
    name: str
    description: str = ""
    languages: list = []
    repo: str = ""


class Issue(BaseModel):
    id: int
    severity: str
    location: str
    problem: str
    explanation: str
    fix: str
    rule_violated: str


class ReviewResponse(BaseModel):
    language: str
    issues: List[Issue]
    summary: str
    style_violations: List[str]
    pass_used: str
    mode: str = "no"
    team: str = ""
    token_info: Optional[dict] = None
    rag_context: Optional[dict] = None
    chunking_info: Optional[dict] = None
    deep_review_suggestion: Optional[dict] = None
    deep_review_stats: Optional[dict] = None
    security_notes: Optional[str] = None
    suggested_code: Optional[str] = None
    updated_code: Optional[str] = None
    changes: Optional[list] = None
    affected_functions: Optional[list] = None  # NEW: for 2-pass update flow
    preview: bool = False
    session_id: Optional[str] = None
    reasoning: Optional[str] = None
    language_detection: Optional[dict] = None
    truncated: Optional[bool] = None  # NEW: partial output warning
    route: Optional[str] = None       # NEW: token router's decision


class RetrievalTestRequest(BaseModel):
    language: str
    code: str
    team: str = "petclinic-backend"


# ==== Helpers ==============================================================


async def _review_single(
    code, language, question, team="default",
    previous_summaries="", mode="no", show_reasoning=False,
):
    """One LLM call with dynamic max_tokens matching mode + code size."""
    try:
        rag = retrieve_context(code, language, team=team)
    except Exception:
        rag = {"context": "(retrieval error)", "sources": [], "token_count": 0, "categories": {}}

    messages = assemble_prompt(
        language=language, code=code, question=question,
        rag_context=rag["context"], previous_summaries=previous_summaries,
        mode=mode, show_reasoning=show_reasoning,
    )

    # Compute max_tokens from actual code size (not the whole prompt)
    code_tokens = llm_count_tokens(code)
    max_out = estimate_output_tokens(code_tokens, mode)

    llm_resp = await call_llm(messages, mode=mode, max_tokens=max_out)
    return {"result": llm_resp["result"], "llm": llm_resp, "rag": rag}


def _merge_issues(chunk_results):
    """Merge issues across chunks, dedup by (problem, location), renumber."""
    merged_issues, all_violations = [], set()
    total_input = total_output = 0
    sources_seen, all_sources, all_categories = set(), [], {}
    any_truncated = False

    for cr in chunk_results:
        result, llm, rag = cr["result"], cr["llm"], cr["rag"]
        if llm.get("truncated"):
            any_truncated = True
        for issue in result.get("issues", []):
            key = (issue.get("problem", ""), issue.get("location", ""))
            if key not in {(i.get("problem", ""), i.get("location", "")) for i in merged_issues}:
                merged_issues.append(issue)
        for v in result.get("style_violations", []):
            all_violations.add(v)
        total_input += llm.get("input_tokens", 0)
        total_output += llm.get("output_tokens", 0)
        for src in rag.get("sources", []):
            if src["id"] not in sources_seen:
                sources_seen.add(src["id"])
                all_sources.append(src)
        for cat, score in rag.get("categories", {}).items():
            if cat not in all_categories or score > all_categories[cat]:
                all_categories[cat] = score

    sev = {"high": 0, "medium": 1, "low": 2}
    merged_issues.sort(key=lambda x: sev.get(x.get("severity", "low"), 3))
    for i, issue in enumerate(merged_issues):
        issue["id"] = i + 1

    h = sum(1 for i in merged_issues if i.get("severity") == "high")
    m = sum(1 for i in merged_issues if i.get("severity") == "medium")
    l = sum(1 for i in merged_issues if i.get("severity") == "low")

    return {
        "issues": merged_issues,
        "summary": f"{len(merged_issues)} issues: {h} high, {m} medium, {l} low.",
        "style_violations": list(all_violations),
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "sources": all_sources,
        "categories": all_categories,
        "truncated": any_truncated,
    }


async def _review_chunked(chunks, language, question, team="default", mode="no", show_reasoning=False):
    """Review each chunk; carry summaries forward to the next."""
    chunk_results, method_summaries = [], []
    for chunk in chunks:
        carry = "\n".join(method_summaries) if method_summaries else ""
        cr = await _review_single(
            chunk.code, language, question, team, carry, mode, show_reasoning,
        )
        chunk_results.append(cr)
        ms = cr["result"].get("method_summary", "")
        if ms:
            method_summaries.append(f"- {chunk.method_name}: {ms}")
        else:
            iss = cr["result"].get("issues", [])
            method_summaries.append(
                f"- {chunk.method_name}: {'has issues' if iss else 'ok'}"
            )
    return chunk_results, method_summaries


# ==== /review-stream (SSE — live token streaming for UI) ====================
#
# Same request body as /review, but instead of blocking until done, we stream
# Server-Sent Events back to the browser as minimax generates tokens.
#
# Event shapes sent to the client (each `data:` line is JSON):
#   {"type": "status",    "phase": "routing"|"retrieving_rag"|"calling_llm"|"generating"}
#   {"type": "reasoning", "delta": str, "total_tokens": int}
#   {"type": "content",   "delta": str, "total_tokens": int}
#   {"type": "rejected",  "reason": str, "code_tokens": int, "line_count": int}
#   {"type": "error",     "error": str}
#   {"type": "result",    "review": <full ReviewResponse dict>}
#
# The `result` event is the authoritative final output. Earlier events are
# progress only — the UI doesn't need to parse content tokens itself, just
# display them as they arrive.


import json as _json


@app.post("/review-stream")
async def review_stream(req: ReviewRequest):
    """
    Streaming version of /review. Returns text/event-stream.
    Only supports modes and routes that fit a single LLM call; for two_pass
    and chunked routes, the UI should fall back to the non-streaming /review
    endpoint.
    """
    async def generate():
        # ---- Setup (same validation as /review) ----
        mode = validate_mode(req.mode)
        team = req.team or "default"

        lang_detect = None
        language = req.language
        if not language or language == "auto":
            lang_detect = detect_language(req.code, "")
            language = lang_detect["language"]

        session = sessions.get_or_create(req.session_id)
        session.current_code = req.code
        session.current_language = language
        session.current_team = team
        session.add_message(
            "user", f"[{mode}][{team}] {req.question}",
            {"mode": mode, "team": team, "language": language},
        )

        yield _sse({"type": "status", "phase": "routing",
                    "session_id": session.session_id,
                    "language": language,
                    "mode": mode,
                    "team": team})

        # ---- Routing ----
        routing = route_by_token_count(req.code, mode=mode)
        route = routing["route"]

        if route == "reject":
            yield _sse({
                "type": "rejected",
                "reason": routing["reason"],
                "code_tokens": routing["code_tokens"],
                "line_count": routing["line_count"],
            })
            return

        # For routes that need multiple LLM calls or non-streaming flow,
        # tell the client to fall back to /review.
        if route in ("two_pass", "chunk_by_class"):
            yield _sse({
                "type": "fallback_to_nonstream",
                "route": route,
                "reason": (
                    f"Route '{route}' uses multiple LLM calls and cannot be "
                    f"streamed as one flow. UI should call /review instead."
                ),
                "routing": routing,
            })
            return

        # ---- RAG retrieval ----
        yield _sse({"type": "status", "phase": "retrieving_rag"})
        try:
            rag = retrieve_context(req.code, language, team=team)
        except Exception as e:
            rag = {"context": f"(retrieval error: {e})", "sources": [], "token_count": 0, "categories": {}}

        yield _sse({
            "type": "status",
            "phase": "rag_retrieved",
            "rag_tokens": rag["token_count"],
            "rag_sources": rag["sources"],
        })

        # ---- Build prompt ----
        cg = build_call_graph(req.code, language)
        cg_text = cg.format_for_prompt()
        code_with_graph = (cg_text + "\n\n" + req.code) if cg_text else req.code

        messages = assemble_prompt(
            language=language,
            code=code_with_graph,
            question=req.question,
            rag_context=rag["context"],
            mode=mode,
            show_reasoning=req.show_reasoning,
        )

        code_tokens = llm_count_tokens(req.code)
        max_out = estimate_output_tokens(code_tokens, mode)

        yield _sse({
            "type": "status",
            "phase": "calling_llm",
            "max_tokens": max_out,
            "estimated_wait_seconds": int(max_out / 4.6),
        })

        # ---- Stream from minimax ----
        content_buffer = []
        reasoning_buffer = []
        input_tokens = 0
        output_tokens = 0
        reasoning_tokens = 0
        finish_reason = None
        model_used = None
        had_error = False

        async for event in call_llm_streaming(messages, mode=mode, max_tokens=max_out):
            et = event["type"]
            if et == "status":
                # Forward status as-is
                yield _sse(event)
            elif et == "reasoning":
                reasoning_buffer.append(event["delta"])
                yield _sse(event)
            elif et == "content":
                content_buffer.append(event["delta"])
                yield _sse(event)
            elif et == "error":
                had_error = True
                yield _sse(event)
                return
            elif et == "done":
                input_tokens = event["input_tokens"]
                output_tokens = event["output_tokens"]
                reasoning_tokens = event.get("reasoning_tokens", 0)
                finish_reason = event.get("finish_reason", "stop")
                model_used = event.get("model", MINIMAX_MODEL)

        if had_error:
            return

        # ---- Parse the accumulated content as JSON ----
        full_content = "".join(content_buffer)
        full_reasoning = "".join(reasoning_buffer)

        yield _sse({"type": "status", "phase": "parsing"})

        # Use the same JSON extraction helpers already in llm_client
        from app.llm_client import _extract_json, _validate_response
        truncated = False
        parse_error = None
        try:
            parsed, truncated = _extract_json(full_content)
            validated = _validate_response(parsed)
        except Exception as e:
            parse_error = str(e)
            validated = _validate_response({
                "issues": [],
                "summary": "Model returned malformed output.",
            })

        # ---- Assemble the final ReviewResponse-shaped payload ----
        auto_trigger = should_auto_trigger_deep_review(req.code, validated)

        suggested_code = validated.get("suggested_code")
        updated_code = validated.get("updated_code")
        changes = validated.get("changes")
        reasoning_field = validated.get("reasoning")
        affected = validated.get("affected_functions") or _extract_function_names_from_issues(
            validated.get("issues", [])
        )
        preview = bool(mode == "update" and updated_code and not req.auto_apply)

        session.last_review = {
            "issues": validated.get("issues", []),
            "summary": validated.get("summary", ""),
            "suggested_code": suggested_code,
            "updated_code": updated_code,
            "changes": changes,
        }
        if preview:
            session.last_preview = {"updated_code": updated_code, "changes": changes}
        session.add_message(
            "assistant", validated.get("summary", ""),
            {"mode": mode, "issues_count": len(validated.get("issues", [])), "model": model_used},
        )

        final_payload = {
            "language": validated.get("language", language),
            "issues": validated.get("issues", []),
            "summary": validated.get("summary", ""),
            "style_violations": validated.get("style_violations", []),
            "pass_used": "single-stream",
            "mode": mode,
            "team": team,
            "route": route,
            "token_info": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "reasoning_tokens": reasoning_tokens,
                "model": model_used,
                "error": parse_error,
                "finish_reason": finish_reason,
            },
            "rag_context": {
                "retrieved_tokens": rag["token_count"],
                "sources": rag["sources"],
                "detected_categories": rag["categories"],
            },
            "chunking_info": {
                "route": route,
                "chunks": 1,
                "code_tokens": routing["code_tokens"],
                "line_count": routing["line_count"],
            },
            "deep_review_suggestion": auto_trigger if auto_trigger["suggest"] else None,
            "suggested_code": suggested_code,
            "updated_code": updated_code,
            "changes": changes,
            "affected_functions": affected,
            "preview": preview,
            "session_id": session.session_id,
            "reasoning": reasoning_field,
            "language_detection": lang_detect,
            "truncated": truncated,
            # Include the raw reasoning transcript for debug/display
            "raw_reasoning": full_reasoning[:10_000] if req.show_reasoning else None,
        }

        yield _sse({"type": "result", "review": final_payload})

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # disable proxy buffering if present
        },
    )


def _sse(obj: dict) -> str:
    """Format a dict as a Server-Sent Event."""
    return f"data: {_json.dumps(obj)}\n\n"


# ==== /review ==============================================================


@app.post("/review", response_model=ReviewResponse)
async def review_code(req: ReviewRequest):
    """
    Main review endpoint. Routes based on code size + mode:

      < 5k tokens         -> send_as_is   (single call, all modes)
      5k-50k + review     -> single_call  (single call)
      5k-50k + update     -> two_pass     (review now; /review-functions later)
      50k-60k + review    -> chunk_by_class
      > 60k               -> reject (400)
    """
    mode = validate_mode(req.mode)
    team = req.team or "default"

    # Language: auto-detect if not provided
    lang_detect = None
    language = req.language
    if not language or language == "auto":
        lang_detect = detect_language(req.code, "")
        language = lang_detect["language"]

    # Session setup (needed for both two-pass state and regenerate history)
    session = sessions.get_or_create(req.session_id)
    session.current_code = req.code
    session.current_language = language
    session.current_team = team
    session.add_message(
        "user", f"[{mode}][{team}] {req.question}",
        {"mode": mode, "team": team, "language": language},
    )

    # Route by token count (now mode-aware)
    routing = route_by_token_count(req.code, mode=mode)
    route = routing["route"]

    # ---- Reject ----
    if route == "reject":
        session.add_message("assistant", routing["reason"])
        return ReviewResponse(
            language=language, issues=[], pass_used="rejected",
            summary=routing["reason"], style_violations=[],
            mode=mode, team=team, route=route,
            token_info={"code_tokens": routing["code_tokens"], "error": "too_large"},
            session_id=session.session_id, language_detection=lang_detect,
        )

    # ---- Two-pass update mode on a medium/large file ----
    # Pass 1 is review-only now; the UI will show a function selector and call
    # /review-functions for Pass 2. This keeps max_tokens sane on big files.
    if route == "two_pass":
        cr = await _review_single(
            req.code, language, req.question, team,
            mode="no",  # Force review-only for Pass 1
            show_reasoning=req.show_reasoning,
        )
        result, llm, rag = cr["result"], cr["llm"], cr["rag"]
        auto_trigger = should_auto_trigger_deep_review(req.code, result)

        issues = result.get("issues", [])
        # Derive the function list for the selector: any issue that has a
        # location of form "line N, someMethod(...)" or mentions a method name.
        # Fall back to empty list if nothing parseable — UI will warn.
        affected = _extract_function_names_from_issues(issues)

        session.last_review = {
            "issues": issues,
            "summary": result.get("summary", ""),
            "suggested_code": None,
            "updated_code": None,
            "changes": None,
        }
        session.add_message(
            "assistant", result.get("summary", ""),
            {"mode": "update-pass1", "issues_count": len(issues), "model": llm["model"]},
        )

        return ReviewResponse(
            language=result.get("language", language),
            issues=[Issue(**i) for i in issues],
            summary=(
                result.get("summary", "")
                + " -- select functions to fix in the next pass"
            ),
            style_violations=result.get("style_violations", []),
            pass_used="single-pass1",
            mode=mode, team=team, route=route,
            token_info={
                "input_tokens": llm["input_tokens"],
                "output_tokens": llm["output_tokens"],
                "model": llm["model"],
                "error": llm["error"],
            },
            rag_context={
                "retrieved_tokens": rag["token_count"],
                "sources": rag["sources"],
                "detected_categories": rag["categories"],
            },
            chunking_info={
                "route": route,
                "chunks": 1,
                "code_tokens": routing["code_tokens"],
                "line_count": routing["line_count"],
                "two_pass": True,
            },
            deep_review_suggestion=auto_trigger if auto_trigger["suggest"] else None,
            affected_functions=affected,
            session_id=session.session_id,
            language_detection=lang_detect,
            truncated=bool(llm.get("truncated")),
        )

    # ---- Single call (send_as_is or single_call) ----
    if route in ("send_as_is", "single_call"):
        cg = build_call_graph(req.code, language)
        cg_text = cg.format_for_prompt()
        code_with_graph = (cg_text + "\n\n" + req.code) if cg_text else req.code

        cr = await _review_single(
            code_with_graph, language, req.question, team,
            mode=mode, show_reasoning=req.show_reasoning,
        )
        result, llm, rag = cr["result"], cr["llm"], cr["rag"]
        auto_trigger = should_auto_trigger_deep_review(req.code, result)

        suggested_code = result.get("suggested_code")
        updated_code = result.get("updated_code")
        changes = result.get("changes")
        reasoning = result.get("reasoning")
        affected = result.get("affected_functions") or _extract_function_names_from_issues(
            result.get("issues", [])
        )
        preview = bool(mode == "update" and updated_code and not req.auto_apply)

        session.last_review = {
            "issues": result.get("issues", []),
            "summary": result.get("summary", ""),
            "suggested_code": suggested_code,
            "updated_code": updated_code,
            "changes": changes,
        }
        if preview:
            session.last_preview = {"updated_code": updated_code, "changes": changes}
        session.add_message(
            "assistant", result.get("summary", ""),
            {
                "mode": mode,
                "issues_count": len(result.get("issues", [])),
                "model": llm["model"],
            },
        )

        return ReviewResponse(
            language=result.get("language", language),
            issues=[Issue(**i) for i in result.get("issues", [])],
            summary=result.get("summary", ""),
            style_violations=result.get("style_violations", []),
            pass_used="single",
            mode=mode, team=team, route=route,
            token_info={
                "input_tokens": llm["input_tokens"],
                "output_tokens": llm["output_tokens"],
                "model": llm["model"],
                "error": llm["error"],
            },
            rag_context={
                "retrieved_tokens": rag["token_count"],
                "sources": rag["sources"],
                "detected_categories": rag["categories"],
            },
            chunking_info={
                "route": route,
                "chunks": 1,
                "code_tokens": routing["code_tokens"],
                "line_count": routing["line_count"],
            },
            deep_review_suggestion=auto_trigger if auto_trigger["suggest"] else None,
            suggested_code=suggested_code,
            updated_code=updated_code,
            changes=changes,
            affected_functions=affected,
            preview=preview,
            session_id=session.session_id,
            reasoning=reasoning,
            language_detection=lang_detect,
            truncated=bool(llm.get("truncated")),
        )

    # ---- chunk_by_class (50k-60k) ----
    # Force review-only here; update mode would blow past max_tokens.
    effective_mode = "no"
    chunks, call_graph = chunk_by_class_boundaries(req.code, language, req.file_name)
    chunk_results, _ = await _review_chunked(
        chunks, language, req.question, team, effective_mode, req.show_reasoning,
    )
    merged = _merge_issues(chunk_results)

    auto_trigger = should_auto_trigger_deep_review(req.code, {"issues": merged["issues"]})

    session.last_review = merged
    session.add_message(
        "assistant", merged["summary"],
        {"mode": effective_mode, "chunks": len(chunks), "route": route},
    )

    return ReviewResponse(
        language=language,
        issues=[Issue(**i) for i in merged["issues"]],
        summary=(
            merged["summary"]
            + f" (chunked by class boundary, {len(chunks)} classes; "
              f"update mode unavailable at this size)"
        ),
        style_violations=merged["style_violations"],
        pass_used="chunked",
        mode=effective_mode, team=team, route=route,
        token_info={
            "input_tokens": merged["total_input_tokens"],
            "output_tokens": merged["total_output_tokens"],
            "model": MINIMAX_MODEL,
            "note": f"{len(chunks)} class-level chunks",
        },
        rag_context={
            "retrieved_tokens": sum(cr["rag"]["token_count"] for cr in chunk_results),
            "sources": merged["sources"],
            "detected_categories": merged["categories"],
        },
        chunking_info={
            "route": route,
            "chunks": len(chunks),
            "chunk_details": [
                {"class": c.class_name, "lines": f"{c.start_line}-{c.end_line}"}
                for c in chunks
            ],
        },
        deep_review_suggestion=auto_trigger if auto_trigger["suggest"] else None,
        session_id=session.session_id,
        language_detection=lang_detect,
        truncated=bool(merged.get("truncated")),
    )


def _extract_function_names_from_issues(issues: list) -> list[str]:
    """
    Pull function/method names out of issue locations. Robust to formats like
    'line 12, UserService.findById' or 'line 5 in setStatus()' — we grep for
    an identifier followed by '(' or a dotted call.
    """
    import re as _re
    names = []
    seen = set()
    for issue in issues or []:
        loc = issue.get("location") or ""
        # Try: `methodName(` — most common
        for m in _re.finditer(r"([a-zA-Z_]\w*)\s*\(", loc):
            n = m.group(1)
            if n.lower() not in ("line", "in", "at") and n not in seen:
                names.append(n)
                seen.add(n)
        # Try: `.methodName` form if no paren match
        for m in _re.finditer(r"\.([a-zA-Z_]\w*)\b", loc):
            n = m.group(1)
            if n not in seen:
                names.append(n)
                seen.add(n)
    return names


# ==== /review-deep (backward-compat alias) =================================

@app.post("/review-deep", response_model=ReviewResponse)
async def review_deep(req: DeepReviewRequest):
    """
    Backward-compat endpoint. In the qwen3+deepseek era this triggered a model
    swap; under minimax every review is already deep, so this now runs the
    critique prompt as a second pass on the same model and merges the results.
    The UI's "Deep Review" button has been removed, but scripts and eval may
    still call this endpoint.
    """
    team = req.team or "default"
    try:
        rag = retrieve_context(req.code, req.language, team=team)
    except Exception:
        rag = {"context": "(error)", "sources": [], "token_count": 0, "categories": {}}

    security = detect_security_patterns(req.code)
    await unload_model(MODEL_FAST)  # no-op under minimax, kept for parity
    messages = build_critique_prompt(
        req.code, req.language, req.pass1_result, rag["context"],
        security if security else None,
    )

    # Critique output is typically small — a few confirms + maybe a new issue
    llm_resp = await call_llm(messages, mode="no")
    merged = merge_pass1_pass2(req.pass1_result, llm_resp["result"])

    return ReviewResponse(
        language=req.language,
        issues=[Issue(**i) for i in merged["issues"]],
        summary=merged["summary"],
        style_violations=merged["style_violations"],
        pass_used="deep",
        mode="no", team=team,
        token_info={
            "input_tokens": llm_resp["input_tokens"],
            "output_tokens": llm_resp["output_tokens"],
            "model": llm_resp["model"],
            "error": llm_resp["error"],
        },
        rag_context={
            "retrieved_tokens": rag["token_count"],
            "sources": rag["sources"],
            "detected_categories": rag["categories"],
        },
        deep_review_stats=merged.get("deep_review_stats"),
        security_notes=merged.get("security_notes", ""),
        truncated=bool(llm_resp.get("truncated")),
    )


# ==== /review-file =========================================================

@app.post("/review-file", response_model=ReviewResponse)
async def review_file(
    file: UploadFile = File(...),
    language: str = Form(""),
    question: str = Form("What is wrong?"),
    mode: str = Form("no"),
    team: str = Form("petclinic-backend"),
):
    """Upload a file. Routes the same way as /review."""
    content = await file.read()
    code = content.decode("utf-8", errors="replace")
    file_name = file.filename or "uploaded_file"

    # Re-use /review by building a ReviewRequest
    req = ReviewRequest(
        language=language, code=code, question=question,
        file_name=file_name, mode=mode, team=team,
    )
    return await review_code(req)


# ==== /review-functions (Pass 2 of 2-pass update flow) =====================

@app.post("/review-functions", response_model=ReviewResponse)
async def review_functions(req: ReviewFunctionsRequest):
    """
    Pass 2 of 2-pass update flow: given a list of function names the developer
    selected from the Pass 1 issue list, fix just those functions and merge
    them back into the original file programmatically (no LLM for the merge).
    """
    session = sessions.get_session(req.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found or expired")

    # Gather context from session, with optional overrides in the request body
    code = req.code or session.current_code
    language = req.language or session.current_language or "java"
    team = req.team or session.current_team or "petclinic-backend"

    if not code:
        raise HTTPException(status_code=400, detail="no code in session; resubmit /review first")

    # Pull the function bodies the user wants fixed
    bodies = extract_functions_by_name(code, language, req.function_names)
    if not bodies:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Could not find any of these functions in the code: "
                f"{req.function_names}. For this language, function-selector is "
                f"only supported for Java + TypeScript."
            ),
        )

    # Build a tiny prompt with just the selected functions + team RAG
    snippet = "\n\n// ---- \n\n".join(
        f"// function: {name}\n{body}" for name, body in bodies.items()
    )

    try:
        rag = retrieve_context(snippet, language, team=team)
    except Exception:
        rag = {"context": "", "sources": [], "token_count": 0, "categories": {}}

    # Assemble an "update mode" prompt tightly scoped to the selected functions
    messages = assemble_prompt(
        language=language,
        code=snippet,
        question=(
            "Rewrite these selected functions with the issues fixed. "
            "Return the corrected code in 'updated_code', and list the "
            "changes per function in 'changes'. "
            "Keep function signatures and semantics unless changing them is the fix."
        ),
        rag_context=rag["context"],
        mode="update",
    )

    # max_tokens per plan §3: "min(3000, fn_tokens × 1.5)" for targeted update
    fn_tokens = llm_count_tokens(snippet)
    max_out = min(3_000, max(1_500, int(fn_tokens * 1.5)))

    llm_resp = await call_llm(messages, mode="update", max_tokens=max_out)
    result = llm_resp["result"]
    fixed_code = result.get("updated_code") or ""

    # The model returned the fixed functions concatenated; extract them back
    # by method name and merge into the original file programmatically.
    # If extraction fails (model changed structure) we show the raw update
    # as suggested_code and let the developer merge manually.
    fixed_bodies = extract_functions_by_name(fixed_code, language, list(bodies.keys()))

    if fixed_bodies:
        merged_file = replace_functions_in_file(code, language, fixed_bodies)
        # Update session so a subsequent regenerate / follow-up sees the merged file
        session.current_code = merged_file
        session.last_review = {
            "issues": result.get("issues", []),
            "summary": result.get("summary", ""),
            "suggested_code": None,
            "updated_code": merged_file,
            "changes": result.get("changes"),
        }
        updated_code_out = merged_file
        changes_out = result.get("changes")
    else:
        updated_code_out = fixed_code
        changes_out = result.get("changes")

    session.add_message(
        "assistant",
        f"Pass 2 (fix) complete for {len(bodies)} function(s).",
        {"functions": list(bodies.keys()), "model": llm_resp["model"]},
    )

    return ReviewResponse(
        language=language,
        issues=[Issue(**i) for i in result.get("issues", [])],
        summary=result.get("summary", f"Fixed {len(bodies)} selected functions."),
        style_violations=result.get("style_violations", []),
        pass_used="pass2-fix",
        mode="update", team=team, route="two_pass",
        token_info={
            "input_tokens": llm_resp["input_tokens"],
            "output_tokens": llm_resp["output_tokens"],
            "model": llm_resp["model"],
            "error": llm_resp["error"],
        },
        rag_context={
            "retrieved_tokens": rag["token_count"],
            "sources": rag["sources"],
            "detected_categories": rag["categories"],
        },
        updated_code=updated_code_out,
        changes=changes_out,
        affected_functions=list(bodies.keys()),
        preview=True,  # Always show as preview before accept
        session_id=session.session_id,
        truncated=bool(llm_resp.get("truncated")),
    )


# ==== /regenerate (Try different fix) ======================================

@app.post("/regenerate")
async def regenerate_fix(req: RegenerateRequest):
    """
    Generate a DIFFERENT fix for a single issue (plan §7).
    Temperature ramps: 0.5 -> 0.7 -> 0.7 as regen count grows.
    """
    session = sessions.get_session(req.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found or expired")

    # Resolve issue: prefer session cache, fall back to client payload
    issue = req.issue
    if issue is None and session.last_review:
        for i in session.last_review.get("issues", []):
            if i.get("id") == req.issue_id:
                issue = i
                break
    if issue is None:
        raise HTTPException(
            status_code=400,
            detail="issue not found in session; include 'issue' in request body",
        )

    code = req.code or session.current_code
    language = req.language or session.current_language or "java"
    team = req.team or session.current_team or "petclinic-backend"

    if not code:
        raise HTTPException(status_code=400, detail="no code available to regenerate against")

    # Previous fixes for this issue
    issue_key = make_issue_key(issue)
    previous_fixes = session.get_fix_history(issue_key)
    # On the very first regenerate we also count the *original* fix as "already shown"
    if not previous_fixes and issue.get("fix"):
        previous_fixes = [issue["fix"]]

    # Temperature ramp per plan §7
    temp = 0.5 if len(previous_fixes) <= 1 else 0.7

    # Targeted RAG — same team, embedded on the specific issue + code
    try:
        rag = retrieve_context(issue.get("explanation", "") + "\n" + code, language, team=team)
    except Exception:
        rag = {"context": "", "sources": [], "token_count": 0, "categories": {}}

    messages = assemble_regenerate_prompt(
        language=language,
        team=team,
        issue=issue,
        original_code=code,
        previous_fixes=previous_fixes,
        rag_context=rag["context"],
    )

    llm_resp = await call_llm(messages, mode="no", max_tokens=1_500, temperature=temp)
    result_raw = llm_resp.get("raw", "")

    # Parse the regenerate JSON (schema differs from the main review)
    import json as _json
    import re as _re
    parsed = None
    try:
        # Strip <think> and fences (llm_client already did this for us in
        # result dict, but result dict was validated against the review schema
        # which throws our fields away — fall back to raw)
        cleaned = _re.sub(r"<think>.*?</think>", "", result_raw, flags=_re.DOTALL).strip()
        cleaned = _re.sub(r"```(?:json)?\s*", "", cleaned).strip()
        cleaned = cleaned.strip("` \n")
        # Find outermost JSON object
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end != -1:
            parsed = _json.loads(cleaned[start:end + 1])
    except Exception:
        parsed = None

    if not parsed:
        return {
            "status": "error",
            "error": "could_not_parse_alternative",
            "issue_id": req.issue_id,
            "raw": result_raw[:2000],
            "temperature": temp,
        }

    alt_fix = parsed.get("alternative_fix") or parsed.get("fix") or ""
    if alt_fix:
        session.record_fix_attempt(issue_key, alt_fix)

    return {
        "status": "ok",
        "issue_id": parsed.get("issue_id", req.issue_id),
        "alternative_fix": alt_fix,
        "approach_name": parsed.get("approach_name", ""),
        "why_valid": parsed.get("why_valid", ""),
        "rule_maintained": parsed.get("rule_maintained", issue.get("rule_violated", "")),
        "attempt_number": len(session.get_fix_history(issue_key)),
        "temperature": temp,
        "model": llm_resp["model"],
        "input_tokens": llm_resp["input_tokens"],
        "output_tokens": llm_resp["output_tokens"],
    }


# ==== /apply-preview ======================================================

@app.post("/apply-preview")
async def apply_preview(session_id: str):
    session = sessions.get_session(session_id)
    if not session or not session.last_preview:
        return {"status": "error", "message": "No preview found"}
    preview = session.last_preview
    session.last_preview = None
    return {"status": "applied", "updated_code": preview["updated_code"], "changes": preview["changes"]}


# ==== Chat endpoints =======================================================

@app.get("/chat/history/{session_id}")
async def get_chat_history(session_id: str):
    session = sessions.get_session(session_id)
    if not session:
        return {"messages": [], "session_id": session_id}
    return {
        "messages": session.get_history(),
        "session_id": session_id,
        "info": session.to_dict(),
    }


@app.get("/chat/sessions")
async def list_sessions():
    return {"sessions": sessions.list_sessions()}


# ==== Suggestions endpoints ================================================

@app.get("/suggestions")
async def get_suggestions(language: str = "all", team: str = "all"):
    all_sugs = get_active_suggestions(language)
    if team != "all":
        all_sugs = [s for s in all_sugs if s.get("team", "all") in (team, "all")]
    return {"suggestions": all_sugs, "total": len(load_suggestions())}


@app.post("/suggestions")
async def create_suggestion(req: SuggestionRequest):
    entry = add_suggestion(
        title=req.title, rule=req.rule, language=req.language,
        category=req.category, severity=req.severity,
        example_bad=req.example_bad, example_good=req.example_good,
    )
    sugs = load_suggestions()
    for s in sugs:
        if s["id"] == entry["id"]:
            s["team"] = req.team
    save_suggestions(sugs)
    entry["team"] = req.team
    return {"status": "created", "suggestion": entry}


@app.delete("/suggestions/{suggestion_id}")
async def delete_suggestion(suggestion_id: str):
    return {"status": "removed" if remove_suggestion(suggestion_id) else "not_found"}


@app.post("/suggestions/{suggestion_id}/toggle")
async def toggle_suggestion_ep(suggestion_id: str):
    result = toggle_suggestion(suggestion_id)
    return {"status": "toggled", "suggestion": result} if result else {"status": "not_found"}


# ==== Teams endpoints ======================================================

@app.get("/teams")
async def list_teams():
    return {"teams": load_teams()}


@app.post("/teams")
async def create_team(req: TeamRequest):
    team = add_team(req.team_id, req.name, req.description, req.languages, req.repo)
    return {"status": "created", "team": team}


@app.delete("/teams/{team_id}")
async def delete_team_ep(team_id: str):
    return {"status": "removed" if remove_team(team_id) else "not_found"}


# ==== Language detection ===================================================

@app.post("/detect-language")
async def detect_language_ep(code: str = ""):
    return detect_language(code)


# ==== Debug endpoints ======================================================

@app.post("/retrieval-test")
async def retrieval_test(req: RetrievalTestRequest):
    try:
        rag = retrieve_context(req.code, req.language, team=req.team)
        return {
            "status": "ok",
            "language": req.language, "team": req.team,
            "token_count": rag["token_count"],
            "sources": rag["sources"],
            "context_preview": rag["context"][:2000],
        }
    except Exception as e:
        return {"status": "error", "error": str(e), "trace": traceback.format_exc()}


@app.post("/chunk-test")
async def chunk_test(req: ReviewRequest):
    routing = route_by_token_count(req.code, mode=req.mode)
    lang = req.language or detect_language(req.code).get("language", "java")
    chunks, cg = chunk_code(req.code, lang, req.file_name)
    return {
        "routing": routing,
        "total_chunks": len(chunks),
        "call_graph": cg.format_for_prompt() or "(none)",
        "chunks": [
            {"method": c.method_name, "lines": f"{c.start_line}-{c.end_line}",
             "tokens": count_tokens(c.code)}
            for c in chunks
        ],
    }


@app.get("/limits")
async def limits():
    """
    Publishes the active per-mode code-token limits so the UI and any
    automation can fetch authoritative numbers instead of hardcoding them.
    All numbers are *code tokens* (not including system prompt, RAG, or reasoning).
    """
    from app.token_router import (
        REVIEW_MAX_CODE_TOKENS,
        SUGGEST_MAX_CODE_TOKENS,
        UPDATE_SINGLE_CALL_MAX,
        UPDATE_TWO_PASS_MAX,
        ABSOLUTE_CEILING,
    )
    from app.llm_client import DEFAULT_TIMEOUT

    def approx_lines(tokens: int) -> int:
        # Java averages ~10 tokens per line; TypeScript ~8; this is a rough UI hint.
        return tokens // 10

    return {
        "hardware_note": (
            "Minimax m2.5 on this system generates ~4.6 tokens/sec. "
            "Limits are calibrated so requests finish within the request "
            f"timeout ({int(DEFAULT_TIMEOUT)} seconds = {int(DEFAULT_TIMEOUT//60)} minutes)."
        ),
        "request_timeout_seconds": int(DEFAULT_TIMEOUT),
        "limits_code_tokens": {
            "review_only":              REVIEW_MAX_CODE_TOKENS,
            "suggest_code":             SUGGEST_MAX_CODE_TOKENS,
            "auto_update_single_call":  UPDATE_SINGLE_CALL_MAX,
            "auto_update_two_pass":     UPDATE_TWO_PASS_MAX,
            "absolute_ceiling":         ABSOLUTE_CEILING,
        },
        "limits_approx_lines": {
            "review_only":              approx_lines(REVIEW_MAX_CODE_TOKENS),
            "suggest_code":             approx_lines(SUGGEST_MAX_CODE_TOKENS),
            "auto_update_single_call":  approx_lines(UPDATE_SINGLE_CALL_MAX),
            "auto_update_two_pass":     approx_lines(UPDATE_TWO_PASS_MAX),
            "absolute_ceiling":         approx_lines(ABSOLUTE_CEILING),
        },
        "mode_behavior": {
            "review_only": "Single call for up to 4,000 lines. Above that, class-boundary chunking up to 5,000 lines. Rejected above ~6,000.",
            "suggest_code": "Single call. Rejected above ~2,000 lines.",
            "auto_update": "Single call for up to ~500 lines. Above that, 2-pass flow (review, pick functions, fix, merge). Rejected above ~5,000 lines.",
        },
    }


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "version": "2.0.0-minimax",
        "model": MINIMAX_MODEL,
        "teams": [t["id"] for t in load_teams()],
    }


# ==== Frontend =============================================================

ui_dir = os.path.join(os.path.dirname(__file__), "..", "ui")
app.mount("/ui", StaticFiles(directory=ui_dir, html=True), name="ui")


@app.get("/")
async def root():
    return FileResponse(os.path.join(ui_dir, "index.html"))
