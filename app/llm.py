"""Groq client: ask for JSON, get a dict back. Raises LLMError so callers can fall back to code."""
import json
import time

from app import config


class LLMError(Exception):
    pass


def chat_json(system: str, user: str, max_tokens: int = 900) -> tuple[dict, dict]:
    if config.MOCK_LLM:  # mock mode: never call the network
        raise LLMError("mock mode")
    from groq import Groq, RateLimitError
    client = Groq(api_key=config.GROQ_API_KEY, max_retries=0, timeout=30)
    last = "unknown"
    for attempt in range(3):  # retry: rate limits and broken JSON happen
        t0 = time.time()
        try:
            resp = client.chat.completions.create(
                model=config.GROQ_MODEL, temperature=0, max_tokens=max_tokens,
                response_format={"type": "json_object"},  # JSON mode: reply must be valid JSON
                reasoning_effort="low",  # gpt-oss thinks first; "low" keeps it short (empty-reply bug)
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
            usage = {"ms": int((time.time() - t0) * 1000), "tokens": resp.usage.total_tokens}
            return json.loads(resp.choices[0].message.content or ""), usage
        except RateLimitError:  # HTTP 429 (free-tier limit): wait once, briefly, then fail fast -> code fallback
            last = "rate limited (429)"
            if attempt:
                break
            time.sleep(2)
        except json.JSONDecodeError:  # broken JSON -> just try again
            last = "invalid JSON"
        except Exception as e:  # network / bad key -> give up, caller uses the fallback
            raise LLMError(str(e)[:150])
    raise LLMError(f"failed after 3 tries: {last}")
