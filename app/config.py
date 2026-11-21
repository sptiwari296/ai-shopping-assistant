"""All settings in one place, read from the .env file."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "whisper-large-v3-turbo")

# MOCK_LLM=true -> no network calls; rule-based stand-ins are used instead.
MOCK_LLM = os.getenv("MOCK_LLM", "false").lower() == "true"

DB_PATH = ROOT / os.getenv("DB_PATH", "data/shop.db")
DATA_DIR = ROOT / "data"
