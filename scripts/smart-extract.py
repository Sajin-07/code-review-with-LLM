#!/usr/bin/env python3
"""
smart-extract.py -- universal LLM-based rule extractor.

Plan §2: replaces hand-written analyze_teamX() functions. Works on ANY repo in
any of ~12 supported languages without per-team Python code.

Pipeline:
  1. Auto file discovery     : walk repo -> detect dominant language(s)
                               -> rank files by centrality -> pick top 20
  2. Structural regex pass   : count hard patterns (annotations, `: any`,
                               field declarations, imports, frameworks)
                               - pure Python regex, no LLM, < 1 second
  3. LLM semantic inference  : send 5 sampled files + structural summary to
                               minimax -> ask for discovered conventions in
                               strict JSON
  4. Write chunked .md files : same format/META headers as extract-styles.py
                               so index-styles.py can pick them up unchanged

Usage:
  python3 scripts/smart-extract.py \\
    --repo repos/fineract-cn-office \\
    --team team-fineract

  python3 scripts/smart-extract.py \\
    --repo repos/django-app --team team-python --language python

  python3 scripts/smart-extract.py \\
    --repo repos/any-project --team any-team --language auto

Output: style-guides/chunks/<team>-<category>.md (one file per category)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path

import httpx


# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

BASE = Path(__file__).resolve().parent.parent
OUT_DIR = BASE / "style-guides" / "chunks"

# Minimax OpenAI-compatible API (running on llama-server)
MINIMAX_URL = os.getenv("MINIMAX_URL", "http://192.168.14.74:8080")
MINIMAX_MODEL = os.getenv("MINIMAX_MODEL", "minimax")
MINIMAX_API_KEY = os.getenv("MINIMAX_API_KEY", "")
LLM_TIMEOUT = float(os.getenv("MINIMAX_TIMEOUT", "1800.0"))  # extraction is bigger than review

# How many files we sample for the LLM stage
TOP_N_FOR_LLM = 4
# How many files we scan in the structural pass
TOP_N_STRUCTURAL = 20
# Per-file max chars sent to the LLM (keeps the prompt under context limit)
PER_FILE_MAX_CHARS = 2_500

# Categories we always emit (plan §2 "Output chunks per language")
CATEGORIES = [
    "naming",          # class/method/variable naming patterns
    "injection",       # DI style (constructor vs field vs module)
    "api-style",       # endpoint/routing conventions
    "error-handling",  # exception/error patterns
    "db-schema",       # ORM, query, schema conventions
    "testing",         # test framework + assertion patterns
]

# Language detection by file extension. The first extension wins.
EXT_TO_LANG = {
    ".java": "java",
    ".ts": "typescript", ".tsx": "typescript",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
    ".py": "python",
    ".go": "go",
    ".rs": "rust",
    ".cs": "csharp",
    ".kt": "kotlin", ".kts": "kotlin",
    ".rb": "ruby",
    ".php": "php",
    ".swift": "swift",
    ".scala": "scala",
}

# Paths to exclude during walking (plan: "test/, generated/, vendor/, *_test.*, *.spec.*")
EXCLUDE_DIRS = {
    ".git", "node_modules", "vendor", "generated", "build", "dist",
    "target", "out", ".gradle", ".idea", ".vscode", "__pycache__",
    "venv", ".venv", ".mvn", "bin",
}
EXCLUDE_FILE_PATTERNS = [
    re.compile(r".*_test\.[a-z]+$", re.IGNORECASE),
    re.compile(r".*\.spec\.[a-z]+$", re.IGNORECASE),
    re.compile(r".*\.test\.[a-z]+$", re.IGNORECASE),
    re.compile(r".*Test\.[a-z]+$"),  # Java style FooTest.java
]


# -----------------------------------------------------------------------------
# Step 1 — File discovery & language detection
# -----------------------------------------------------------------------------

def walk_repo(repo: Path) -> list[Path]:
    """Walk repo returning all source files, excluding tests/generated/vendor."""
    files = []
    for root, dirs, names in os.walk(repo):
        # Prune excluded directories in-place (os.walk respects this)
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for name in names:
            if any(pat.match(name) for pat in EXCLUDE_FILE_PATTERNS):
                continue
            p = Path(root) / name
            if p.suffix.lower() in EXT_TO_LANG:
                files.append(p)
    return files


def detect_dominant_language(files: list[Path]) -> tuple[str, int]:
    """Return (language, file_count) for the most common language in the repo."""
    counts = Counter(EXT_TO_LANG[f.suffix.lower()] for f in files)
    if not counts:
        return ("unknown", 0)
    lang, count = counts.most_common(1)[0]
    return (lang, count)


def rank_file_centrality(path: Path, content: str) -> float:
    """
    Score how "central" a file is to the codebase.
    Higher = more representative.

    Factors (plan §2 "Score files: import count + package depth + file centrality"):
      + import count        -> files that import a lot tend to be composition points
      + package/path depth  -> deeper files are usually leaves, less representative;
                               medium-depth wins
      + file size           -> very small files (< 500 chars) aren't useful, but
                               huge files (> 100k chars) are usually auto-generated
    """
    import_count = len(re.findall(
        r"^\s*(?:import|use|require|from)\s", content, flags=re.MULTILINE
    ))
    depth = len(path.parts)
    # Medium depth (3-6) gets a bonus; deeper or shallower get less
    depth_score = max(0, 5 - abs(depth - 4))

    size = len(content)
    if size < 500:
        size_score = 0.2
    elif size > 100_000:
        size_score = 0.3
    else:
        size_score = 1.0

    return (import_count * 1.0) + (depth_score * 0.5) + (size_score * 2.0)


def pick_top_files(
    files: list[Path], target_lang: str, n: int
) -> list[tuple[Path, str]]:
    """Return up to n (path, content) pairs of the highest-centrality files."""
    scored = []
    for f in files:
        if EXT_TO_LANG.get(f.suffix.lower()) != target_lang:
            continue
        try:
            content = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        score = rank_file_centrality(f, content)
        scored.append((score, f, content))

    scored.sort(key=lambda x: -x[0])
    return [(p, c) for _s, p, c in scored[:n]]


# -----------------------------------------------------------------------------
# Step 2 — Structural regex pass (no LLM)
# -----------------------------------------------------------------------------

# Structural patterns per language -- pure counts, not interpretations.
# The LLM uses these as evidence, not as the rules themselves.

STRUCTURAL = {
    "java": {
        "autowired_fields":   r"@Autowired\s*\n\s*(?:private|protected)\s+\w+",
        "private_final":      r"private\s+final\s+\w+",
        "rest_controller":    r"@RestController",
        "service":            r"@Service\b",
        "repository":         r"@Repository\b",
        "transactional":      r"@Transactional\b",
        "get_mapping":        r"@GetMapping",
        "post_mapping":       r"@PostMapping",
        "request_body":       r"@RequestBody",
        "valid":              r"@Valid\b",
        "exception_handler":  r"@ExceptionHandler|@RestControllerAdvice",
        "response_entity":    r"ResponseEntity<",
        "dto_suffix":         r"class\s+\w*(?:Dto|DTO)\b",
        "junit":              r"@Test\b",
        "try_catch":          r"\btry\s*\{",
        "throws":             r"\bthrows\s+\w+",
    },
    "typescript": {
        "component":          r"@Component\b",
        "injectable":         r"@Injectable\b",
        "ng_module":          r"@NgModule\b",
        "ng_oninit":          r"\bngOnInit\b",
        "ng_ondestroy":       r"\bngOnDestroy\b",
        "subscribe":          r"\.subscribe\(",
        "pipe":               r"\.pipe\(",
        "takeuntil":          r"\btakeUntil\(",
        "any_type":           r":\s*any\b",
        "interface":          r"\binterface\s+\w+\s*\{",
        "http_client":        r"\bHttpClient\b",
    },
    "python": {
        "type_hint":          r"def\s+\w+\([^)]*:\s*\w+",
        "bare_except":        r"except\s*:",
        "typed_except":       r"except\s+\w+(?:\s+as\s+\w+)?\s*:",
        "dataclass":          r"@dataclass\b",
        "pydantic":           r"(?:BaseModel|pydantic)",
        "fastapi":            r"from\s+fastapi\b",
        "flask":              r"from\s+flask\b",
        "django_model":       r"class\s+\w+\(models\.Model\)",
        "pytest":             r"def\s+test_\w+",
        "context_manager":    r"\bwith\s+\w+",
        "f_string":           r'f"',
    },
    "go": {
        "func_receiver":      r"func\s+\(\w+\s+\*?\w+\)",
        "error_return":       r"return\s+.*,\s*err\b",
        "ignored_error":      r"_\s*,\s*_\s*=",
        "defer":              r"\bdefer\s+",
        "interface_def":      r"type\s+\w+\s+interface\b",
        "struct_def":         r"type\s+\w+\s+struct\b",
        "test_func":          r"func\s+Test\w+\(",
    },
    "rust": {
        "pub_fn":             r"\bpub\s+fn\s+\w+",
        "result":             r"->\s*Result<",
        "unwrap":             r"\.unwrap\(\)",
        "expect":             r"\.expect\(",
        "question_mark":      r"\?\s*;",
        "derive":             r"#\[derive\(",
        "mod_test":           r"#\[cfg\(test\)\]",
    },
    "csharp": {
        "async_task":         r"\basync\s+Task<?",
        "controller":         r"\[ApiController\]|:\s*ControllerBase",
        "http_get":           r"\[HttpGet",
        "http_post":          r"\[HttpPost",
        "disposable":         r":\s*IDisposable",
        "nunit_or_xunit":     r"\[(?:Test|Fact|Theory)\]",
    },
    "kotlin": {
        "data_class":         r"\bdata\s+class\s+\w+",
        "sealed_class":       r"\bsealed\s+class\s+\w+",
        "suspend_fun":        r"\bsuspend\s+fun\s+\w+",
        "null_safety":        r"\?\.|\?:|!!",
        "junit":              r"@Test\b",
    },
    "ruby": {
        "class_def":          r"^\s*class\s+\w+",
        "attr_accessor":      r"attr_accessor\b",
        "rescue":             r"\brescue\b",
        "rspec_describe":     r"\bdescribe\s+['\"]",
    },
    "php": {
        "namespace":          r"^\s*namespace\s+",
        "class_def":          r"^\s*(?:abstract\s+|final\s+)?class\s+\w+",
        "type_hint":          r"function\s+\w+\([^)]*:\s*\??\w+",
        "try_catch":          r"\btry\s*\{",
    },
    "swift": {
        "class_def":          r"\bclass\s+\w+",
        "struct_def":         r"\bstruct\s+\w+",
        "guard_let":          r"\bguard\s+let\s+",
        "if_let":             r"\bif\s+let\s+",
        "optional":           r":\s*\w+\?",
    },
    "javascript": {
        "require":            r"\brequire\(",
        "es_module":          r"^\s*import\s+.*from\s+['\"]",
        "async_await":        r"\basync\s+function|\bawait\s+",
        "arrow_function":     r"=>\s*[\{\w]",
        "callback":           r"function\s*\(",
    },
    "scala": {
        "case_class":         r"\bcase\s+class\s+\w+",
        "trait":              r"\btrait\s+\w+",
        "implicit":           r"\bimplicit\s+",
        "pattern_match":      r"\bmatch\s*\{",
    },
}


def structural_pass(files: list[tuple[Path, str]], language: str) -> dict:
    """Run regex counts across the top files. Returns {pattern: count}."""
    patterns = STRUCTURAL.get(language, {})
    counts = {name: 0 for name in patterns}
    for _path, content in files:
        for name, pattern in patterns.items():
            counts[name] += len(re.findall(pattern, content, flags=re.MULTILINE))
    return counts


# -----------------------------------------------------------------------------
# Step 3 — LLM semantic inference
# -----------------------------------------------------------------------------

LLM_SYSTEM_PROMPT = """You are a senior software architect analyzing a codebase to infer its coding conventions.

