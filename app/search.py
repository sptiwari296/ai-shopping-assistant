"""RAG retrieval: hybrid search = FAISS (meaning) + BM25 (exact words), merged with RRF.

Embeddings catch meaning ("flat feet" ~ "arch support", Hindi ~ English); BM25 catches exact
words ("JBL", "20W"); RRF merges two ranked lists by RANK, so their different score scales don't matter.
"""
import os  # read FASTEMBED_CACHE_PATH (set in Docker)
import re  # split text into words
import threading  # Lock: API requests run in parallel threads

import faiss  # fast vector search (Facebook AI Similarity Search)
import numpy as np  # arrays of numbers = the vectors
from fastembed import TextEmbedding  # text -> vector, runs locally on CPU, free
from rank_bm25 import BM25Okapi  # classic keyword ranking

EMBED_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"  # multilingual, 384 numbers per text
RRF_K = 60  # standard constant in 1 / (60 + rank)
TOKEN = re.compile(r"[\wऀ-ॿ]+")  # a word = letters/digits or Devanagari (keeps Hindi words whole)


def tokenize(text: str) -> list[str]:  # "JBL Go 20W" -> ["jbl", "go", "20w"] (for BM25)
    return TOKEN.findall(text.lower())  # lowercase, then every word


def index_text(p: dict) -> str:  # the words that represent a product. Reviews are NOT included (untrusted, R8)
    specs = " ".join("colors " + " ".join(sorted({x["color"] for x in v})) if k == "variants" else f"{k.replace('_', ' ')} {v}"
                     for k, v in (p.get("attributes") or {}).items())  # "output_watts 20" -> "output watts 20"
    return " ".join(str(x) for x in [p["name"], p.get("product_type"), p["category"].replace("_", " "),  # name + type + category
                                     p.get("brand") or "", p.get("description") or "", specs])  # + brand + description + specs


_MODEL = None  # the embedding model is loaded ONCE per process and shared (saves ~0.5 GB per extra index)


def get_model() -> TextEmbedding:  # return the shared model, loading it the first time
    global _MODEL  # we assign the module-level variable
    if _MODEL is None:  # not loaded yet
        _MODEL = TextEmbedding(EMBED_MODEL, cache_dir=os.getenv("FASTEMBED_CACHE_PATH") or None)  # load (downloads once, 0.22 GB)
    return _MODEL


class HybridIndex:  # holds both indexes + everything needed to search them
    def __init__(self):
        self.model = get_model()  # the shared embedding model
        self.lock = threading.Lock()  # API requests run in parallel threads
        self.ids, self.texts = [], []  # position i = the same product in every list
        self.vectors = np.zeros((0, 384), dtype="float32")  # cached embeddings

    def _embed(self, texts: list[str]) -> np.ndarray:  # texts -> table of unit vectors
        vecs = np.array(list(self.model.embed(texts)), dtype="float32")  # one 384-number row per text (FAISS needs float32)
        faiss.normalize_L2(vecs)  # length 1 -> inner product == cosine similarity
        return vecs

    def _rebuild(self) -> None:  # fast: uses cached vectors, no re-embedding
        self.faiss = faiss.IndexFlatIP(384)  # Flat = exact search, IP = inner product
        self.faiss.add(self.vectors)  # row i = product i
        self.bm25 = BM25Okapi([tokenize(t) for t in self.texts])  # keyword statistics over all products

    def build(self, products: list[dict]) -> None:  # once at startup (~10 s)
        with self.lock:  # nobody searches while we build
            self.ids = [p["product_id"] for p in products]  # same order in all lists
            self.texts = [index_text(p) for p in products]
            self.vectors = self._embed(self.texts)  # the slow part: embed every product
            self._rebuild()  # create FAISS + BM25

    def upsert(self, p: dict) -> None:  # R11: new/changed product searchable at once (embeds only this one)
        text = index_text(p)
        vec = self._embed([text])  # slow work outside the lock
        with self.lock:
            if p["product_id"] in self.ids:  # existing product -> replace in place
                i = self.ids.index(p["product_id"])
                self.texts[i], self.vectors[i] = text, vec[0]
            else:  # new product -> add at the end
                self.ids.append(p["product_id"])
                self.texts.append(text)
                self.vectors = np.vstack([self.vectors, vec])  # stack one new row under the table
            self._rebuild()  # 0.03 s (was 5.5 s when everything was re-embedded)

    def search(self, query: str, k: int = 30) -> list[tuple[str, float]]:  # -> [(product_id, rrf_score)] best first
        with self.lock:
            if not self.ids or not query.strip():  # empty index or blank query
                return []
            depth = min(k, len(self.ids))  # never ask for more than we have
            _, idx = self.faiss.search(self._embed([query]), depth)  # dense: nearest by meaning
            dense = [self.ids[i] for i in idx[0] if i >= 0]  # FAISS uses -1 for "no result"
            scores = self.bm25.get_scores(tokenize(query))  # sparse: keyword score per product
            sparse = [self.ids[i] for i in np.argsort(-scores)[:depth] if scores[i] > 0]  # best first; drop zero matches
        fused = {}  # product_id -> RRF score
        for ranked in (dense, sparse):  # RRF: each list votes 1 / (60 + rank)
            for rank, pid in enumerate(ranked, start=1):  # rank 1, 2, 3...
                fused[pid] = fused.get(pid, 0.0) + 1.0 / (RRF_K + rank)  # good in both lists = high total
        return sorted(fused.items(), key=lambda x: -x[1])[:k]  # highest score first, top k
