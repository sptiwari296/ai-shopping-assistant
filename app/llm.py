"""Groq client: ask for JSON, get a dict back. Raises LLMError so callers can fall back to code."""
import json  # turn the model's JSON text into a Python dict
import time  # measure how long a call takes; wait before a retry

from app import config  # API key, model name, MOCK_LLM switch


class LLMError(Exception):  # our own error type: "the AI could not help this time"
    pass  # no extra code needed; callers catch it and use code-written text instead


def chat_json(system: str, user: str, max_tokens: int = 900) -> tuple[dict, dict]:  # system = rules, user = data
    if config.MOCK_LLM:  # mock mode: never call the network
        raise LLMError("mock mode")  # the caller falls back to rules / templates
    from groq import Groq, RateLimitError  # imported here so mock mode never needs the network library
    client = Groq(api_key=config.GROQ_API_KEY, max_retries=0, timeout=30)  # we do our own retries; max 30 s per call
    last = "unknown"  # remembers why the last attempt failed (goes into the error message)
    for attempt in range(3):  # retry: rate limits and broken JSON happen
        t0 = time.time()  # start the stopwatch for this attempt
        try:
            resp = client.chat.completions.create(  # ONE request to the Groq LLM
                model=config.GROQ_MODEL, temperature=0, max_tokens=max_tokens,  # temperature 0 = no randomness; cap reply length
                response_format={"type": "json_object"},  # JSON mode: reply must be valid JSON
                reasoning_effort="low",  # gpt-oss thinks first; "low" keeps it short (empty-reply bug)
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])  # rules first, then data
            usage = {"ms": int((time.time() - t0) * 1000), "tokens": resp.usage.total_tokens}  # time + tokens for the trace
            return json.loads(resp.choices[0].message.content or ""), usage  # reply text -> dict (broken JSON jumps below)
        except RateLimitError:  # HTTP 429 (free-tier limit): wait once, briefly, then fail fast -> code fallback
            last = "rate limited (429)"  # remember the reason
            if attempt:  # second time we hit the limit
                break  # stop retrying -> fail fast
            time.sleep(2)  # first time: wait 2 s and try again
        except json.JSONDecodeError:  # broken JSON -> just try again
            last = "invalid JSON"  # remember the reason, loop retries
        except Exception as e:  # network / bad key -> give up, caller uses the fallback
            raise LLMError(str(e)[:150])  # short error text for the trace
    raise LLMError(f"failed after 3 tries: {last}")  # all attempts failed: say the real reason