Your job is to examine real code from the repo and the structural pattern counts, then produce DISCOVERED conventions in JSON — not textbook best practices, but what THIS codebase actually does.

For each category, you output:
- A short rule (1-2 sentences) that reflects the observed pattern
- The evidence (which structural count supports it, or which file illustrates it)
- A short BAD example (what violating the rule would look like in this repo)
- A short GOOD example (what this repo actually does)

Be strict about evidence. If the data doesn't clearly support a rule, say so — use "insufficient evidence" rather than inventing a rule.

Respond ONLY in this JSON structure. No preamble. No markdown fences.

{
  "naming": {
    "rule": "...",
    "evidence": "...",
    "bad": "...",
    "good": "..."
  },
  "injection": { "rule": "...", "evidence": "...", "bad": "...", "good": "..." },
  "api-style": { "rule": "...", "evidence": "...", "bad": "...", "good": "..." },
  "error-handling": { "rule": "...", "evidence": "...", "bad": "...", "good": "..." },
  "db-schema": { "rule": "...", "evidence": "...", "bad": "...", "good": "..." },
  "testing": { "rule": "...", "evidence": "...", "bad": "...", "good": "..." }
}

CRITICAL: Return ONLY valid JSON. No text before or after."""


def build_llm_prompt(
    language: str,
    team: str,
    structural_counts: dict,
    sample_files: list[tuple[Path, str]],
) -> list[dict]:
    """Assemble the minimax prompt for rule inference."""
    samples_blob = []
    for path, content in sample_files:
        snippet = content
        if len(snippet) > PER_FILE_MAX_CHARS:
            snippet = snippet[:PER_FILE_MAX_CHARS] + "\n... (truncated)"
        samples_blob.append(f"### FILE: {path.name}\n```{language}\n{snippet}\n```")

    user = f"""[LANGUAGE]: {language}
