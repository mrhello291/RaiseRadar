"""OpenRouter client tuned for free models: model fallback, JSON repair, rate limiting, caching, call budget."""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Any

import httpx

from .util import sha

log = logging.getLogger(__name__)
OPENROUTER_URL = "https://openrouter.ai/api/v1"


class LLMError(Exception):
    pass


class LLMUnavailable(LLMError):
    """Every configured model failed for this request."""


class LLMQuotaExceeded(LLMError):
    """Daily/credit limit hit or our own per-run budget used up: stop calling the LLM for this run."""


def extract_json(text: str) -> Any:
    """Parse a JSON object/array out of an LLM reply (handles code fences, prose around it, <think> blocks)."""
    if not text:
        raise ValueError("empty reply")
    t = re.sub(r"<think>.*?</think>", "", text, flags=re.S | re.I).strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.I).strip()
    try:
        return json.loads(t)
    except ValueError:
        pass
    for open_c, close_c in (("{", "}"), ("[", "]")):
        i, j = t.find(open_c), t.rfind(close_c)
        if i != -1 and j > i:
            try:
                return json.loads(t[i : j + 1])
            except ValueError:
                continue
    raise ValueError("no JSON found in reply")


class LLMClient:
    def __init__(
        self,
        api_key: str,
        models: list[str],
        *,
        cache=None,                      # object with cache_get(key)/cache_put(key, resp, model) (the DB)
        max_calls: int = 30,
        requests_per_minute: float = 12,
        base_url: str = OPENROUTER_URL,
        timeout: float = 180,
        client: httpx.Client | None = None,
        sleep=time.sleep,
        max_tokens: int = 8000,
        reasoning_effort: str = "",       # "low"/"medium"/"high" for reasoning models; "" = do not send
        debug_log=None,                   # Path: raw replies that could not be used are appended here (JSON lines)
    ):
        if not models:
            raise ValueError("no LLM models configured")
        self.api_key = api_key
        self.models = models
        self.cache = cache
        self.max_calls = max_calls
        self.min_interval = 60.0 / max(requests_per_minute, 0.1)
        self.base_url = base_url.rstrip("/")
        self.client = client or httpx.Client(timeout=timeout)
        self.sleep = sleep
        self.max_tokens = max_tokens
        self.debug_log = debug_log
        self.reasoning_effort = reasoning_effort
        self.calls = 0
        self.cache_hits = 0
        self.partial_replies = 0
        self.model_used: dict[str, int] = {}
        self._dead: set[str] = set()          # models that 404'd this run
        self._off: dict[str, set[str]] = {}   # per model: optional request params it rejected ("response_format", "reasoning")
        self._last = 0.0
        self._lock = threading.Lock()

    # ---------------------------------------------------------------------------------
    def _throttle(self) -> None:
        with self._lock:
            wait = self._last + self.min_interval - time.monotonic()
            if wait > 0:
                self.sleep(wait)
            self._last = time.monotonic()

    def _post(self, model: str, system: str, user: str, max_tokens: int, json_mode: bool, reasoning: bool = False) -> httpx.Response:
        if self.calls >= self.max_calls:
            raise LLMQuotaExceeded(f"per-run LLM call budget ({self.max_calls}) used up")
        self._throttle()
        self.calls += 1
        body: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0,
            "max_tokens": max_tokens,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if reasoning:
            body["reasoning"] = {"effort": self.reasoning_effort}
        return self.client.post(
            f"{self.base_url}/chat/completions",
            json=body,
            headers={"Authorization": f"Bearer {self.api_key}", "X-Title": "CompanyFinder", "HTTP-Referer": "https://localhost/companyfinder"},
        )

    def _log_failure(self, model: str, finish, content: str, error: str) -> None:
        """Keep the raw text of a bad reply so the cause can be diagnosed (never raises)."""
        if not self.debug_log:
            return
        try:
            path = self.debug_log
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size > 1_000_000:
                path.unlink()
            rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "model": model, "finish_reason": finish, "chars": len(content),
                   "error": error[:300], "head": content[:2500], "tail": content[-800:] if len(content) > 2500 else ""}
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def complete_json(self, system: str, user: str, *, max_tokens: int | None = None, validate=None, salvage=None) -> Any:
        """Ask for a JSON reply. Tries each model in order.

        validate(parsed): may raise ValueError (or return False) to reject a reply and try the next model.
        salvage(text): called when the reply is not valid JSON (typically cut off by the token limit); may return a
        partial parsed result built from the complete pieces, or None.
        """
        max_tokens = max_tokens or self.max_tokens
        key = sha(system + "\n" + user, 40)
        if self.cache:
            hit = self.cache.cache_get(key)
            if hit:
                try:
                    parsed = json.loads(hit)
                    if validate is None or validate(parsed) is not False:
                        self.cache_hits += 1
                        return parsed
                except Exception:
                    pass
        if not self.api_key:
            raise LLMQuotaExceeded("OPENROUTER_API_KEY is not set (add it to .env)")

        errors: list[str] = []
        for model in self.models:
            if model in self._dead:
                continue
            off = self._off.setdefault(model, set())
            rate_limited = 0
            for _attempt in range(5):
                json_mode = "response_format" not in off
                reasoning = bool(self.reasoning_effort) and "reasoning" not in off
                try:
                    resp = self._post(model, system, user, max_tokens, json_mode, reasoning)
                except httpx.HTTPError as exc:
                    errors.append(f"{model}: network {type(exc).__name__}")
                    break
                status = resp.status_code
                if status == 401:
                    raise LLMQuotaExceeded("OpenRouter rejected the API key (401)")
                if status == 400:
                    # free providers often reject optional params: drop them one at a time and remember per model
                    if json_mode:
                        off.add("response_format")
                        continue
                    if reasoning:
                        off.add("reasoning")
                        continue
                    errors.append(f"{model}: HTTP 400 {resp.text[:100]!r}")
                    break
                if status in (402, 429):
                    msg = resp.text[:300].lower()
                    if "per-day" in msg or "per day" in msg or status == 402 or "daily" in msg:
                        raise LLMQuotaExceeded(f"free-tier daily limit reached ({model}): {resp.text[:160]}")
                    try:
                        wait = min(float(resp.headers.get("Retry-After", "")), 30.0)
                    except ValueError:
                        wait = 8.0
                    rate_limited += 1
                    if rate_limited == 1:
                        self.sleep(wait)
                        continue
                    errors.append(f"{model}: rate limited")
                    break
                if status == 404:
                    self._dead.add(model)
                    errors.append(f"{model}: not available (404)")
                    break
                if status >= 400:
                    errors.append(f"{model}: HTTP {status}")
                    break
                try:
                    data = resp.json()
                except ValueError:
                    errors.append(f"{model}: non-JSON HTTP body")
                    break
                if data.get("error"):
                    errors.append(f"{model}: {str(data['error'])[:120]}")
                    break
                content, finish, partial = "", None, False
                try:
                    choice = data["choices"][0]
                    content = (choice.get("message") or {}).get("content") or ""
                    finish = choice.get("finish_reason")
                    try:
                        parsed = extract_json(content)
                    except ValueError:
                        parsed = salvage(content) if salvage else None
                        if parsed is None:
                            raise
                        partial = True
                    if validate is not None and validate(parsed) is False:
                        raise ValueError("validation rejected reply")
                except (KeyError, IndexError, ValueError, TypeError, AttributeError) as exc:
                    errors.append(f"{model}: unusable reply ({exc}; finish_reason={finish}; {len(content)} chars; starts {content[:70]!r})")
                    self._log_failure(model, finish, content, str(exc))
                    break
                if partial:
                    self.partial_replies += 1
                    log.warning("%s: reply was not clean JSON (finish_reason=%s, %d chars); kept the items that were complete", model, finish, len(content))
                    self._log_failure(model, finish, content, "partial: salvaged")
                self.model_used[model] = self.model_used.get(model, 0) + 1
                if self.cache:
                    self.cache.cache_put(key, json.dumps(parsed), model)
                return parsed
        raise LLMUnavailable("; ".join(errors) or "no model available")


def list_free_models(client: httpx.Client | None = None) -> list[dict]:
    """Currently free OpenRouter models (public endpoint, no key needed)."""
    c = client or httpx.Client(timeout=30)
    data = c.get(f"{OPENROUTER_URL}/models").json().get("data", [])
    out = []
    for m in data:
        p = m.get("pricing") or {}
        if str(p.get("prompt")) == "0" and str(p.get("completion")) == "0":
            sp = m.get("supported_parameters") or []
            modality = (m.get("architecture") or {}).get("modality", "")
            if modality and modality.split("->")[-1].strip() != "text":
                continue   # skip music/image/audio generators and similar
            out.append({
                "id": m["id"],
                "context": m.get("context_length"),
                "json": "response_format" in sp or "structured_outputs" in sp,
                "tools": "tools" in sp,
                "modality": modality,
            })
    return sorted(out, key=lambda m: -(m["context"] or 0))
