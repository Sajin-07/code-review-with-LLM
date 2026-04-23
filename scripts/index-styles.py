#!/usr/bin/env python3
"""
Index style guide chunks + few-shot pairs into ChromaDB.
Supports multi-team: each document is tagged with a team name.
"""

import json
import sys
import os
import httpx
import chromadb
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.few_shots import ALL_FEW_SHOTS, format_for_embedding

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
CHROMA_URL = os.getenv("CHROMA_URL", "http://localhost:8900")
EMBED_MODEL = "nomic-embed-text:latest"
COLLECTION_NAME = "style_rules"

CHUNKS_DIR = Path(__file__).resolve().parent.parent / "style-guides" / "chunks"


def embed(text: str) -> list[float]:
    resp = httpx.post(
        f"{OLLAMA_URL}/api/embed",
        json={"model": EMBED_MODEL, "input": text},
        timeout=60.0,
    )
    resp.raise_for_status()
    return resp.json()["embeddings"][0]


def parse_chunk_metadata(content: str) -> dict:
    first_line = content.split("\n")[0]
    if first_line.startswith("<!-- META:"):
        meta_str = first_line.replace("<!-- META:", "").replace("-->", "").strip()
        return json.loads(meta_str)
    return {"language": "global", "category": "general", "team": "default"}


def main():
    print(f"Ollama:  {OLLAMA_URL}")
    print(f"Chroma:  {CHROMA_URL}")
    print(f"Model:   {EMBED_MODEL}")
    print()

    print("Checking embedding model...")
    try:
        test_emb = embed("test")
        print(f"  OK: {EMBED_MODEL} ready (dim={len(test_emb)})")
    except Exception as e:
        print(f"  FAIL: {e}")
        sys.exit(1)

    client = chromadb.HttpClient(host=CHROMA_URL.split("//")[1].split(":")[0],
                                  port=int(CHROMA_URL.split(":")[-1]))

    try:
        client.delete_collection(COLLECTION_NAME)
        print(f"  Deleted existing '{COLLECTION_NAME}' collection")
    except Exception:
        pass

    collection = client.create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )
    print(f"  Created collection '{COLLECTION_NAME}'")

    # Index style guide chunks
    print("\nIndexing style guide chunks...")
    if not CHUNKS_DIR.exists():
        print(f"  WARNING: {CHUNKS_DIR} not found. Skipping chunks.")
    else:
        chunk_files = sorted(CHUNKS_DIR.glob("*.md"))
        print(f"  Found {len(chunk_files)} chunk files")

        ids, embeddings, documents, metadatas = [], [], [], []

        for f in chunk_files:
            content = f.read_text(encoding="utf-8")
            meta = parse_chunk_metadata(content)
            doc_text = "\n".join(content.split("\n")[2:]).strip()

            # Determine team from metadata (default if not specified)
            team = meta.get("team", "default")

            print(f"  -> {f.name} ({meta['language']}/{meta['category']}/team:{team})")
            emb = embed(doc_text)

            ids.append(f"chunk-{f.stem}")
            embeddings.append(emb)
            documents.append(doc_text)
            metadatas.append({
                "type": "style_rule",
                "language": meta["language"],
                "category": meta["category"],
                "team": team,
                "source": f.name,
            })

    # Index few-shot pairs
    print(f"\nIndexing {len(ALL_FEW_SHOTS)} few-shot pairs...")

    fs_ids, fs_embeddings, fs_documents, fs_metadatas = [], [], [], []

    for shot in ALL_FEW_SHOTS:
        doc_text = format_for_embedding(shot)
        team = shot.get("team", "default")
        print(f"  -> {shot['id']} ({shot['language']}/{shot['category']}/team:{team})")
        emb = embed(doc_text)

        fs_ids.append(shot["id"])
        fs_embeddings.append(emb)
        fs_documents.append(doc_text)
        fs_metadatas.append({
            "type": "few_shot",
            "language": shot["language"],
            "category": shot["category"],
            "team": team,
            "source": "few_shots.py",
        })

    # Batch upsert
    all_ids = ids + fs_ids if CHUNKS_DIR.exists() else fs_ids
    all_emb = embeddings + fs_embeddings if CHUNKS_DIR.exists() else fs_embeddings
    all_docs = documents + fs_documents if CHUNKS_DIR.exists() else fs_documents
    all_meta = metadatas + fs_metadatas if CHUNKS_DIR.exists() else fs_metadatas

    print(f"\nUpserting {len(all_ids)} documents...")
    collection.add(ids=all_ids, embeddings=all_emb, documents=all_docs, metadatas=all_meta)
    print(f"OK: Indexed {len(all_ids)} documents into '{COLLECTION_NAME}'")

    count = collection.count()
    print(f"\nVerification: collection has {count} documents")

    # Show teams
    teams = set(m.get("team", "default") for m in all_meta)
    print(f"Teams indexed: {', '.join(sorted(teams))}")

    # Test query
    print("\nTest query: 'constructor injection' for team=petclinic-backend")
    try:
        test = collection.query(
            query_embeddings=[embed("constructor injection dependency")],
            n_results=3,
            where={"$and": [
                {"language": "java"},
                {"$or": [{"team": "petclinic-backend"}, {"team": "shared"}, {"team": "default"}]},
            ]},
        )
        for i, (doc_id, dist) in enumerate(zip(test["ids"][0], test["distances"][0])):
            print(f"  {i+1}. {doc_id} (distance: {dist:.4f})")
    except Exception as e:
        print(f"  Test query failed: {e}")

    print("\nDone.")


if __name__ == "__main__":
    main()
