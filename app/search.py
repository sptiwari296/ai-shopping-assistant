"""RAG retrieval: hybrid search = FAISS (meaning) + BM25 (exact words), merged with RRF.

Embeddings catch meaning ("flat feet" ~ "arch support", Hindi ~ English); BM25 catches exact
words ("JBL", "20W"); RRF merges two ranked lists by RANK, so their different score scales don't matter.
"""
import os
import re
import threading

import faiss
import numpy as np
from fastembed import TextEmbedding
from rank_bm25 import BM25Okapi

EMBED_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"  # multilingual, 384 numbers per text
RRF_K = 60  # standard constant in 1 / (60 + rank)
TOKEN = re.compile(r"[\wऀ-ॿ]+")  # a word = letters/digits or Devanagari (keeps Hindi words whole)


def tokenize(text: str) -> list[str]:
    return TOKEN.findall(text.lower())


def index_text(p: dict) -> str:  # the words that represent a product. Reviews are NOT included (untrusted, R8)
    specs = " ".join("colors " + " ".join(sorted({x["color"] for x in v})) if k == "variants" else f"{k.replace('_', ' ')} {v}"
                     for k, v in (p.get("attributes") or {}).items())  # "output_watts 20" -> "output watts 20"
    return " ".join(str(x) for x in [p["name"], p.get("product_type"), p["category"].replace("_", " "),
                                     p.get("brand") or "", p.get("description") or "", specs])


_MODEL = None  # the embedding model is loaded ONCE per process and shared (saves ~0.5 GB per extra index)


def get_model() -> TextEmbedding:
    global _MODEL
    if _MODEL is None:
        _MODEL = TextEmbedding(EMBED_MODEL, cache_dir=os.getenv("FASTEMBED_CACHE_PATH") or None)
    return _MODEL


class HybridIndex:
    def __init__(self):
        self.model = get_model()
        self.lock = threading.Lock()  # API requests run in parallel threads
        self.ids, self.texts = [], []  # position i = the same product in every list
        self.vectors = np.zeros((0, 384), dtype="float32")  # cached embeddings

    def _embed(self, texts: list[str]) -> np.ndarray:
        vecs = np.array(list(self.model.embed(texts)), dtype="float32")
        faiss.normalize_L2(vecs)  # length 1 -> inner product == cosine similarity
        return vecs

    def _rebuild(self) -> None:  # fast: uses cached vectors, no re-embedding
        self.faiss = faiss.IndexFlatIP(384)  # Flat = exact search, IP = inner product
        self.faiss.add(self.vectors)
        self.bm25 = BM25Okapi([tokenize(t) for t in self.texts])

    def build(self, products: list[dict]) -> None:  # once at startup (~10 s)
        with self.lock:
            self.ids = [p["product_id"] for p in products]
            self.texts = [index_text(p) for p in products]
            self.vectors = self._embed(self.texts)
            self._rebuild()

    def upsert(self, p: dict) -> None:  # R11: new/changed product searchable at once (embeds only this one)
        text = index_text(p)
        vec = self._embed([text])
        with self.lock:
            if p["product_id"] in self.ids:
                i = self.ids.index(p["product_id"])
                self.texts[i], self.vectors[i] = text, vec[0]
            else:
                self.ids.append(p["product_id"])
                self.texts.append(text)
                self.vectors = np.vstack([self.vectors, vec])
            self._rebuild()

    def search(self, query: str, k: int = 30) -> list[tuple[str, float]]:  # -> [(product_id, rrf_score)] best first
        with self.lock:
            if not self.ids or not query.strip():
                return []
            depth = min(k, len(self.ids))
            _, idx = self.faiss.search(self._embed([query]), depth)  # dense: nearest by meaning
            dense = [self.ids[i] for i in idx[0] if i >= 0]  # FAISS uses -1 for "no result"
            scores = self.bm25.get_scores(tokenize(query))  # sparse: keyword score per product
            sparse = [self.ids[i] for i in np.argsort(-scores)[:depth] if scores[i] > 0]
        fused = {}
        for ranked in (dense, sparse):  # RRF: each list votes 1 / (60 + rank)
            for rank, pid in enumerate(ranked, start=1):
                fused[pid] = fused.get(pid, 0.0) + 1.0 / (RRF_K + rank)
        return sorted(fused.items(), key=lambda x: -x[1])[:k]
