from __future__ import annotations

import asyncio
import logging
import math
import secrets
import time
from pathlib import Path
from typing import Any

import httpx
import tiktoken
import yaml
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse, Response, StreamingResponse

_enc = tiktoken.get_encoding("o200k_base")

ANYLLM_BASE_URL = "http://anyllm:8000"
ANYLLM_KEY = ""
ROUTER_SHARED_KEY = ""
ROUTER_DEFAULT_USER = "openclaw-local"
ROUTER_MODEL_MAP_FILE = "/app/models.yml"
PRICING_CACHE_TTL_SEC = 60
REQUEST_TIMEOUT_SEC = 240.0

logger = logging.getLogger("cost_router")
app = FastAPI(title="Local Cost Router", version="0.1.0")

_pricing_cache: dict[str, tuple[float, float]] = {}
_pricing_cache_expires_at = 0.0
_pricing_cache_lock = asyncio.Lock()

_model_aliases_cache: dict[str, dict[str, Any]] = {}
_model_aliases_mtime = -1.0

TIER_ORDER = ("SIMPLE", "MEDIUM", "COMPLEX", "REASONING")
VALID_TIERS = set(TIER_ORDER)
TIER_RANK = {tier: index for index, tier in enumerate(TIER_ORDER)}

REASONING_KEYWORDS = (
    "reason step by step",
    "step by step",
    "prove",
    "proof",
    "formalize",
    "theorem",
    "derive",
    "chain of thought",
    "reasoning",
)
CODE_KEYWORDS = (
    "code",
    "function",
    "class",
    "algorithm",
    "refactor",
    "typescript",
    "python",
    "javascript",
    "rust",
    "golang",
    "bug",
    "debug",
    "stack trace",
)
COMPLEX_KEYWORDS = (
    "architecture",
    "distributed",
    "production",
    "migration",
    "optimize",
    "performance",
    "design doc",
    "tradeoff",
    "multi-step",
    "tool use",
)
SIMPLE_KEYWORDS = (
    "summarize",
    "briefly",
    "quickly",
    "simple answer",
    "short answer",
    "one sentence",
    "yes or no",
    "what is",
)


def _normalize_model_key(model_key: str) -> str:
    """
    Normalize model identifiers for better OpenClaw/Any-LLM compatibility.

    - Accept optional `anyllm/` transport prefix used by some OpenClaw configs.
    - Map `google:<model>` to Any-LLM's gateway provider id `gemini:<model>`.
    """
    value = (model_key or "").strip()
    if not value:
        return value
    if value.lower().startswith("anyllm/"):
        value = value.split("/", 1)[1].strip()
    if ":" in value:
        provider, model = value.split(":", 1)
        if provider.strip().lower() == "google":
            value = f"gemini:{model.strip()}"
    return value


def _load_env() -> None:
    global ANYLLM_BASE_URL
    global ANYLLM_KEY
    global ROUTER_SHARED_KEY
    global ROUTER_DEFAULT_USER
    global ROUTER_MODEL_MAP_FILE
    global PRICING_CACHE_TTL_SEC
    global REQUEST_TIMEOUT_SEC

    import os

    ANYLLM_BASE_URL = os.getenv("ANYLLM_BASE_URL", ANYLLM_BASE_URL).rstrip("/")
    ANYLLM_KEY = os.getenv("ANYLLM_KEY", ANYLLM_KEY)
    ROUTER_SHARED_KEY = os.getenv("ROUTER_SHARED_KEY", ROUTER_SHARED_KEY)
    ROUTER_DEFAULT_USER = os.getenv("ROUTER_DEFAULT_USER", ROUTER_DEFAULT_USER)
    ROUTER_MODEL_MAP_FILE = os.getenv("ROUTER_MODEL_MAP_FILE", ROUTER_MODEL_MAP_FILE)
    PRICING_CACHE_TTL_SEC = int(os.getenv("PRICING_CACHE_TTL_SEC", str(PRICING_CACHE_TTL_SEC)))
    REQUEST_TIMEOUT_SEC = float(os.getenv("REQUEST_TIMEOUT_SEC", str(REQUEST_TIMEOUT_SEC)))


