"""
llm_selector.py
-------------
Single shared entry point for every chat-completion call in the project.
Both intent_classifier.py and reply_generator.py call
create_chat_completion() instead of building their own OpenAI client.

Behavior: try the primary endpoint. If that call fails for ANY reason,
log it and try each fallback provider in order (as listed in
FALLBACK_LLM_APIKEY_ENDPOINT_MODEL) until one succeeds. If every
provider fails, the LAST provider's exception is what propagates to the
caller.

Required environment variables:
    PRIMARY_LLM_API_KEY     - required
    PRIMARY_LLM_BASE_URL    - your primary custom endpoint's base URL
    PRIMARY_LLM_MODEL       - model name as that provider expects it

    FALLBACK_LLM_APIKEY_ENDPOINT_MODEL
        A JSON-encoded list of fallback providers, tried in the order
        given, e.g.:
        [
          {"api_key": "abc", "base_url": "https://.../v1", "model": "gemma-4-31b-it"},
          {"api_key": "xyz", "base_url": "https://.../v1", "model": "llama-3"}
        ]
        Each item needs exactly these three keys. Malformed entries or
        an unparseable/missing env var are logged and skipped rather
        than crashing at import time — worst case, you just have zero
        fallbacks instead of a broken deploy.

All endpoints must be OpenAI-compatible (support the /chat/completions
shape) — true for anything you can point the `openai` Python SDK's
`base_url` at.
"""

import json
import os
import re

# Only wrap with Langfuse tracing if valid Langfuse keys are configured,
# preventing OpenTelemetry batch export timeout errors and thread freezes.
if (os.environ.get("LANGFUSE_PUBLIC_KEY") or "").strip() and (os.environ.get("LANGFUSE_SECRET_KEY") or "").strip():
    try:
        from langfuse.openai import OpenAI
    except ImportError:
        try:
            from openai import OpenAI
        except ImportError:
            OpenAI = None
else:
    try:
        from openai import OpenAI
    except ImportError:
        OpenAI = None

_DISCOVERED_PRIMARY_MODEL: str | None = None


def _resolve_primary_config():
    global _DISCOVERED_PRIMARY_MODEL
    api_key = (
        os.environ.get("PRIMARY_LLM_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or os.environ.get("GROQ_API_KEY")
        or os.environ.get("GEMINI_API_KEY")
        or ""
    ).strip()

    base_url = os.environ.get("PRIMARY_LLM_BASE_URL")
    if not base_url:
        if os.environ.get("GROQ_API_KEY"):
            base_url = "https://api.groq.com/openai/v1"
        elif os.environ.get("GEMINI_API_KEY"):
            base_url = "https://generativelanguage.googleapis.com/v1beta/openai/"
        elif os.environ.get("OPENAI_API_KEY"):
            base_url = "https://api.openai.com/v1"

    if _DISCOVERED_PRIMARY_MODEL:
        return api_key, base_url, _DISCOVERED_PRIMARY_MODEL

    model = (
        os.environ.get("PRIMARY_LLM_MODEL")
        or os.environ.get("LLM_MODEL")
        or os.environ.get("MODEL")
        or os.environ.get("CUSTOM_MODEL")
    )
    if not model:
        if os.environ.get("PRIMARY_LLM_BASE_URL"):
            model = os.environ.get("OPENAI_MODEL") or "gpt-4o-mini"
        elif os.environ.get("OPENAI_API_KEY"):
            model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
        elif os.environ.get("GROQ_API_KEY"):
            model = "llama-3.3-70b-versatile"
        elif os.environ.get("GEMINI_API_KEY"):
            model = "gemini-1.5-flash"
        else:
            model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

    return api_key, base_url, model


PRIMARY_API_KEY, PRIMARY_BASE_URL, PRIMARY_MODEL = _resolve_primary_config()
PRIMARY_TIMEOUT = float(os.environ.get("PRIMARY_LLM_TIMEOUT", "20.0"))
FALLBACK_TIMEOUT = float(os.environ.get("FALLBACK_LLM_TIMEOUT", "25.0"))


def _probe_and_discover_primary_model(client: OpenAI, current_model: str) -> str:
    global _DISCOVERED_PRIMARY_MODEL
    if _DISCOVERED_PRIMARY_MODEL:
        return _DISCOVERED_PRIMARY_MODEL
    if os.environ.get("PRIMARY_LLM_MODEL"):
        return os.environ.get("PRIMARY_LLM_MODEL").strip()

    base_url = os.environ.get("PRIMARY_LLM_BASE_URL") or ""
    if base_url and "api.openai.com" not in base_url:
        try:
            model_list = client.models.list()
            data = getattr(model_list, "data", None)
            if data and len(data) > 0:
                first_model = getattr(data[0], "id", None) or (data[0].get("id") if isinstance(data[0], dict) else None)
                if first_model:
                    print(f"[llm_selector] Auto-discovered primary model from custom endpoint: '{first_model}'")
                    _DISCOVERED_PRIMARY_MODEL = str(first_model).strip()
                    return _DISCOVERED_PRIMARY_MODEL
        except Exception:
            pass
    return current_model


def _load_fallback_providers() -> list[dict]:
    raw = (os.environ.get("FALLBACK_LLM_APIKEY_ENDPOINT_MODEL") or "").strip()
    if not raw:
        return []

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"[llm_selector] FALLBACK_LLM_APIKEY_ENDPOINT_MODEL is not valid JSON ({e}) — ignoring, no fallbacks")
        return []

    if not isinstance(parsed, list):
        print("[llm_selector] FALLBACK_LLM_APIKEY_ENDPOINT_MODEL must be a JSON list — ignoring, no fallbacks")
        return []

    providers = []
    for i, item in enumerate(parsed):
        if not isinstance(item, dict) or not all(k in item and item[k] for k in ("api_key", "base_url", "model")):
            print(f"[llm_selector] fallback provider #{i} is missing api_key/base_url/model — skipped")
            continue
        providers.append(item)

    print(f"[llm_selector] loaded {len(providers)} fallback provider(s)")
    return providers


