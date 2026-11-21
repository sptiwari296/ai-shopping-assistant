"""FastAPI: the 5 endpoints from brief §6, plus /transcribe (voice bonus) and /shoppers (UI picker).
One Brain is built at startup and shared. Pydantic rejects bad input with 422 before our code runs.
"""
from contextlib import asynccontextmanager  # startup / shutdown hook ("lifespan")
from typing import Literal  # restrict a field to fixed values

from fastapi import FastAPI, File, HTTPException, UploadFile  # web framework pieces
from pydantic import BaseModel, Field  # request-body schemas with automatic validation

from app import config, db  # settings + SQLite
from app.brain import Brain  # the whole pipeline

state = {}  # holds the one shared Brain


@asynccontextmanager  # code before "yield" runs at startup, after it at shutdown
async def lifespan(_app):
    state["brain"] = Brain()  # build DB + search index once
    yield  # the API serves requests while paused here


app = FastAPI(title="NovaMart AI Shopping Assistant", lifespan=lifespan)  # the app; title shows in Swagger /docs


class ChatIn(BaseModel):  # body of POST /chat
    session_id: str = Field(min_length=1, max_length=64)  # which conversation
    customer_id: str = Field(min_length=1, max_length=32)  # identity comes from HERE, never from the message (R9)
    message: str = Field(min_length=1, max_length=1000)  # empty / huge messages -> 422 automatically
    input_mode: Literal["text", "voice"] = "text"  # "voice" when it came from the microphone


class ProductIn(BaseModel):  # body of POST /ingest; bad products are rejected before reaching the DB
    product_id: str = Field(pattern=r"^P-\d{4}$")  # must look like P-1234
    name: str = Field(min_length=2)
    category: Literal["footwear", "electronics", "outdoor", "home_kitchen", "fashion", "sports_fitness"]  # known categories only
    product_type: str | None = None  # new types allowed
    brand: str | None = None
    price: int = Field(gt=0)  # greater than 0
    stock: int = Field(ge=0)  # 0 or more
    attributes: dict = Field(default_factory=dict)  # specs; may include "variants"
    description: str | None = None
    rating: float | None = Field(default=None, ge=0, le=5)  # 0..5
    reviews: list[str] = Field(default_factory=list)


@app.get("/health")  # is the app up? (used by Docker and judges)
def health():
    brain = state.get("brain")  # None while still starting
    return {"status": "ok" if brain else "starting", "indexed": len(brain.index.ids) if brain else 0, "mock_llm": config.MOCK_LLM}


@app.post("/chat")  # the main endpoint
def chat(body: ChatIn):  # FastAPI has already validated the body
    r = state["brain"].handle(body.message, body.session_id, body.customer_id)  # run the whole pipeline
    if body.input_mode == "voice":  # mark the trace as a voice turn
        trace = db.get_trace(r["trace_id"])  # the trace just saved
        trace["input_mode"] = "voice"
        with db.connect() as conn:
            conn.execute("DELETE FROM traces WHERE trace_id = ?", (r["trace_id"],))  # replace the old record...
        db.save_trace(trace)  # ...with the voice-marked one
    return r  # FastAPI turns the dict into JSON


@app.get("/products/{product_id}")  # judges check our claims against the catalogue
def get_product(product_id: str):
    p = db.get_product(product_id)
    if not p:
        raise HTTPException(404, "product not found")  # proper HTTP error, not a crash
    return p


@app.post("/ingest")  # add or update products while running
def ingest(products: ProductIn | list[ProductIn]):  # R11: searchable in the very next /chat, no restart
    items = products if isinstance(products, list) else [products]  # one product or a list -> always a list
    for item in items:
        p = item.model_dump()  # Pydantic object -> dict
        if p["attributes"].get("variants"):  # total stock = sum of variant stock
            p["stock"] = sum(int(v.get("stock", 0)) for v in p["attributes"]["variants"])
        db.upsert_product(p)  # 1) SQLite
        state["brain"].index.upsert(p)  # 2) FAISS + BM25
    state["brain"].refresh()  # 3) new types / brands known to the router
    return {"ingested": len(items), "product_ids": [i.product_id for i in items]}


@app.get("/trace/{trace_id}")
def get_trace(trace_id: str):  # R10: replay how an answer was made
    t = db.get_trace(trace_id)
    if not t:
        raise HTTPException(404, "trace not found")
    return t  # the full audit record


@app.get("/shoppers")
def shoppers():  # for the UI's demo login: ids + names only
    return db.query("SELECT customer_id, name FROM shoppers ORDER BY customer_id")


@app.post("/transcribe")
async def transcribe(audio: UploadFile = File(...)):  # voice bonus: speech -> text, then the normal /chat
    if config.MOCK_LLM:  # Whisper needs the network
        raise HTTPException(503, "voice needs the live model")
    data = await audio.read()  # the recorded audio bytes
    if not data or len(data) > 10_000_000:  # empty or over 10 MB -> reject
        raise HTTPException(400, "audio missing or over 10 MB")
    from groq import Groq
    try:
        out = Groq(api_key=config.GROQ_API_KEY).audio.transcriptions.create(  # speech -> text with Whisper
            file=("speech.wav", data), model=config.WHISPER_MODEL, temperature=0,  # temperature 0 = most likely words
            prompt=VOICE_HINT)  # vocabulary hint: steers Whisper to shopping words in English / Hinglish (live bug: "soosh", "Bueno")
    except Exception as e:
        raise HTTPException(502, f"transcription failed: {str(e)[:100]}")  # 502 = the outside service failed
    text = out.text.strip()  # the transcript
    if len(text.split()) < 2:  # silence / noise gives 1-word junk -> ask again instead of searching for it
        raise HTTPException(422, "Couldn't hear that clearly. Please speak a little longer and closer to the mic.")
    return {"text": text}  # the UI then sends this text to /chat


# Example sentences given to Whisper so it expects shopping words (English + Hinglish)
VOICE_HINT = ("NovaMart shopping assistant. Shopper asks in English or Hinglish, e.g.: running shoes under 5000 rupees, "
              "Bluetooth speaker, laptop for study, trekking backpack, earbuds, kurta, mujhe sasta speaker chahiye, "
              "joote dikhao, 3000 ke andar, compare the first two, is the blue one in size 9.")
