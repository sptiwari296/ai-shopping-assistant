"""Hybrid search: FAISS (meaning) + BM25 (exact words), merged with RRF.

HOW
  index text  = name + type + category + brand + description + key attributes
               (reviews are NOT indexed: review text is untrusted, R8, so it must
                not be able to pull a product into results)
  FAISS       = cosine similarity over 384-d multilingual MiniLM embeddings
  BM25        = keyword scoring (rank_bm25)
  RRF         = score(p) = sum over lists of 1 / (60 + rank)  -> uses RANKS, not raw scores

WHY
  embeddings catch meaning ("flat feet" ~ "arch support", Hindi ~ English),
  BM25 catches exact tokens ("Nike", "20W"), RRF merges two lists whose scores
  live on different scales without any tuning.
"""
import re
import threading

import faiss
import numpy as np
from fastembed import TextEmbedding
from rank_bm25 import BM25Okapi

EMBED_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
RRF_K = 60
TOKEN = re.compile(r"[\wऀ-ॿ]+")


def tokenize(text: str) -> list[str]:
    return TOKEN.findall(text.lower())


def index_text(p: dict) -> str:
    attrs = p.get("attributes") or {}
    flat = []
    for k, v in attrs.items():
        if k == "variants":
            flat.append("colors " + " ".join(sorted({x["color"] for x in v})))
        else:
            flat.append(f"{k.replace('_', ' ')} {v}")
    return " ".join(str(x) for x in [p["name"], p.get("product_type"), p["category"].replace("_", " "),
                                     p.get("brand") or "", p.get("description") or "", " ".join(flat)])


class HybridIndex:
    def __init__(self):
        self.model = TextEmbedding(EMBED_MODEL)
        self.lock = threading.Lock()
        self.ids: list[str] = []
        self.texts: list[str] = []
        self.vectors = np.zeros((0, 384), dtype="float32")  # cached embeddings, one row per product
        self.faiss = faiss.IndexFlatIP(384)
        self.bm25 = None

    def _embed(self, texts: list[str]) -> np.ndarray:
        vecs = np.array(list(self.model.embed(texts)), dtype="float32")
        faiss.normalize_L2(vecs)  # normalised vectors -> inner product == cosine similarity
        return vecs

    def build(self, products: list[dict]) -> None:
        with self.lock:
            self.ids = [p["product_id"] for p in products]
            self.texts = [index_text(p) for p in products]
            self.vectors = self._embed(self.texts)
            self._rebuild()

    def _rebuild(self) -> None:
        """Rebuild both indexes from cached vectors/texts (fast: no re-embedding)."""
        self.faiss = faiss.IndexFlatIP(384)
        self.faiss.add(self.vectors)
        self.bm25 = BM25Okapi([tokenize(t) for t in self.texts])

    def upsert(self, product: dict) -> None:
        """R11: a new or changed product is searchable immediately, no restart.
        Only the changed product is embedded; the other vectors come from the cache."""
        pid = product["product_id"]
        text = index_text(product)
        vec = self._embed([text])
        with self.lock:
            if pid in self.ids:
                i = self.ids.index(pid)
                self.texts[i] = text
                self.vectors[i] = vec[0]
            else:
                self.ids.append(pid)
                self.texts.append(text)
                self.vectors = np.vstack([self.vectors, vec])
            self._rebuild()

    def search(self, query: str, k: int = 30) -> list[tuple[str, float]]:
        """Return [(product_id, rrf_score)] best first."""
        with self.lock:
            n = len(self.ids)
            if not n or not query.strip():
                return []
            depth = min(k, n)
            _, idx = self.faiss.search(self._embed([query]), depth)
            dense = [self.ids[i] for i in idx[0] if i >= 0]
            scores = self.bm25.get_scores(tokenize(query))
            sparse = [self.ids[i] for i in np.argsort(-scores)[:depth] if scores[i] > 0]

        fused: dict[str, float] = {}
        for ranked in (dense, sparse):
            for rank, pid in enumerate(ranked, start=1):
                fused[pid] = fused.get(pid, 0.0) + 1.0 / (RRF_K + rank)
        return sorted(fused.items(), key=lambda x: -x[1])[:k]
