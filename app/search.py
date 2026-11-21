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
import os  # read FASTEMBED_CACHE_PATH (set in Docker)
import re  # regex used to split text into words
import threading  # Lock: API requests run in parallel threads

import faiss  # Facebook AI Similarity Search: fast vector search
import numpy as np  # arrays of numbers (the vectors)
from fastembed import TextEmbedding  # text -> vector, runs locally on CPU (ONNX), no API cost
from rank_bm25 import BM25Okapi  # classic keyword-ranking algorithm

EMBED_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"  # multilingual: Hindi + English share one space; 384 numbers per text
RRF_K = 60  # the constant in 1/(60 + rank); standard value from the RRF paper
TOKEN = re.compile(r"[\wऀ-ॿ]+")  # one "word" = letters/digits/_ OR any Devanagari char (keeps Hindi vowel signs inside words)


def tokenize(text: str) -> list[str]:  # used by BM25 only (the embedding model has its own tokenizer)
    return TOKEN.findall(text.lower())  # lowercase so "JBL" == "jbl", then list every word


def index_text(p: dict) -> str:  # the text that represents one product in BOTH indexes
    attrs = p.get("attributes") or {}  # messy data: attributes may be missing -> use {}
    flat = []  # attribute words collected here
    for k, v in attrs.items():  # e.g. ("output_watts", 20)
        if k == "variants":  # shoes/clothes: list of {color, size, stock}
            flat.append("colors " + " ".join(sorted({x["color"] for x in v})))  # unique colours only; sizes are checked exactly later, not searched
        else:
            flat.append(f"{k.replace('_', ' ')} {v}")  # "output_watts 20" -> "output watts 20" so the word "watts" can match
    return " ".join(str(x) for x in [p["name"], p.get("product_type"), p["category"].replace("_", " "),  # home_kitchen -> "home kitchen"
                                     p.get("brand") or "", p.get("description") or "", " ".join(flat)])  # brand may be None -> ""; REVIEWS deliberately left out (R8)


class HybridIndex:
    def __init__(self):
        # In Docker the model is pre-downloaded to FASTEMBED_CACHE_PATH at build time
        self.model = TextEmbedding(EMBED_MODEL, cache_dir=os.getenv("FASTEMBED_CACHE_PATH") or None)  # load once; None = default cache
        self.lock = threading.Lock()  # one thread at a time inside the index (ingest vs search)
        self.ids: list[str] = []  # position i -> product_id
        self.texts: list[str] = []  # position i -> that product's index text
        self.vectors = np.zeros((0, 384), dtype="float32")  # cached embeddings, one row per product
        self.faiss = faiss.IndexFlatIP(384)  # Flat = exact search over all vectors; IP = inner product
        self.bm25 = None  # built in _rebuild()

    def _embed(self, texts: list[str]) -> np.ndarray:  # texts -> table of unit vectors
        vecs = np.array(list(self.model.embed(texts)), dtype="float32")  # embed() yields vectors one by one -> list -> 2-D array; FAISS needs float32
        faiss.normalize_L2(vecs)  # normalised vectors -> inner product == cosine similarity
        return vecs  # shape (len(texts), 384)

    def build(self, products: list[dict]) -> None:  # runs once at startup
        with self.lock:  # nobody searches while we build
            self.ids = [p["product_id"] for p in products]  # same order in all three lists
            self.texts = [index_text(p) for p in products]
            self.vectors = self._embed(self.texts)  # the slow part (~10 s for 276 products)
            self._rebuild()  # create FAISS + BM25 from them

    def _rebuild(self) -> None:
        """Rebuild both indexes from cached vectors/texts (fast: no re-embedding)."""
        self.faiss = faiss.IndexFlatIP(384)  # fresh empty vector index
        self.faiss.add(self.vectors)  # add every cached vector (row i = product i)
        self.bm25 = BM25Okapi([tokenize(t) for t in self.texts])  # word statistics change when any doc changes -> rebuild (milliseconds)

    def upsert(self, product: dict) -> None:
        """R11: a new or changed product is searchable immediately, no restart.
        Only the changed product is embedded; the other vectors come from the cache."""
        pid = product["product_id"]
        text = index_text(product)
        vec = self._embed([text])  # slow AI work done OUTSIDE the lock so searches aren't blocked
        with self.lock:
            if pid in self.ids:  # existing product -> update in place
                i = self.ids.index(pid)  # its position
                self.texts[i] = text  # replace text
                self.vectors[i] = vec[0]  # replace vector (lists stay parallel)
            else:  # new product -> add at the end
                self.ids.append(pid)
                self.texts.append(text)
                self.vectors = np.vstack([self.vectors, vec])  # stack one new row under the table
            self._rebuild()  # 0.03 s (was 5.5 s when every product was re-embedded)

    def search(self, query: str, k: int = 30) -> list[tuple[str, float]]:
        """Return [(product_id, rrf_score)] best first."""
        with self.lock:
            n = len(self.ids)  # how many products are indexed
            if not n or not query.strip():  # empty index or blank query -> nothing to do
                return []
            depth = min(k, n)  # never ask for more results than products exist
            _, idx = self.faiss.search(self._embed([query]), depth)  # (A) DENSE: nearest vectors by meaning; we ignore the scores (_)
            dense = [self.ids[i] for i in idx[0] if i >= 0]  # positions -> product IDs; FAISS uses -1 for "no result"
            scores = self.bm25.get_scores(tokenize(query))  # (B) SPARSE: one BM25 score per product (exact words)
            sparse = [self.ids[i] for i in np.argsort(-scores)[:depth] if scores[i] > 0]  # best first (minus = descending); drop zero-match products

        fused: dict[str, float] = {}  # (C) RRF: product_id -> combined score (outside the lock: only local data)
        for ranked in (dense, sparse):  # each list "votes"
            for rank, pid in enumerate(ranked, start=1):  # rank 1, 2, 3...
                fused[pid] = fused.get(pid, 0.0) + 1.0 / (RRF_K + rank)  # vote = 1/(60+rank); good in both lists = high sum
        return sorted(fused.items(), key=lambda x: -x[1])[:k]  # highest score first, keep top k