def _extract_bearer_token(request: Request) -> str | None:
    for header_name in ("Authorization", "X-AnyLLM-Key"):
        header_value = request.headers.get(header_name)
        if header_value and header_value.startswith("Bearer "):
            return header_value[7:]
    return None


def _require_router_auth(request: Request) -> None:
    if not ROUTER_SHARED_KEY:
        return

    token = _extract_bearer_token(request)
    if token is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Bearer token for local router.",
        )

    if not secrets.compare_digest(token, ROUTER_SHARED_KEY):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid local router key.",
        )


def _normalize_tier(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    tier = value.strip().upper()
    if tier in VALID_TIERS:
        return tier
    return None


def _normalize_tier_list(raw_tier_list: Any) -> list[str]:
    if not isinstance(raw_tier_list, list):
        return []
    normalized: list[str] = []
    for item in raw_tier_list:
        tier = _normalize_tier(item)
        if tier and tier not in normalized:
            normalized.append(tier)
    return normalized


def _normalize_candidates(raw_candidates: Any) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    if not isinstance(raw_candidates, list):
        return candidates

    for item in raw_candidates:
        if isinstance(item, str):
            normalized_model = _normalize_model_key(item)
            if normalized_model:
                candidates.append({"model": normalized_model})
            continue

        if isinstance(item, dict) and isinstance(item.get("model"), str):
            normalized_model = _normalize_model_key(item["model"])
            if not normalized_model:
                continue
            normalized = {"model": normalized_model}
            if "input_price_per_million" in item:
                normalized["input_price_per_million"] = float(item["input_price_per_million"])
            if "output_price_per_million" in item:
                normalized["output_price_per_million"] = float(item["output_price_per_million"])
            candidates.append(normalized)

    return candidates


def _parse_alias_tiers(raw_alias_config: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    raw_tiers = raw_alias_config.get("tiers")
    if not isinstance(raw_tiers, dict):
        return {}

    parsed_tiers: dict[str, list[dict[str, Any]]] = {}
    for raw_tier_name, raw_candidates in raw_tiers.items():
        tier = _normalize_tier(raw_tier_name)
        if not tier:
            continue
        candidates = _normalize_candidates(raw_candidates)
        if candidates:
            parsed_tiers[tier] = candidates

    return parsed_tiers


def _flatten_tier_candidates(tiers: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    flattened: list[dict[str, Any]] = []
    seen_models: set[str] = set()
    for tier in TIER_ORDER:
        for candidate in tiers.get(tier, []):
            model_key = candidate["model"]
            if model_key in seen_models:
                continue
            seen_models.add(model_key)
            flattened.append(dict(candidate))
    return flattened


def _load_model_aliases() -> dict[str, dict[str, Any]]:
    global _model_aliases_cache
    global _model_aliases_mtime

    config_path = Path(ROUTER_MODEL_MAP_FILE)
    if not config_path.exists():
        raise RuntimeError(f"Model map file not found: {config_path}")

    file_mtime = config_path.stat().st_mtime
    if _model_aliases_cache and _model_aliases_mtime == file_mtime:
        return _model_aliases_cache

    with config_path.open("r", encoding="utf-8") as file:
        raw_config = yaml.safe_load(file) or {}

    raw_models = raw_config.get("models", {})
    if not isinstance(raw_models, dict):
        raise RuntimeError("Model map file must contain a top-level 'models' mapping.")

    parsed: dict[str, dict[str, Any]] = {}
    for alias, raw_alias_config in raw_models.items():
        if not isinstance(alias, str) or not isinstance(raw_alias_config, dict):
            continue

        candidates = _normalize_candidates(raw_alias_config.get("candidates", []))
        tiers = _parse_alias_tiers(raw_alias_config)

        if not candidates and tiers:
            candidates = _flatten_tier_candidates(tiers)

        if not candidates and not tiers:
            continue

        default_tier = _normalize_tier(raw_alias_config.get("default_tier")) or "MEDIUM"

        parsed[alias] = {
            "description": raw_alias_config.get("description", ""),
            "default_output_tokens": int(raw_alias_config.get("default_output_tokens", 700)),
            "candidates": candidates,
            "tiers": tiers,
            "default_tier": default_tier,
            "fallback_tiers_on_failure": bool(raw_alias_config.get("fallback_tiers_on_failure", True)),
            "tier_fallback_order": _normalize_tier_list(raw_alias_config.get("tier_fallback_order", [])),
        }

    _model_aliases_cache = parsed
    _model_aliases_mtime = file_mtime
    return _model_aliases_cache


def _estimate_prompt_tokens(request_body: dict[str, Any]) -> int:
    import json as _json

    tokens = 0
    messages = request_body.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, dict):
                continue
            tokens += 4  # role + formatting overhead per message

            content = message.get("content")
            if isinstance(content, str):
                tokens += len(_enc.encode(content))
                continue

            if isinstance(content, list):
                for item in content:
                    if isinstance(item, str):
                        tokens += len(_enc.encode(item))
                    elif isinstance(item, dict):
                        for text_key in ("text", "content", "input", "output"):
                            value = item.get(text_key)
                            if isinstance(value, str):
                                tokens += len(_enc.encode(value))

    tools = request_body.get("tools")
    if isinstance(tools, list) and tools:
        tokens += len(_enc.encode(_json.dumps(tools)))

    return max(1, tokens)


def _estimate_output_tokens(request_body: dict[str, Any], alias_default: int) -> int:
    max_completion_tokens = request_body.get("max_completion_tokens")
    if isinstance(max_completion_tokens, int) and max_completion_tokens > 0:
        return max_completion_tokens

    max_tokens = request_body.get("max_tokens")
    if isinstance(max_tokens, int) and max_tokens > 0:
        return max_tokens

    return max(1, alias_default)


def _extract_request_text(messages: Any) -> str:
    if not isinstance(messages, list):
        return ""

    parts: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue

        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
            continue

        if isinstance(content, list):
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    for text_key in ("text", "content", "input", "output"):
                        value = item.get(text_key)
                        if isinstance(value, str):
                            parts.append(value)

    return "\n".join(parts).lower()


def _keyword_hits(text: str, keywords: tuple[str, ...]) -> int:
    if not text:
        return 0
    return sum(1 for keyword in keywords if keyword in text)


def _has_structured_output(response_format: Any) -> bool:
    if isinstance(response_format, dict):
        response_type = response_format.get("type")
        if isinstance(response_type, str) and response_type in {"json_schema", "json_object"}:
            return True
        if "json_schema" in response_format:
            return True
    return False


def _classify_request_tier(
    request_body: dict[str, Any],
    default_output_tokens: int,
    default_tier: str = "MEDIUM",
) -> str:
    prompt_tokens = _estimate_prompt_tokens(request_body)
    output_tokens = _estimate_output_tokens(request_body, default_output_tokens)
    request_text = _extract_request_text(request_body.get("messages"))

    reasoning_hits = _keyword_hits(request_text, REASONING_KEYWORDS)
    code_hits = _keyword_hits(request_text, CODE_KEYWORDS)
    complex_hits = _keyword_hits(request_text, COMPLEX_KEYWORDS)
    simple_hits = _keyword_hits(request_text, SIMPLE_KEYWORDS)

    tools = request_body.get("tools")
    has_tools = isinstance(tools, list) and len(tools) > 0
    has_structured_output = _has_structured_output(request_body.get("response_format"))

    if reasoning_hits >= 2:
        return "REASONING"
    if reasoning_hits >= 1 and (has_tools or prompt_tokens >= 1200 or output_tokens >= 1200):
        return "REASONING"

    if prompt_tokens >= 3000 or output_tokens >= 5000:
        return "COMPLEX"
    if has_tools and (prompt_tokens >= 600 or output_tokens >= 800):
        return "COMPLEX"
    if has_structured_output and (prompt_tokens >= 500 or code_hits >= 1 or complex_hits >= 1):
        return "COMPLEX"
    if code_hits >= 2 or complex_hits >= 2:
        return "COMPLEX"

    if (
        simple_hits >= 1
        and reasoning_hits == 0
        and code_hits == 0
        and complex_hits == 0
        and not has_tools
        and not has_structured_output
        and prompt_tokens <= 350
        and output_tokens <= 800
    ):
        return "SIMPLE"
    if (
        prompt_tokens <= 150
        and output_tokens <= 400
        and not has_tools
        and not has_structured_output
        and reasoning_hits == 0
        and complex_hits == 0
    ):
        return "SIMPLE"

    return default_tier if default_tier in VALID_TIERS else "MEDIUM"


def _nearest_available_tier(selected_tier: str, available_tiers: set[str]) -> str:
    if selected_tier in available_tiers:
        return selected_tier

    selected_rank = TIER_RANK.get(selected_tier, TIER_RANK["MEDIUM"])
    for delta in range(1, len(TIER_ORDER)):
        higher_rank = selected_rank + delta
        if higher_rank < len(TIER_ORDER):
            higher_tier = TIER_ORDER[higher_rank]
            if higher_tier in available_tiers:
                return higher_tier

        lower_rank = selected_rank - delta
        if lower_rank >= 0:
            lower_tier = TIER_ORDER[lower_rank]
            if lower_tier in available_tiers:
                return lower_tier

    for tier in TIER_ORDER:
        if tier in available_tiers:
            return tier

    return "MEDIUM"


def _default_tier_attempt_order(selected_tier: str, available_tiers: set[str]) -> list[str]:
    selected_rank = TIER_RANK[selected_tier]
    order = [selected_tier]

    # Prefer moving up in capability first, then down if needed.
    for tier in TIER_ORDER:
        if tier in available_tiers and TIER_RANK[tier] > selected_rank:
            order.append(tier)

    for tier in reversed(TIER_ORDER):
        if tier in available_tiers and TIER_RANK[tier] < selected_rank:
            order.append(tier)

    return order


def _tier_attempt_order(
    selected_tier: str,
    available_tiers: set[str],
    fallback_tiers_on_failure: bool,
    configured_tier_order: list[str],
) -> list[str]:
    if not fallback_tiers_on_failure:
        return [selected_tier]

    default_order = _default_tier_attempt_order(selected_tier, available_tiers)
    if not configured_tier_order:
        return default_order

    order = [selected_tier]
    for tier in configured_tier_order:
        if tier != selected_tier and tier in available_tiers and tier not in order:
            order.append(tier)

    for tier in default_order:
        if tier not in order:
            order.append(tier)

    return order


def _candidate_price(
    candidate: dict[str, Any],
    pricing_map: dict[str, tuple[float, float]],
) -> tuple[float, float] | None:
    input_override = candidate.get("input_price_per_million")
    output_override = candidate.get("output_price_per_million")
    if isinstance(input_override, (int, float)) and isinstance(output_override, (int, float)):
        return float(input_override), float(output_override)

    return pricing_map.get(candidate["model"])


def _candidate_score(
    candidate: dict[str, Any],
    pricing_map: dict[str, tuple[float, float]],
    input_tokens: int,
    output_tokens: int,
) -> float:
    prices = _candidate_price(candidate, pricing_map)
    if prices is None:
        return math.inf

    input_per_million, output_per_million = prices
    input_cost = (input_tokens / 1_000_000.0) * input_per_million
    output_cost = (output_tokens / 1_000_000.0) * output_per_million
    return input_cost + output_cost


async def _fetch_pricing_map() -> dict[str, tuple[float, float]]:
    global _pricing_cache
    global _pricing_cache_expires_at

    now = time.time()
    if _pricing_cache and now < _pricing_cache_expires_at:
        return _pricing_cache

    async with _pricing_cache_lock:
        now = time.time()
        if _pricing_cache and now < _pricing_cache_expires_at:
            return _pricing_cache

        headers: dict[str, str] = {}
        if ANYLLM_KEY:
            headers["Authorization"] = f"Bearer {ANYLLM_KEY}"

        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.get(f"{ANYLLM_BASE_URL}/v1/pricing", headers=headers)
            response.raise_for_status()
            pricing_rows = response.json()

            parsed: dict[str, tuple[float, float]] = {}
            if isinstance(pricing_rows, list):
                for row in pricing_rows:
                    if not isinstance(row, dict):
                        continue
                    model_key = row.get("model_key")
                    input_price = row.get("input_price_per_million")
                    output_price = row.get("output_price_per_million")
                    if isinstance(model_key, str) and isinstance(input_price, (int, float)) and isinstance(
                        output_price, (int, float)
                    ):
                        parsed[model_key] = (float(input_price), float(output_price))

            _pricing_cache = parsed
            _pricing_cache_expires_at = now + PRICING_CACHE_TTL_SEC
            return _pricing_cache
        except Exception as exc:
            logger.warning("Failed to refresh pricing map from Any-LLM: %s", exc)
            # Keep stale cache if available, otherwise empty map.
            _pricing_cache_expires_at = now + min(PRICING_CACHE_TTL_SEC, 10)
            return _pricing_cache


def _resolve_alias_config(
    requested_model: str,
    aliases: dict[str, dict[str, Any]],
) -> tuple[str | None, dict[str, Any], int]:
    alias_config = aliases.get(requested_model)
    if alias_config is not None:
        return requested_model, alias_config, int(alias_config["default_output_tokens"])

    normalized_requested_model = _normalize_model_key(requested_model)
    if ":" in normalized_requested_model or "/" in normalized_requested_model:
        direct_config = {
            "description": "Direct provider:model route",
            "default_output_tokens": 700,
            "candidates": [{"model": normalized_requested_model}],
            "tiers": {},
            "default_tier": "MEDIUM",
            "fallback_tiers_on_failure": False,
            "tier_fallback_order": [],
        }
        return None, direct_config, 700

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"Unknown model alias '{requested_model}'. Use /v1/models to list aliases.",
    )


def _build_upstream_headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if ANYLLM_KEY:
        headers["Authorization"] = f"Bearer {ANYLLM_KEY}"
    return headers


def _sorted_candidates(
    candidates: list[dict[str, Any]],
    pricing_map: dict[str, tuple[float, float]],
    request_body: dict[str, Any],
    default_output_tokens: int,
) -> list[dict[str, Any]]:
    prompt_tokens = _estimate_prompt_tokens(request_body)
    output_tokens = _estimate_output_tokens(request_body, default_output_tokens)

    scored: list[tuple[int, float, int, dict[str, Any]]] = []
    for index, candidate in enumerate(candidates):
        score = _candidate_score(candidate, pricing_map, prompt_tokens, output_tokens)
        # Rank priced candidates first; preserve configured order for ties and unpriced entries.
        missing_pricing = 1 if math.isinf(score) else 0
        normalized_score = score if not math.isinf(score) else 0.0
        scored.append((missing_pricing, normalized_score, index, candidate))

    scored.sort(key=lambda item: (item[0], item[1], item[2]))
    return [item[3] for item in scored]


def _build_candidate_chain(
    alias_config: dict[str, Any],
    request_body: dict[str, Any],
    pricing_map: dict[str, tuple[float, float]],
    default_output_tokens: int,
) -> tuple[list[dict[str, Any]], str | None, str | None, list[str]]:
    tiers: dict[str, list[dict[str, Any]]] = alias_config.get("tiers", {})
    if not tiers:
        ordered_candidates = []
        for candidate in _sorted_candidates(alias_config["candidates"], pricing_map, request_body, default_output_tokens):
            decorated = dict(candidate)
            decorated["_tier"] = "DIRECT"
            ordered_candidates.append(decorated)
        return ordered_candidates, None, None, []

    default_tier = _normalize_tier(alias_config.get("default_tier")) or "MEDIUM"
    classified_tier = _classify_request_tier(request_body, default_output_tokens, default_tier)
    available_tiers = set(tiers.keys())
    effective_tier = _nearest_available_tier(classified_tier, available_tiers)
    attempt_order = _tier_attempt_order(
        selected_tier=effective_tier,
        available_tiers=available_tiers,
        fallback_tiers_on_failure=bool(alias_config.get("fallback_tiers_on_failure", True)),
        configured_tier_order=alias_config.get("tier_fallback_order", []),
    )

    ordered_candidates: list[dict[str, Any]] = []
    seen_models: set[str] = set()
    for tier in attempt_order:
        tier_candidates = _sorted_candidates(tiers[tier], pricing_map, request_body, default_output_tokens)
        for candidate in tier_candidates:
            model_key = candidate["model"]
            if model_key in seen_models:
                continue
            seen_models.add(model_key)
            decorated = dict(candidate)
            decorated["_tier"] = tier
            ordered_candidates.append(decorated)

    return ordered_candidates, classified_tier, effective_tier, attempt_order


def _response_headers(
    alias: str | None,
    routed_model: str,
    selected_tier: str | None = None,
    effective_tier: str | None = None,
    routed_tier: str | None = None,
) -> dict[str, str]:
    headers = {"x-routed-model": routed_model}
    if alias:
        headers["x-router-alias"] = alias
    if selected_tier:
        headers["x-router-selected-tier"] = selected_tier
    if effective_tier:
        headers["x-router-effective-tier"] = effective_tier
    if routed_tier:
        headers["x-router-routed-tier"] = routed_tier
    return headers


def _is_empty_chat_response(upstream: httpx.Response) -> bool:
    content_type = upstream.headers.get("content-type", "").lower()
    if "application/json" not in content_type:
        return False

    try:
        payload = upstream.json()
    except Exception:
        return False

    if not isinstance(payload, dict):
        return False

    choices = payload.get("choices")
    return isinstance(choices, list) and len(choices) == 0


@app.on_event("startup")
async def startup() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    _load_env()
    aliases = _load_model_aliases()
    tiered_aliases = sum(1 for alias in aliases.values() if alias.get("tiers"))
    logger.info(
        "Loaded %d model aliases (%d tiered) from %s",
        len(aliases),
        tiered_aliases,
        ROUTER_MODEL_MAP_FILE,
    )


@app.get("/health")
async def health() -> dict[str, Any]:
    aliases = _load_model_aliases()
    tiered_aliases = sum(1 for alias in aliases.values() if alias.get("tiers"))
    return {
        "status": "ok",
        "anyllm_base_url": ANYLLM_BASE_URL,
        "router_auth_enabled": bool(ROUTER_SHARED_KEY),
        "aliases_count": len(aliases),
        "tiered_aliases_count": tiered_aliases,
    }


async def _forward_anyllm(
    model_key: str,
    forwarded_body: dict[str, Any],
    is_stream: bool,
    resp_headers: dict[str, str],
) -> tuple[Response | None, dict[str, Any] | None]:
    """Forward a request through the Any-LLM gateway (Docker mode)."""
    upstream_headers = _build_upstream_headers()

    if is_stream:
        client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SEC)
        try:
            upstream = await client.send(
                client.build_request(
                    "POST",
                    f"{ANYLLM_BASE_URL}/v1/chat/completions",
                    headers=upstream_headers,
                    json=forwarded_body,
                ),
                stream=True,
            )
        except Exception as exc:
            await client.aclose()
            return None, {"model": model_key, "error": str(exc)}

        if upstream.status_code >= 400:
            error_body = (await upstream.aread()).decode("utf-8", errors="replace")
            await upstream.aclose()
            await client.aclose()
            return None, {"model": model_key, "status_code": upstream.status_code, "detail": error_body[:500]}

        async def stream_bytes() -> Any:
            try:
                async for chunk in upstream.aiter_bytes():
                    yield chunk
            finally:
                await upstream.aclose()
                await client.aclose()

        return StreamingResponse(
            stream_bytes(),
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "text/event-stream"),
            headers=resp_headers,
        ), None

    # Non-streaming
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SEC) as client:
            upstream = await client.post(
                f"{ANYLLM_BASE_URL}/v1/chat/completions",
                headers=upstream_headers,
                json=forwarded_body,
            )
    except Exception as exc:
        return None, {"model": model_key, "error": str(exc)}

    if upstream.status_code >= 400:
        return None, {"model": model_key, "status_code": upstream.status_code, "detail": upstream.text[:500]}

    if _is_empty_chat_response(upstream):
        return None, {
            "model": model_key,
            "status_code": upstream.status_code,
            "detail": "Upstream returned empty choices; trying fallback candidate.",
        }

    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "application/json"),
        headers=resp_headers,
    ), None


