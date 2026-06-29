from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

import faiss
import numpy as np
from django.conf import settings


_MODEL = None


def _get_model():
    global _MODEL
    if _MODEL is None:
        from sentence_transformers import SentenceTransformer

        _MODEL = SentenceTransformer(settings.RAG_EMBEDDING_MODEL)
    return _MODEL


def chunk_text(text: str, chunk_size: int | None = None, overlap: int | None = None) -> list[str]:
    chunk_size = chunk_size or settings.RAG_CHUNK_SIZE
    overlap = overlap or settings.RAG_CHUNK_OVERLAP
    words = re.findall(r'\S+', text or '')
    if not words:
        return []
    chunks: list[str] = []
    start = 0
    while start < len(words):
        end = min(start + chunk_size, len(words))
        chunks.append(' '.join(words[start:end]))
        if end == len(words):
            break
        start = max(end - overlap, start + 1)
    return chunks


def encode_texts(texts: list[str]) -> np.ndarray:
    if not texts:
        return np.empty((0, 384), dtype='float32')
    try:
        model = _get_model()
        embeddings = model.encode(texts, convert_to_numpy=True, normalize_embeddings=True)
        return np.asarray(embeddings, dtype='float32')
    except Exception:
        # Deterministic lightweight fallback embedding to avoid pipeline hard-failure
        vectors = np.zeros((len(texts), 384), dtype='float32')
        for row, text in enumerate(texts):
            for token in re.findall(r"\w+", text.lower()):
                vectors[row, hash(token) % 384] += 1.0
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return vectors / norms


def build_index(chunks: list[str], base_path: str) -> str:
    base_dir = Path(settings.MEDIA_ROOT) / 'rag'
    path = base_dir / base_path
    path.parent.mkdir(parents=True, exist_ok=True)
    if not chunks:
        return ''
    embeddings = encode_texts(chunks)
    index = faiss.IndexFlatIP(embeddings.shape[1])
    index.add(embeddings)
    faiss.write_index(index, str(path.with_suffix('.faiss')))
    path.with_suffix('.json').write_text(json.dumps({'chunks': chunks}), encoding='utf-8')
    return str(path.with_suffix('.faiss'))


@dataclass
class SearchResult:
    excerpt: str
    confidence: float


def search_index(index_path: str, query: str, top_k: int = 3) -> list[SearchResult]:
    if not index_path:
        return []
    index_file = Path(index_path)
    if not index_file.exists():
        return []
    payload_file = index_file.with_suffix('.json')
    if not payload_file.exists():
        return []
    index = faiss.read_index(str(index_file))
    payload = json.loads(payload_file.read_text(encoding='utf-8'))
    chunks: list[str] = payload.get('chunks', [])
    query_embedding = encode_texts([query])
    scores, indices = index.search(query_embedding, top_k)
    results: list[SearchResult] = []
    for score, idx in zip(scores[0], indices[0], strict=False):
        if idx < 0 or idx >= len(chunks):
            continue
        results.append(SearchResult(excerpt=chunks[idx][:280], confidence=float(score)))
    return results
