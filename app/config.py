"""All settings in one place, read from the .env file."""
import os  # read environment variables (os.getenv)
from pathlib import Path  # build file paths that work on Windows and Linux

from dotenv import load_dotenv  # loads KEY=VALUE lines from .env into the environment

ROOT = Path(__file__).resolve().parent.parent  # project root: app/config.py -> app/ -> shopping-assistant/
load_dotenv(ROOT / ".env")  # read .env once at import time (the API key never sits in code)

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")  # secret key for Groq; "" if missing (then LLM calls fail -> rule fallback)
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")  # which LLM to use for steps 1 and 4
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "whisper-large-v3-turbo")  # speech-to-text model for the voice bonus

# MOCK_LLM=true -> no network calls; rule-based stand-ins are used instead.
MOCK_LLM = os.getenv("MOCK_LLM", "false").lower() == "true"  # string "true"/"false" -> Python bool

DB_PATH = ROOT / os.getenv("DB_PATH", "data/shop.db")  # SQLite file location (Docker overrides it to db/shop.db)
DATA_DIR = ROOT / "data"  # folder with catalogue.json, shoppers.json, orders.json (seed data)
