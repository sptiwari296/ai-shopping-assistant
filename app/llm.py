"""Thin Groq client: JSON calls with validation-friendly retries.

HOW: chat_json() asks Groq for a JSON object (JSON mode), parses it and returns a dict.
On rate limits (HTTP 429) it waits and retries; on bad JSON it retries once.
It raises LLMError when it cannot get valid JSON, so callers can fall back to
rule-based code (and MOCK_LLM=true never calls the network at all).

WHY a thin wrapper and not LangChain: two simple calls per message; fewer layers
means every line can be explained, and swapping provider = changing this one file.
"""
import json
import time

from app import config


class LLMError(Exception):
    pass


_client = None


def _get_client():
    global _client
    if _client is None:
        from groq import Groq
        _client = Groq(api_key=config.GROQ_API_KEY, max_retries=0, timeout=30)
    return _client


def chat_json(system: str, user: str, max_tokens: int = 900) -> tuple[dict, dict]:
    """Return (parsed_json, usage_info). Raises LLMError."""
    if config.MOCK_LLM:
        raise LLMError("mock mode: no LLM calls")
    from groq import APIStatusError, RateLimitError

    last = None
    for attempt in range(3):
        t0 = time.time()
        try:
            resp = _get_client().chat.completions.create(
                model=config.GROQ_MODEL,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                response_format={"type": "json_object"},
                temperature=0,
                max_tokens=max_tokens,
                reasoning_effort="low",
            )
            text = resp.choices[0].message.content or ""
            usage = {"model": config.GROQ_MODEL, "ms": int((time.time() - t0) * 1000),
                     "tokens": getattr(resp.usage, "total_tokens", None)}
            return json.loads(text), usage
        except RateLimitError as e:
            last = e
            time.sleep(min(2 ** attempt * 3, 15))
        except json.JSONDecodeError as e:
            last = e
        except APIStatusError as e:
            last = e
            if e.status_code < 500:
                break
        except Exception as e:  # network, timeout
            last = e
    raise LLMError(f"LLM failed: {type(last).__name__}: {str(last)[:200]}")
