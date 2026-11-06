"""LLM access for Khata's agents: structured output only, free tiers only.

Groq and OpenRouter both speak the OpenAI chat-completions API, so one client
covers both. Every call:

* asks for JSON matching a Pydantic model (``response_format: json_schema``)
  and validates the answer with that model; one repair attempt if it doesn't fit;
* retries rate limits and server errors with backoff, honouring ``retry-after``
  and Groq's ``x-ratelimit-reset-*`` headers; a wait longer than ``max_wait``
  (a daily cap) moves on to the next endpoint in the chain instead;
* is cached on disk by a hash of (endpoints, prompts, schema), so re-running the
  demo or the evals doesn't spend the free quota twice;
* is logged to ``llm_calls`` with the invoice id.

LLMs here read documents and write explanations. They never compute amounts or
make decisions; see khata/agents/.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Generic, TypeVar

import httpx
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, ValidationError

from khata.config import Settings

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

GROQ_URL = "https://api.groq.com/openai/v1"
OPENROUTER_URL = "https://openrouter.ai/api/v1"
RETRYABLE = {408, 409, 425, 429, 500, 502, 503, 504, 529}


class LLMUnavailable(RuntimeError):
    """No endpoint in the chain produced a valid answer."""


@dataclass(frozen=True)
class Endpoint:
    name: str  # how chains refer to it: groq | openrouter | groq-secondary
    provider: str  # groq | openrouter
    base_url: str
    api_key: str
    model: str
    fallback_models: tuple[str, ...] = ()  # OpenRouter's `models` routing
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class LLMResult(Generic[T]):
    value: T
    endpoint: str
    provider: str
    model: str
    cached: bool
    attempts: int
    ms: int


# --- JSON schema for strict structured output --------------------------------


def strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    """The model's JSON schema, inlined and tightened for strict mode.

    Strict structured output wants every property required, no extra keys and
    no $ref indirection; optional fields become ``["type", "null"]``.
    """
    raw = model.model_json_schema()
    defs = raw.pop("$defs", {})

    def fix(node: Any) -> Any:
        if isinstance(node, list):
            return [fix(n) for n in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            return fix(defs[node["$ref"].rsplit("/", 1)[-1]])
        node = {
            k: fix(v)
            for k, v in node.items()
            if k not in {"title", "default", "examples"}
        }
        any_of = node.get("anyOf")
        if any_of and len(any_of) == 2 and {"type": "null"} in any_of:
            other = next(a for a in any_of if a != {"type": "null"})
            if "type" in other and isinstance(other["type"], str):
                node.pop("anyOf")
                node.update(other)
                node["type"] = [other["type"], "null"]
        if node.get("type") == "object" and "properties" in node:
            node["required"] = list(node["properties"])
            node["additionalProperties"] = False
        return node

    return fix(raw)


def parse_json(text: str) -> Any:
    """JSON from a model's reply, tolerating code fences and stray prose."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.S)
    if fenced:
        text = fenced.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise
        return json.loads(text[start : end + 1])


def _wait_from_headers(headers: httpx.Headers) -> float | None:
    """Seconds to wait, from retry-after or Groq's reset headers ("1m26.4s", "3.9s")."""
    ra = headers.get("retry-after")
    if ra:
        try:
            return float(ra)
        except ValueError:
            pass
    best = None
    for h in ("x-ratelimit-reset-tokens", "x-ratelimit-reset-requests"):
        v = headers.get(h)
        m = v and re.fullmatch(r"(?:(\d+)m)?(?:([\d.]+)s)?|([\d.]+)ms", v)
        if m:
            secs = (
                float(m.group(3)) / 1000
                if m.group(3)
                else int(m.group(1) or 0) * 60 + float(m.group(2) or 0)
            )
            best = secs if best is None else min(best, secs)
    return best


class _Retryable(Exception):
    def __init__(self, msg: str, wait: float | None = None) -> None:
        super().__init__(msg)
        self.wait = wait


class _BadSchemaMode(Exception):
    """The endpoint rejected json_schema mode; retry with json_object."""


# --- The client ---------------------------------------------------------------