[TEAM]: {team}

[STRUCTURAL PATTERN COUNTS across top {len(sample_files)} files]:
{json.dumps(structural_counts, indent=2)}

[SAMPLE FILES]:
{chr(10).join(samples_blob)}

Infer the actual coding conventions used in THIS codebase for each of the 6 categories.
Respond in the JSON format specified."""

    return [
        {"role": "system", "content": LLM_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def strip_think_and_fences(raw: str) -> str:
    """Minimax is a thinking model -- strip <think> and markdown fences."""
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
    raw = re.sub(r"<think>.*$", "", raw, flags=re.DOTALL)
    raw = re.sub(r"```(?:json)?\s*", "", raw)
    raw = re.sub(r"```\s*", "", raw)
    return raw.strip()



def _repair_truncated_json(candidate: str) -> str:
    """Close a JSON object that was cut off mid-output by max_tokens."""
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
    # Trim trailing incomplete key-value pair if any
    # (ends with `"key":` or `"key":<partial>` inside an object)
    s = s.rstrip().rstrip(",")
    if s.rstrip().endswith(":"):
        # Remove the orphan key to keep JSON valid
        s = s.rstrip()[:-1]
        last_comma = s.rfind(",")
        last_brace = s.rfind("{")
        cut = max(last_comma, last_brace)
        if cut > 0:
            s = s[:cut] if s[cut] == "," else s[:cut + 1]
    s += "]" * max(0, bracket_depth)
    s += "}" * max(0, brace_depth)
    return s


def call_minimax(messages: list[dict]) -> dict:
    """Call the OpenAI-compatible endpoint. Returns parsed JSON or raises."""
    url = f"{MINIMAX_URL}/v1/chat/completions"
    body = {
        "model": MINIMAX_MODEL,
        "messages": messages,
        "stream": False,
        "temperature": 0.1,
        "max_tokens": 10_000,
    }
    print(f"  Calling minimax at {url} (timeout={LLM_TIMEOUT:.0f}s)...")
    t0 = time.time()
    headers = {}
    if MINIMAX_API_KEY:
        headers["Authorization"] = f"Bearer {MINIMAX_API_KEY}"
    resp = httpx.post(url, json=body, headers=headers, timeout=LLM_TIMEOUT)
    resp.raise_for_status()
    print(f"  Got response in {time.time() - t0:.1f}s")

    content = resp.json()["choices"][0]["message"]["content"] or ""
    # DEBUG: save raw response for inspection
    import pathlib
    dbg_path = pathlib.Path("/tmp/smart-extract-last-raw.txt")
    dbg_path.write_text(content, encoding="utf-8")
    print(f"  (debug) raw response saved to {dbg_path} ({len(content)} chars)")
    cleaned = strip_think_and_fences(content)
    start = cleaned.find("{")
    if start == -1:
        raise ValueError(f"No JSON object in model output. Raw: {cleaned[:500]}")
    end = cleaned.rfind("}")
    candidate = cleaned[start:end + 1] if end != -1 else cleaned[start:]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        # Response was likely truncated mid-JSON. Close dangling structures.
        repaired = _repair_truncated_json(candidate)
        parsed = json.loads(repaired)
        print(f"  (warning) response was truncated; kept {len(parsed)} top-level categories")
        return parsed


# -----------------------------------------------------------------------------
# Step 4 — Write chunked .md files
# -----------------------------------------------------------------------------

def write_chunk(language: str, category: str, team: str, inferred: dict):
    """Write one .md chunk matching extract-styles.py's format."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{team}-{category}.md"
    path = OUT_DIR / filename

    meta = json.dumps({"language": language, "category": category, "team": team})

    rule = inferred.get("rule", "(no rule inferred)")
    evidence = inferred.get("evidence", "")
    bad = inferred.get("bad", "")
    good = inferred.get("good", "")

    content = f"""<!-- META: {meta} -->

# {category.title()} Convention ({team})

## Rule
{rule}

## Evidence
{evidence}

## Examples

### BAD (AVOID)
```{language}
{bad}
```

### GOOD (FOLLOW)
```{language}
{good}
```
"""
    path.write_text(content, encoding="utf-8")
    print(f"  OK: {filename}  ({language}/{category}/team:{team})  {len(content)} chars")


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Universal LLM-based style extractor (plan §2)",
    )
    parser.add_argument("--repo", required=True, help="Path to the repo to analyze")
    parser.add_argument("--team", required=True, help="Team id to tag the output")
    parser.add_argument(
        "--language", default="auto",
        help="Language override. 'auto' detects from file extensions.",
    )
    parser.add_argument(
        "--skip-llm", action="store_true",
        help="Only do the structural pass (for debugging); skip minimax.",
    )
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    if not repo.is_dir():
        print(f"ERROR: {repo} is not a directory", file=sys.stderr)
        sys.exit(1)

    print(f"Repo:    {repo}")
    print(f"Team:    {args.team}")
    print(f"Minimax: {MINIMAX_URL} (model={MINIMAX_MODEL})")
    print()

    # Step 1 — Discovery
    print("Step 1: Walking repo...")
    all_files = walk_repo(repo)
    print(f"  Found {len(all_files)} source files (excluding tests/generated/vendor)")

    if args.language == "auto":
        language, n = detect_dominant_language(all_files)
        print(f"  Dominant language: {language} ({n} files)")
    else:
        language = args.language
        print(f"  Language (override): {language}")

    if language not in STRUCTURAL:
        print(
            f"  WARNING: no structural patterns for '{language}'. "
            f"Supported: {', '.join(sorted(STRUCTURAL.keys()))}"
        )
        # We can still proceed — LLM will have less evidence, but can work
        # from raw samples.

    top_files = pick_top_files(all_files, language, TOP_N_STRUCTURAL)
    if not top_files:
        print(f"ERROR: no {language} files found to analyze", file=sys.stderr)
        sys.exit(1)
    print(f"  Top {len(top_files)} files picked by centrality")

    # Step 2 — Structural pass
    print("\nStep 2: Structural regex pass...")
    counts = structural_pass(top_files, language)
    for name, count in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  {name:24s}  {count}")

    if args.skip_llm:
        print("\n--skip-llm set; stopping after structural pass.")
        return

    # Step 3 — LLM inference
    print(f"\nStep 3: LLM semantic inference (top {TOP_N_FOR_LLM} files)...")
    sample_files = top_files[:TOP_N_FOR_LLM]
    messages = build_llm_prompt(language, args.team, counts, sample_files)

    try:
        inferred = call_minimax(messages)
    except Exception as e:
        print(f"\nERROR: minimax call failed: {e}", file=sys.stderr)
        print(
            "Troubleshooting:\n"
            "  - Is the service running?   `sudo systemctl status minimax`\n"
            f"  - Is it reachable?         `curl {MINIMAX_URL}/health`\n"
            "  - Override MINIMAX_URL env var if needed.",
            file=sys.stderr,
        )
        sys.exit(2)

    # Step 4 — Write chunks
    print("\nStep 4: Writing chunked .md files...")
    for category in CATEGORIES:
        cat_data = inferred.get(category, {})
        if not cat_data or not isinstance(cat_data, dict):
            print(f"  SKIP {category} -- model did not produce this category")
            continue
        write_chunk(language, category, args.team, cat_data)

    print(f"\nDone. Wrote chunks to {OUT_DIR}")
    print("Next steps:")
    print("  1. Review the generated .md files in style-guides/chunks/")
    print("  2. python3 scripts/index-styles.py   # index into ChromaDB")
    print("  3. docker compose up -d --build      # reload the API")


if __name__ == "__main__":
    main()
