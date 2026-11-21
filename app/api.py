"""FastAPI app: the contract from brief section 6 (+ /transcribe for the voice bonus, /shoppers for the UI).

HOW: one Brain (DB + search index) is built at startup and shared by all requests.
Pydantic models validate every request body; bad input -> 422 before our code runs.
WHY FastAPI: built-in validation + free Swagger UI at /docs that judges can click through.
"""
import json  # traces are stored as JSON
from contextlib import asynccontextmanager  # for the startup/shutdown "lifespan" hook
from typing import Literal  # restrict a field to fixed values

from fastapi import FastAPI, File, HTTPException, UploadFile  # web framework pieces
from pydantic import BaseModel, Field  # request-body schemas with validation

from app import config, db  # settings + SQLite
from app.brain import Brain  # the whole pipeline

state: dict = {}  # holds the single shared Brain (set at startup)


@asynccontextmanager  # code before "yield" runs at startup, after it at shutdown
async def lifespan(_app: FastAPI):
    state["brain"] = Brain()  # loads DB + builds the hybrid index once
    yield  # the API serves requests while we're paused here


app = FastAPI(title="NovaMart AI Shopping Assistant", lifespan=lifespan)  # the app; title shows in Swagger /docs


class ChatIn(BaseModel):  # body of POST /chat (the brief: session_id, customer_id, message)
    session_id: str = Field(min_length=1, max_length=64)  # which conversation
    customer_id: str = Field(min_length=1, max_length=32)  # identity comes from HERE, never from the message (R9)
    message: str = Field(min_length=1, max_length=1000)  # empty or huge messages -> 422 automatically
    input_mode: Literal["text", "voice"] = "text"  # set to "voice" by the UI after /transcribe


class ProductIn(BaseModel):  # body of POST /ingest (one product); bad data is rejected before it reaches the DB
    product_id: str = Field(pattern=r"^P-\d{4}$")  # must look like P-1234
    name: str = Field(min_length=2)
    category: Literal["footwear", "electronics", "outdoor", "home_kitchen", "fashion", "sports_fitness"]  # only known categories
    product_type: str | None = None  # new types are allowed (e.g. "Meditation Cushion")
    brand: str | None = None
    price: int = Field(gt=0)  # greater than 0
    stock: int = Field(ge=0)  # 0 or more
    attributes: dict = Field(default_factory=dict)  # specs; may include "variants"
    description: str | None = None
    rating: float | None = Field(default=None, ge=0, le=5)  # 0..5
    reviews: list[str] = Field(default_factory=list)


@app.get("/health")  # judges / Docker healthcheck
def health():
    brain = state.get("brain")  # None while still starting
    with db.connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]  # products in the DB
    return {"status": "ok" if brain else "starting", "products": n,
            "indexed": len(brain.index.ids) if brain else 0, "mock_llm": config.MOCK_LLM}  # indexed should equal products


@app.post("/chat")  # the main endpoint
def chat(body: ChatIn):  # FastAPI validated the body already
    reply = state["brain"].handle(body.message, body.session_id, body.customer_id)  # run the whole pipeline
    if body.input_mode == "voice":
        with db.connect() as conn:  # record that this turn came from the microphone
            row = conn.execute("SELECT record FROM traces WHERE trace_id=?", (reply["trace_id"],)).fetchone()  # the trace just saved
            rec = json.loads(row[0])
            rec["input_mode"] = "voice"  # mark it
            conn.execute("UPDATE traces SET record=? WHERE trace_id=?", (json.dumps(rec, ensure_ascii=False), reply["trace_id"]))  # save back
    return reply  # FastAPI turns the dict into JSON


@app.get("/products/{product_id}")  # judges check our claims against the catalogue
def get_product(product_id: str):
    p = db.get_product(product_id)
    if not p:
        raise HTTPException(404, "product not found")  # proper HTTP error
    return p


@app.post("/ingest")  # add/update products while running
def ingest(products: ProductIn | list[ProductIn]):  # accepts one product or a list
    """Add or update products. They are searchable in the very next /chat (R11): DB + both indexes updated."""
    items = products if isinstance(products, list) else [products]  # always work with a list
    brain = state["brain"]
    for p in items:
        d = p.model_dump()  # Pydantic object -> plain dict
        if d["attributes"].get("variants"):  # sized product: total stock = sum of variant stock
            d["stock"] = sum(int(v.get("stock", 0)) for v in d["attributes"]["variants"])
        db.upsert_product(d)  # 1) save in SQLite
        brain.index.upsert(d)  # 2) add to FAISS + BM25 (searchable now)
    brain.refresh_vocab()  # new brands / product types become known to the router
    return {"ingested": len(items), "product_ids": [p.product_id for p in items]}


@app.get("/trace/{trace_id}")  # R10: replay how an answer was made
def get_trace(trace_id: str):
    with db.connect() as conn:
        row = conn.execute("SELECT record FROM traces WHERE trace_id = ?", (trace_id,)).fetchone()
    if not row:
        raise HTTPException(404, "trace not found")
    return json.loads(row[0])  # the full audit record


@app.get("/shoppers")
def shoppers():
    """For the UI's shopper picker (stands in for a login). Only id + name, nothing private."""
    with db.connect() as conn:
        return [{"customer_id": r[0], "name": r[1]} for r in conn.execute("SELECT customer_id, name FROM shoppers ORDER BY customer_id")]


@app.post("/transcribe")  # voice bonus
async def transcribe(audio: UploadFile = File(...)):  # multipart file upload; "..." = required
    """Voice bonus: speech -> text with Groq Whisper. The text then goes through the normal /chat."""
    if config.MOCK_LLM:  # Whisper needs the network
        raise HTTPException(503, "voice needs the live model (MOCK_LLM=true)")
    data = await audio.read()  # the audio bytes
    if not data or len(data) > 10 * 1024 * 1024:  # empty or > 10 MB -> reject
        raise HTTPException(400, "audio missing or larger than 10 MB")
    from groq import Groq
    try:
        out = Groq(api_key=config.GROQ_API_KEY).audio.transcriptions.create(  # speech -> text
            file=(audio.filename or "speech.wav", data), model=config.WHISPER_MODEL)
    except Exception as e:
        raise HTTPException(502, f"transcription failed: {str(e)[:120]}")  # 502 = upstream service failed
    return {"text": out.text.strip()}  # the UI then sends this text to /chat