class LLM:
    def __init__(
        self,
        endpoints: dict[str, Endpoint],
        *,
        cache_dir: Path | None = None,
        pool: AsyncConnectionPool | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        max_retries: int = 4,
        max_wait: float = 65.0,
        timeout: float = 90.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.endpoints = endpoints
        self.cache_dir = cache_dir
        self.pool = pool
        self.max_retries = max_retries
        self.max_wait = max_wait
        self.sleep = sleep
        self.http = httpx.AsyncClient(transport=transport, timeout=timeout)

    async def aclose(self) -> None:
        await self.http.aclose()

    def available(self, name: str) -> bool:
        ep = self.endpoints.get(name)
        return bool(ep and ep.api_key and ep.model)

    # -- cache -----------------------------------------------------------------

    def _key(self, chain: Sequence[str], system: str, user: str, schema: dict) -> str:
        models = [
            (n, self.endpoints[n].model, self.endpoints[n].fallback_models)
            for n in chain
            if n in self.endpoints
        ]
        blob = json.dumps(
            {"models": models, "system": system, "user": user, "schema": schema},
            sort_keys=True,
        )
        return hashlib.sha256(blob.encode()).hexdigest()

    def _cache_get(self, key: str) -> dict | None:
        if self.cache_dir is None:
            return None
        f = self.cache_dir / f"{key}.json"
        try:
            return json.loads(f.read_text()) if f.exists() else None
        except (OSError, json.JSONDecodeError):
            return None

    def _cache_put(self, key: str, entry: dict) -> None:
        if self.cache_dir is None:
            return
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_dir / f"{key}.tmp"
        tmp.write_text(json.dumps(entry, default=str, indent=1))
        tmp.replace(self.cache_dir / f"{key}.json")

    async def _log(self, **row: Any) -> None:
        if self.pool is None:
            return
        try:
            async with self.pool.connection() as conn:
                await conn.execute(
                    """INSERT INTO llm_calls (invoice_id, agent, provider, model, cache_key,
                           cached, ok, attempts, error, prompt_tokens, output_tokens, duration_ms)
                       VALUES (%(invoice_id)s, %(agent)s, %(provider)s, %(model)s, %(cache_key)s,
                           %(cached)s, %(ok)s, %(attempts)s, %(error)s, %(prompt_tokens)s,
                           %(output_tokens)s, %(duration_ms)s)""",
                    {
                        "provider": None,
                        "model": None,
                        "attempts": 1,
                        "error": None,
                        "prompt_tokens": None,
                        "output_tokens": None,
                        **row,
                    },
                )
        except Exception:  # logging must never break the workflow
            logger.exception("could not log llm call")

    # -- one HTTP request ------------------------------------------------------

    async def _post(
        self, ep: Endpoint, messages: list[dict], schema: dict, json_mode: bool
    ) -> tuple[str, str, dict]:
        body: dict[str, Any] = {
            "model": ep.model,
            "messages": messages,
            "temperature": 0,
            "response_format": (
                {"type": "json_object"}
                if json_mode
                else {
                    "type": "json_schema",
                    "json_schema": {"name": "answer", "strict": True, "schema": schema},
                }
            ),
            **ep.extra,
        }
        if ep.fallback_models:
            body["models"] = [ep.model, *ep.fallback_models][:3]
        try:
            r = await self.http.post(
                f"{ep.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {ep.api_key}"},
                json=body,
            )
        except (httpx.TimeoutException, httpx.TransportError) as e:
            raise _Retryable(f"{type(e).__name__}: {e}") from e
        if r.status_code in RETRYABLE:
            raise _Retryable(
                f"HTTP {r.status_code}: {r.text[:200]}", _wait_from_headers(r.headers)
            )
        if r.status_code == 400 and not json_mode and "response_format" in r.text:
            raise _BadSchemaMode(r.text[:200])
        if r.status_code >= 400:
            raise LLMUnavailable(f"{ep.name} HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()
        if "choices" not in data:  # OpenRouter reports upstream errors in a 200
            err = data.get("error", {})
            raise _Retryable(f"upstream error {err.get('code')}: {str(err)[:200]}")
        content = data["choices"][0]["message"].get("content") or ""
        return content, data.get("model", ep.model), data.get("usage") or {}

    # -- the public call -------------------------------------------------------

    async def structured(
        self,
        schema: type[T],
        *,
        system: str,
        user: str,
        chain: Sequence[str],
        agent: str,
        invoice_id: int | None = None,
    ) -> LLMResult[T]:
        """Ask the first working endpoint in `chain` for an answer shaped like `schema`."""
        js = strict_schema(schema)
        sys_prompt = (
            f"{system}\n\nRespond with a single JSON object matching this JSON schema, "
            f"and nothing else:\n{json.dumps(js)}"
        )
        key = self._key(chain, sys_prompt, user, js)
        start = time.perf_counter()
        hit = self._cache_get(key)
        if hit is not None:
            try:
                value = schema.model_validate(hit["value"])
            except ValidationError:
                hit = None
            else:
                await self._log(
                    invoice_id=invoice_id,
                    agent=agent,
                    provider=hit["provider"],
                    model=hit["model"],
                    cache_key=key,
                    cached=True,
                    ok=True,
                    duration_ms=0,
                )
                return LLMResult(
                    value, hit["endpoint"], hit["provider"], hit["model"], True, 0, 0
                )

        errors: list[str] = []
        attempts = 0
        for name in chain:
            ep = self.endpoints.get(name)
            if ep is None or not ep.api_key or not ep.model:
                errors.append(f"{name}: not configured")
                continue
            messages = [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user},
            ]
            json_mode = False
            repaired = False
            retry = 0
            while True:
                attempts += 1
                try:
                    content, model, usage = await self._post(
                        ep, messages, js, json_mode
                    )
                    value = schema.model_validate(parse_json(content))
                except _BadSchemaMode:
                    json_mode = True
                    continue
                except (ValidationError, json.JSONDecodeError, ValueError) as e:
                    if repaired:
                        errors.append(f"{name}: invalid output twice: {e}"[:300])
                        break
                    repaired = True
                    messages += [
                        {"role": "assistant", "content": content[:4000]},
                        {
                            "role": "user",
                            "content": f"That did not match the schema ({str(e)[:400]}). "
                            "Reply again with only the corrected JSON object.",
                        },
                    ]
                    continue
                except _Retryable as e:
                    retry += 1
                    wait = (
                        e.wait
                        if e.wait is not None
                        else min(2**retry, 30) + random.uniform(0, 1)
                    )
                    if retry > self.max_retries or wait > self.max_wait:
                        errors.append(f"{name}: {e}"[:300])
                        break
                    logger.warning(
                        "%s %s: %s; retry %d in %.1fs", agent, name, e, retry, wait
                    )
                    await self.sleep(wait)
                    continue
                except LLMUnavailable as e:
                    errors.append(str(e))
                    break
                ms = int((time.perf_counter() - start) * 1000)
                self._cache_put(
                    key,
                    {
                        "value": value.model_dump(mode="json"),
                        "endpoint": name,
                        "provider": ep.provider,
                        "model": model,
                    },
                )
                await self._log(
                    invoice_id=invoice_id,
                    agent=agent,
                    provider=ep.provider,
                    model=model,
                    cache_key=key,
                    cached=False,
                    ok=True,
                    attempts=attempts,
                    prompt_tokens=usage.get("prompt_tokens"),
                    output_tokens=usage.get("completion_tokens"),
                    duration_ms=ms,
                )
                return LLMResult(value, name, ep.provider, model, False, attempts, ms)
        ms = int((time.perf_counter() - start) * 1000)
        await self._log(
            invoice_id=invoice_id,
            agent=agent,
            cache_key=key,
            cached=False,
            ok=False,
            attempts=attempts,
            error="; ".join(errors)[:2000],
            duration_ms=ms,
        )
        raise LLMUnavailable("; ".join(errors) or "no endpoint in chain")