@app.get("/v1/models")
async def list_models(request: Request) -> dict[str, Any]:
    _require_router_auth(request)
    aliases = _load_model_aliases()
    pricing_map = await _fetch_pricing_map()

    model_data: list[dict[str, Any]] = []
    created = int(time.time())
    for alias_name, alias_config in aliases.items():
        candidates: list[dict[str, Any]] = []
        for candidate in alias_config["candidates"]:
            pricing = _candidate_price(candidate, pricing_map)
            candidate_info: dict[str, Any] = {"model": candidate["model"]}
            if pricing is not None:
                candidate_info["input_price_per_million"] = pricing[0]
                candidate_info["output_price_per_million"] = pricing[1]
            candidates.append(candidate_info)

        metadata: dict[str, Any] = {
            "description": alias_config.get("description", ""),
            "routing_mode": "tiered_cost" if alias_config.get("tiers") else "cost_only",
            "candidates": candidates,
        }

        tiers = alias_config.get("tiers", {})
        if tiers:
            tier_metadata: dict[str, list[dict[str, Any]]] = {}
            for tier_name, tier_candidates in tiers.items():
                tier_data: list[dict[str, Any]] = []
                for candidate in tier_candidates:
                    pricing = _candidate_price(candidate, pricing_map)
                    tier_candidate: dict[str, Any] = {"model": candidate["model"]}
                    if pricing is not None:
                        tier_candidate["input_price_per_million"] = pricing[0]
                        tier_candidate["output_price_per_million"] = pricing[1]
                    tier_data.append(tier_candidate)
                tier_metadata[tier_name] = tier_data

            metadata["default_tier"] = alias_config.get("default_tier")
            metadata["fallback_tiers_on_failure"] = bool(alias_config.get("fallback_tiers_on_failure", True))
            metadata["tier_fallback_order"] = alias_config.get("tier_fallback_order", [])
            metadata["tiers"] = tier_metadata

        model_data.append(
            {
                "id": alias_name,
                "object": "model",
                "created": created,
                "owned_by": "local-cost-router",
                "metadata": metadata,
            }
        )

    return {"object": "list", "data": model_data}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Response:
    _require_router_auth(request)

    try:
        request_body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid JSON body: {exc}") from exc

    if not isinstance(request_body, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Request body must be a JSON object.")

    requested_model = request_body.get("model")
    if not isinstance(requested_model, str) or not requested_model:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Request must include a 'model' string.")

    aliases = _load_model_aliases()
    alias_name, alias_config, default_output_tokens = _resolve_alias_config(requested_model, aliases)
    pricing_map = await _fetch_pricing_map()
    ordered_candidates, selected_tier, effective_tier, tier_attempt_order = _build_candidate_chain(
        alias_config=alias_config,
        request_body=request_body,
        pricing_map=pricing_map,
        default_output_tokens=default_output_tokens,
    )

    request_body = dict(request_body)
    if "user" not in request_body and ROUTER_DEFAULT_USER:
        request_body["user"] = ROUTER_DEFAULT_USER

    is_stream = bool(request_body.get("stream"))

    errors: list[dict[str, Any]] = []

    for candidate in ordered_candidates:
        model_key = candidate["model"]
        routed_tier = candidate.get("_tier")
        forwarded_body = dict(request_body)
        forwarded_body["model"] = model_key
        resp_headers = _response_headers(alias_name, model_key, selected_tier, effective_tier, routed_tier)
        response, error = await _forward_anyllm(model_key, forwarded_body, is_stream, resp_headers)

        if error is not None:
            error["tier"] = routed_tier
            errors.append(error)
            continue

        return response

    return JSONResponse(
        status_code=status.HTTP_502_BAD_GATEWAY,
        content={
            "error": "All candidate models failed.",
            "requested_model": requested_model,
            "selected_tier": selected_tier,
            "effective_tier": effective_tier,
            "tier_attempt_order": tier_attempt_order,
            "attempts": errors,
        },
    )
