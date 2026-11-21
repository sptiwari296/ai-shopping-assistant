"""Thin Groq client: JSON calls with validation-friendly retries.

HOW: chat_json() asks Groq for a JSON object (JSON mode), parses it and returns a dict.
On rate limits (HTTP 429) it waits and retries; on bad JSON it retries once.
It raises LLMError when it cannot get valid JSON, so callers can fall back to
rule-based code (and MOCK_LLM=true never calls the network at all).

WHY a thin wrapper and not LangChain: two simple calls per message; fewer layers
means every line can be explained, and swapping provider = changing this one file.
"""
import json  # parse the model's JSON text into a Python dict
import time  # measure latency and wait between retries

from app import config  # API key, model name, MOCK_LLM flag


class LLMError(Exception):  # our own error type: "the LLM could not give us valid JSON"
    pass  # no extra behaviour; callers catch it and use the rule-based fallback


_client = None  # the Groq client, created lazily (only when first needed) and then reused


def _get_client():
    global _client  # we assign the module-level variable, not a local one
    if _client is None:  # first call: create the client
        from groq import Groq  # imported here so mock mode never needs the groq package to connect
        _client = Groq(api_key=config.GROQ_API_KEY, max_retries=0, timeout=30)  # we do our own retries; 30 s max per call
    return _client  # later calls reuse the same client (keeps the HTTP connection)


def chat_json(system: str, user: str, max_tokens: int = 900) -> tuple[dict, dict]:
    """Return (parsed_json, usage_info). Raises LLMError."""
    if config.MOCK_LLM:  # mock mode: never touch the network
        raise LLMError("mock mode: no LLM calls")  # caller catches this and uses rules/templates
    from groq import APIStatusError, RateLimitError  # the error types the Groq SDK can raise

    last = None  # remembers the last error, for the final message
    for attempt in range(3):  # try at most 3 times
        t0 = time.time()  # start time of this attempt
        try:
            resp = _get_client().chat.completions.create(  # one chat request to Groq
                model=config.GROQ_MODEL,  # e.g. openai/gpt-oss-120b
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],  # rules + data
                response_format={"type": "json_object"},  # JSON mode: the model must return valid JSON
                temperature=0,  # no randomness: same input -> (almost) same output, easier to test
                max_tokens=max_tokens,  # cap on reply length (cost + speed)
                reasoning_effort="low",  # gpt-oss is a reasoning model; low = think briefly (empty-reply bug in Phase 0)
            )
            text = resp.choices[0].message.content or ""  # the reply text ("" if the model returned nothing)
            usage = {"model": config.GROQ_MODEL, "ms": int((time.time() - t0) * 1000),  # latency in milliseconds
                     "tokens": getattr(resp.usage, "total_tokens", None)}  # tokens used (for cost), if reported
            return json.loads(text), usage  # parse JSON; a parse error jumps to the JSONDecodeError branch
        except RateLimitError as e:  # HTTP 429: too many requests on the free tier
            last = e  # remember it
            time.sleep(min(2 ** attempt * 3, 15))  # exponential backoff: wait 3 s, 6 s, 12 s (max 15)
        except json.JSONDecodeError as e:  # the model replied with broken JSON
            last = e  # remember it and simply try again
        except APIStatusError as e:  # any other HTTP error from Groq
            last = e  # remember it
            if e.status_code < 500:  # 4xx = our request is wrong (bad key etc.): retrying won't help
                break  # stop retrying
        except Exception as e:  # network, timeout
            last = e  # remember it and retry
    raise LLMError(f"LLM failed: {type(last).__name__}: {str(last)[:200]}")  # all attempts failed -> caller falls back
