"""FastAPI app: the contract from brief section 6 (+ /transcribe for the voice bonus, /shoppers for the UI).

HOW: one Brain (DB + search index) is built at startup and shared by all requests.
Pydantic models validate every request body; bad input -> 422 before our code runs.
WHY FastAPI: built-in validation + free Swagger UI at /docs that judges can click through.
"""
import json
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from app import config, db
from app.brain import Brain

state: dict = {}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    state["brain"] = Brain()  # loads DB + builds the hybrid index once
    yield


app = FastAPI(title="NovaMart AI Shopping Assistant", lifespan=lifespan)


class ChatIn(BaseModel):
    session_id: str = Field(min_length=1, max_length=64)
    customer_id: str = Field(min_length=1, max_length=32)  # identity comes from HERE, never from the message (R9)
    message: str = Field(min_length=1, max_length=1000)
    input_mode: Literal["text", "voice"] = "text"


class ProductIn(BaseModel):
    product_id: str = Field(pattern=r"^P-\d{4}$")
    name: str = Field(min_length=2)
    category: Literal["footwear", "electronics", "outdoor", "home_kitchen", "fashion", "sports_fitness"]
    product_type: str | None = None
    brand: str | None = None
    price: int = Field(gt=0)
    stock: int = Field(ge=0)
    attributes: dict = Field(default_factory=dict)
    description: str | None = None
    rating: float | None = Field(default=None, ge=0, le=5)
    reviews: list[str] = Field(default_factory=list)


@app.get("/health")
def health():
    brain = state.get("brain")
    with db.connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    return {"status": "ok" if brain else "starting", "products": n,
            "indexed": len(brain.index.ids) if brain else 0, "mock_llm": config.MOCK_LLM}


@app.post("/chat")
def chat(body: ChatIn):
    reply = state["brain"].handle(body.message, body.session_id, body.customer_id)
    if body.input_mode == "voice":
        with db.connect() as conn:  # record that this turn came from the microphone
            row = conn.execute("SELECT record FROM traces WHERE trace_id=?", (reply["trace_id"],)).fetchone()
            rec = json.loads(row[0])
            rec["input_mode"] = "voice"
            conn.execute("UPDATE traces SET record=? WHERE trace_id=?", (json.dumps(rec, ensure_ascii=False), reply["trace_id"]))
    return reply


@app.get("/products/{product_id}")
def get_product(product_id: str):
    p = db.get_product(product_id)
    if not p:
        raise HTTPException(404, "product not found")
    return p


@app.post("/ingest")
def ingest(products: ProductIn | list[ProductIn]):
    """Add or update products. They are searchable in the very next /chat (R11): DB + both indexes updated."""
    items = products if isinstance(products, list) else [products]
    brain = state["brain"]
    for p in items:
        d = p.model_dump()
        if d["attributes"].get("variants"):
            d["stock"] = sum(int(v.get("stock", 0)) for v in d["attributes"]["variants"])
        db.upsert_product(d)
        brain.index.upsert(d)
    brain.refresh_vocab()  # new brands / product types become known to the router
    return {"ingested": len(items), "product_ids": [p.product_id for p in items]}


@app.get("/trace/{trace_id}")
def get_trace(trace_id: str):
    with db.connect() as conn:
        row = conn.execute("SELECT record FROM traces WHERE trace_id = ?", (trace_id,)).fetchone()
    if not row:
        raise HTTPException(404, "trace not found")
    return json.loads(row[0])


@app.get("/shoppers")
def shoppers():
    """For the UI's shopper picker (stands in for a login). Only id + name, nothing private."""
    with db.connect() as conn:
        return [{"customer_id": r[0], "name": r[1]} for r in conn.execute("SELECT customer_id, name FROM shoppers ORDER BY customer_id")]


@app.post("/transcribe")
async def transcribe(audio: UploadFile = File(...)):
    """Voice bonus: speech -> text with Groq Whisper. The text then goes through the normal /chat."""
    if config.MOCK_LLM:
        raise HTTPException(503, "voice needs the live model (MOCK_LLM=true)")
    data = await audio.read()
    if not data or len(data) > 10 * 1024 * 1024:
        raise HTTPException(400, "audio missing or larger than 10 MB")
    from groq import Groq
    try:
        out = Groq(api_key=config.GROQ_API_KEY).audio.transcriptions.create(
            file=(audio.filename or "speech.wav", data), model=config.WHISPER_MODEL)
    except Exception as e:
        raise HTTPException(502, f"transcription failed: {str(e)[:120]}")
    return {"text": out.text.strip()}
