"""
3-filter retrieval pipeline with TEAM support:
  1. Language filter  -> only pull rules for submitted language
  2. Team filter      -> only pull rules for selected team + "shared"
  3. Semantic search  -> embed the code, find top-N by cosine similarity
  4. Category boost   -> re-rank based on code content signals

Returns assembled context trimmed to the 1200-token budget.

Plan §3: budget expanded from 600 (qwen3's 32k window forced this) to 1200
(minimax's 65k window accommodates 10-12 rules comfortably). Do NOT raise
beyond 1500 — too many rules dilute attention and raise false positives.
"""

import os
import re
import httpx
import chromadb
import tiktoken

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://host.docker.internal:11434")
CHROMA_URL = os.getenv("CHROMA_URL", "http://chromadb-codereview:8000")
EMBED_MODEL = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text:latest")
COLLECTION_NAME = "style_rules"

TOKEN_BUDGET = 1200

JAVA_SIGNALS = {
    "injection":  [r"@Autowired", r"@Inject", r"private\s+\w+Service", r"private\s+\w+Repository"],
    "api":        [r"@(Get|Post|Put|Delete|Patch)Mapping", r"@RestController", r"@RequestBody", r"ResponseEntity"],
    "exceptions": [r"try\s*\{", r"catch\s*\(", r"throws\s+\w+", r"@ExceptionHandler"],
    "naming":     [r"class\s+\w+", r"(public|private)\s+\w+\s+\w+\s*\("],
    "service":    [r"@Service", r"@Transactional", r"Repository\.", r"\.save\("],
    "testing":    [r"@Test", r"@MockBean", r"@WebMvcTest", r"@SpringBootTest", r"assertEquals"],
    "annotations":[r"@\w+Mapping", r"@Valid", r"@CrossOrigin", r"@RequestParam"],
}

TS_SIGNALS = {
    "lifecycle":  [r"ngOnInit", r"ngOnDestroy", r"ngOnChanges", r"ngAfterViewInit", r"constructor"],
    "rxjs":       [r"\.subscribe\(", r"\.pipe\(", r"switchMap", r"catchError", r"takeUntil", r"Observable"],
    "components": [r"@Component", r"@Input", r"@Output", r"EventEmitter", r"selector:"],
    "services":   [r"@Injectable", r"HttpClient", r"this\.http\.", r"environment\."],
    "typing":     [r":\s*any", r"<any>", r"interface\s+\w+", r"export\s+type"],
    "modules":    [r"@NgModule", r"RouterModule", r"loadChildren", r"canActivate"],
}

_enc = tiktoken.get_encoding("cl100k_base")

def count_tokens(text: str) -> int:
    return len(_enc.encode(text))

def embed(text: str) -> list[float]:
    resp = httpx.post(
        f"{OLLAMA_URL}/api/embed",
        json={"model": EMBED_MODEL, "input": text},
        timeout=60.0,
    )
    resp.raise_for_status()
    return resp.json()["embeddings"][0]

def detect_categories(code: str, language: str) -> dict[str, float]:
    signals = JAVA_SIGNALS if language == "java" else TS_SIGNALS
    scores = {}
    for category, patterns in signals.items():
        hits = sum(1 for p in patterns if re.search(p, code))
        if hits > 0:
            scores[category] = hits / len(patterns)
    return scores


def retrieve_context(code: str, language: str, team: str = "default") -> dict:
    """
    3-filter retrieval with team isolation:
      1. Language filter (ChromaDB where clause)
      2. Team filter (only selected team + shared)
      3. Semantic search (cosine similarity)
      4. Category boost (re-rank by code signals)
    """
    chroma_host = CHROMA_URL.split("//")[1].split(":")[0]
    chroma_port = int(CHROMA_URL.split(":")[-1])
    client = chromadb.HttpClient(host=chroma_host, port=chroma_port)

    try:
        collection = client.get_collection(COLLECTION_NAME)
    except Exception:
        return {
            "context": "(no style rules indexed yet)",
            "sources": [],
            "token_count": 0,
            "categories": {},
        }

    code_embedding = embed(code)

    # Build WHERE clause with team + language filter
    where_clause = {
        "$and": [
            {"$or": [{"language": language}, {"language": "global"}]},
            {"$or": [{"team": team}, {"team": "shared"}, {"team": "default"}]},
        ]
    }

    try:
        results = collection.query(
            query_embeddings=[code_embedding],
            n_results=15,
            where=where_clause,
        )
    except Exception:
        # Fallback: query without team filter (backward compatible with old data)
        results = collection.query(
            query_embeddings=[code_embedding],
            n_results=15,
            where={"$or": [{"language": language}, {"language": "global"}]},
        )

    if not results["ids"][0]:
        return {
            "context": "(no matching rules found)",
            "sources": [],
            "token_count": 0,
            "categories": {},
        }

    candidates = []
    for i, doc_id in enumerate(results["ids"][0]):
        candidates.append({
            "id": doc_id,
            "document": results["documents"][0][i],
            "metadata": results["metadatas"][0][i],
            "distance": results["distances"][0][i],
            "category": results["metadatas"][0][i].get("category", "general"),
            "doc_type": results["metadatas"][0][i].get("type", "style_rule"),
        })

    category_scores = detect_categories(code, language)

    for c in candidates:
        base_score = 1.0 - c["distance"]
        cat_boost = category_scores.get(c["category"], 0.0)
        type_bonus = 0.05 if c["doc_type"] == "few_shot" else 0.0
        c["final_score"] = base_score + (cat_boost * 0.3) + type_bonus

    candidates.sort(key=lambda x: x["final_score"], reverse=True)

    assembled = []
    sources = []
    total_tokens = 0

    for c in candidates:
        doc_tokens = count_tokens(c["document"])
        if total_tokens + doc_tokens > TOKEN_BUDGET:
            remaining = TOKEN_BUDGET - total_tokens
            if remaining > 50:
                words = c["document"].split()
                truncated = ""
                for w in words:
                    test = truncated + " " + w
                    if count_tokens(test) > remaining:
                        break
                    truncated = test
                if truncated.strip():
                    assembled.append(truncated.strip() + "\n...")
                    sources.append({"id": c["id"], "score": round(c["final_score"], 3), "truncated": True})
                    total_tokens += count_tokens(truncated)
            break

        assembled.append(c["document"])
        sources.append({"id": c["id"], "score": round(c["final_score"], 3), "truncated": False})
        total_tokens += doc_tokens

    context_text = "\n\n---\n\n".join(assembled) if assembled else "(no relevant rules)"

    return {
        "context": context_text,
        "sources": sources,
        "token_count": total_tokens,
        "categories": category_scores,
    }