_FALLBACK_PROVIDERS = _load_fallback_providers()

_primary_client = None
_fallback_clients: dict[int, OpenAI] = {}  # built lazily, one per provider index


def _get_primary() -> OpenAI:
    global _primary_client
    if OpenAI is None:
        raise RuntimeError("Neither langfuse nor openai SDK is installed. Please install openai or langfuse.")
    api_key, base_url, _ = _resolve_primary_config()
    if not api_key:
        raise RuntimeError("No LLM API key configured (set OPENAI_API_KEY, GROQ_API_KEY, GEMINI_API_KEY, or PRIMARY_LLM_API_KEY in .env)")
    if _primary_client is None:
        _primary_client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            max_retries=0,  # Do not retry 502/down endpoints; immediately try fallback
            timeout=PRIMARY_TIMEOUT,
        )
    return _primary_client


def _get_fallback_client(index: int) -> OpenAI:
    if OpenAI is None:
        raise RuntimeError("Neither langfuse nor openai SDK is installed. Please install openai or langfuse.")
    if index not in _fallback_clients:
        provider = _FALLBACK_PROVIDERS[index]
        _fallback_clients[index] = OpenAI(
            api_key=provider["api_key"],
            base_url=provider["base_url"],
            max_retries=0,
            timeout=FALLBACK_TIMEOUT,
        )
    return _fallback_clients[index]


def create_chat_completion(*, messages, **kwargs):
    """Drop-in replacement for `client.chat.completions.create(...)`.
    Pass everything you'd normally pass (response_format, temperature,
    etc.) as keyword arguments. Tries the primary provider first, then
    each configured fallback in order, returning the first success.

    `extra_body` is treated as PRIMARY-ONLY and stripped before trying
    any fallback. It exists for provider-specific parameters (e.g. a
    self-hosted vLLM/SGLang server's `chat_template_kwargs`) that a
    different provider (Gemini, OpenAI, etc.) won't recognize and will
    outright reject the request over — forwarding it blindly to every
    fallback defeats the whole purpose of having a fallback.
    """
    global _DISCOVERED_PRIMARY_MODEL
    last_error: Exception | None = None

    try:
        _, _, model = _resolve_primary_config()
        client = _get_primary()
        if not os.environ.get("PRIMARY_LLM_MODEL") and not _DISCOVERED_PRIMARY_MODEL:
            model = _probe_and_discover_primary_model(client, model)
        return client.chat.completions.create(model=model, messages=messages, **kwargs)
    except Exception as e:
        last_error = e
        err_str = str(e)
        alias_match = re.search(
            r'(?:valid aliases|valid models?|available models?|did you mean):\s*[`\'"]?([a-zA-Z0-9_\-\.\/]+)',
            err_str,
            re.IGNORECASE,
        )
        if alias_match:
            discovered = alias_match.group(1).strip("`'\".,")
            if discovered and discovered != model:
                print(f"[llm_selector] Detected valid model alias '{discovered}' from endpoint error response. Auto-healing primary model and retrying...")
                _DISCOVERED_PRIMARY_MODEL = discovered
                try:
                    return client.chat.completions.create(model=discovered, messages=messages, **kwargs)
                except Exception as retry_err:
                    print(f"[llm_selector] Primary retry with discovered model '{discovered}' failed: {retry_err!r}")
                    last_error = retry_err

        if _FALLBACK_PROVIDERS:
            print(f"[llm_selector] primary endpoint failed ({last_error!r}) — trying {len(_FALLBACK_PROVIDERS)} fallback(s)")

    fallback_kwargs = {k: v for k, v in kwargs.items() if k != "extra_body"}

    for i, provider in enumerate(_FALLBACK_PROVIDERS):
        provider_model = str(provider.get("model", "")).strip()
        provider_url = str(provider.get("base_url", "")).strip()
        if "generativelanguage.googleapis.com" in provider_url and provider_model.startswith("models/"):
            provider_model = provider_model.removeprefix("models/")

        try:
            client = _get_fallback_client(i)
            return client.chat.completions.create(model=provider_model, messages=messages, **fallback_kwargs)
        except Exception as e:
            last_error = e
            if "response_format" in fallback_kwargs:
                try:
                    no_rf = {k: v for k, v in fallback_kwargs.items() if k != "response_format"}
                    client = _get_fallback_client(i)
                    return client.chat.completions.create(model=provider_model, messages=messages, **no_rf)
                except Exception as rf_err:
                    last_error = rf_err
            print(f"[llm_selector] fallback provider #{i} ({provider_url}) failed ({last_error!r})")
            continue

    print("[llm_selector] ALL providers (primary + every fallback) failed")
    raise last_error