# --- Prompt Guard: a second, model-based injection check ----------------------


class PromptGuard:
    """Llama Prompt Guard 2 on Groq: the probability that text is a prompt attack.

    It sees at most 512 tokens, so a document is scored in overlapping windows
    of lines and the highest score wins. It complements the regex detector; it
    doesn't replace it (it misses an instruction split across table cells).
    """

    def __init__(
        self,
        endpoint: Endpoint,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        window_chars: int = 1200,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.ep = endpoint
        self.window_chars = window_chars
        self.sleep = sleep
        self.http = httpx.AsyncClient(transport=transport, timeout=30)

    async def aclose(self) -> None:
        await self.http.aclose()

    def windows(self, text: str) -> list[str]:
        lines = [ln for ln in text.splitlines() if ln.strip()]
        out, cur = [], []
        for ln in lines:
            if cur and sum(len(c) + 1 for c in cur) + len(ln) > self.window_chars:
                out.append("\n".join(cur))
                cur = cur[-2:]  # overlap, so a sentence across the cut is seen whole
            cur.append(ln)
        if cur:
            out.append("\n".join(cur))
        return out

    async def _score(self, text: str) -> float:
        for attempt in range(5):
            r = await self.http.post(
                f"{self.ep.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.ep.api_key}"},
                json={
                    "model": self.ep.model,
                    "messages": [{"role": "user", "content": text}],
                },
            )
            if r.status_code in RETRYABLE:
                await self.sleep(min(_wait_from_headers(r.headers) or 2**attempt, 30))
                continue
            r.raise_for_status()
            return float(r.json()["choices"][0]["message"]["content"].strip())
        raise LLMUnavailable("prompt guard rate-limited")

    async def score(self, text: str) -> float:
        scores = [await self._score(w) for w in self.windows(text)]
        return max(scores, default=0.0)


# --- Wiring from settings -----------------------------------------------------


def endpoints_from_settings(s: Settings) -> dict[str, Endpoint]:
    def groq_extra(model: str) -> dict[str, Any]:
        if model.startswith("openai/gpt-oss"):
            return {"reasoning_effort": "low"}
        if "qwen" in model:
            return {"reasoning_format": "hidden"}
        return {}

    fallbacks = tuple(
        m.strip() for m in s.openrouter_fallback_models.split(",") if m.strip()
    )
    return {
        "groq": Endpoint(
            "groq",
            "groq",
            GROQ_URL,
            s.groq_api_key,
            s.groq_model,
            extra=groq_extra(s.groq_model),
        ),
        "openrouter": Endpoint(
            "openrouter",
            "openrouter",
            OPENROUTER_URL,
            s.openrouter_api_key,
            s.openrouter_model or (fallbacks[0] if fallbacks else ""),
            fallback_models=tuple(m for m in fallbacks if m != s.openrouter_model),
        ),
        "groq-secondary": Endpoint(
            "groq-secondary",
            "groq",
            GROQ_URL,
            s.groq_api_key,
            s.groq_secondary_model,
            extra=groq_extra(s.groq_secondary_model),
        ),
        "guard": Endpoint(
            "guard", "groq", GROQ_URL, s.groq_api_key, s.groq_guard_model
        ),
    }
