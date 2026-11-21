"""FastAPI app. Phase 0: only /health. Other endpoints are added in later phases."""
from fastapi import FastAPI

from app import config

app = FastAPI(title="AI Shopping Assistant")


@app.get("/health")
def health():
    return {"status": "ok", "mock_llm": config.MOCK_LLM}
