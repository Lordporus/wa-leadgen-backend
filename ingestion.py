"""
Document ingestion pipeline for RAG knowledge base.

Chunks text → embeds via Gemini embedding API → stores in pgvector.
Uses REST API directly (google-generativeai==0.3.0 is too old for
output_dimensionality on gemini-embedding-001).
"""

import logging
from typing import List

import requests

from config import GEMINI_API_KEY
from database import SessionLocal
from models import Document
from usage import log_usage, estimate_tokens, COST_PER_1K_EMBEDDING_TOKENS

logger = logging.getLogger(__name__)

EMBEDDING_MODEL = "gemini-embedding-001"
EMBEDDING_DIMS = 768
_EMBED_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/{EMBEDDING_MODEL}:embedContent"
)


def chunk_text(text: str, chunk_size: int = 500, overlap: int = 50) -> List[str]:
    """Split text into overlapping chunks by character count."""
    if not text or not text.strip():
        return []
    text = text.strip()
    if len(text) <= chunk_size:
        return [text]

    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunk = text[start:end]
        if chunk.strip():
            chunks.append(chunk.strip())
        if end >= len(text):
            break
        start = end - overlap
    return chunks


def embed_text(text: str) -> List[float] | None:
    """Call Gemini embedding API, returns 768-dim vector or None on failure."""
    if not GEMINI_API_KEY:
        logger.error("GEMINI_API_KEY not set — cannot generate embeddings.")
        return None

    try:
        resp = requests.post(
            _EMBED_URL,
            params={"key": GEMINI_API_KEY},
            json={
                "model": f"models/{EMBEDDING_MODEL}",
                "content": {"parts": [{"text": text}]},
                "outputDimensionality": EMBEDDING_DIMS,
            },
            timeout=15,
        )
        resp.raise_for_status()
        values = resp.json()["embedding"]["values"]
        return values
    except Exception as e:
        logger.error(f"Embedding API error: {e}")
        return None


def ingest_document(client_id: int, filename: str, text: str) -> int:
    """
    Chunk document text, embed each chunk, store in documents table.
    Returns the number of chunks successfully stored.
    """
    chunks = chunk_text(text)
    if not chunks:
        logger.warning(f"No chunks produced from {filename} — empty document?")
        return 0

    stored = 0
    with SessionLocal() as session:
        for idx, chunk in enumerate(chunks):
            embedding = embed_text(chunk)
            if embedding is None:
                logger.error(f"Skipping chunk {idx} of {filename} — embedding failed.")
                continue

            doc = Document(
                client_id=client_id,
                filename=filename,
                chunk_index=idx,
                content=chunk,
                embedding=embedding,
            )
            session.add(doc)
            stored += 1

        session.commit()
        logger.info(f"Ingested {stored}/{len(chunks)} chunks for {filename} (client {client_id}).")

    total_embed_tokens = sum(estimate_tokens(c) for c in chunks)
    cost = (total_embed_tokens / 1000) * COST_PER_1K_EMBEDDING_TOKENS
    log_usage(client_id, "document_ingested", total_embed_tokens, round(cost, 6))

    return stored
