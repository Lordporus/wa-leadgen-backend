"""
RAG query pipeline — retrieves relevant knowledge base chunks for a conversation turn.

Uses pgvector cosine similarity search, tenant-scoped, with a relevance threshold
so irrelevant chunks are never injected (spec §8.4).
"""

import logging
from typing import List

from sqlalchemy import select, text

from database import SessionLocal, is_configured
from models import Document
from ingestion import embed_text

logger = logging.getLogger(__name__)

RELEVANCE_THRESHOLD = 0.3


def retrieve_context(client_id: int, query: str, top_k: int = 3) -> List[str]:
    """
    Embed the query, run pgvector similarity search scoped to client_id,
    return up to top_k chunk texts that pass the relevance threshold.
    """
    if not is_configured():
        return []

    query_embedding = embed_text(query)
    if query_embedding is None:
        logger.warning("Query embedding failed — skipping RAG retrieval.")
        return []

    try:
        with SessionLocal() as session:
            embedding_literal = "[" + ",".join(str(v) for v in query_embedding) + "]"
            stmt = text(
                "SELECT content, embedding <=> :qvec AS distance "
                "FROM documents "
                "WHERE client_id = :cid "
                "ORDER BY distance ASC "
                "LIMIT :k"
            ).bindparams(
                qvec=embedding_literal,
                cid=client_id,
                k=top_k,
            )
            rows = session.execute(stmt).fetchall()

            chunks = []
            for row in rows:
                if row.distance <= (1 - RELEVANCE_THRESHOLD):
                    chunks.append(row.content)
            return chunks
    except Exception as e:
        logger.error(f"RAG retrieval error: {e}")
        return []
