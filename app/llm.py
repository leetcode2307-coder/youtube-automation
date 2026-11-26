import json
import re
from typing import Any, Optional, Tuple, Type, TypeVar
from pydantic import BaseModel, ValidationError
from langchain_groq import ChatGroq
from langchain_core.messages import BaseMessage, SystemMessage, HumanMessage
from app.config import settings
from app.logging_config import logger

T = TypeVar("T", bound=BaseModel)

FALLBACK_GROQ_MODELS: Tuple[str, ...] = (
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
)
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)
_PARAM_TAG_RE = re.compile(r"<parameter=([A-Za-z0-9_]+)>\s*(.*?)\s*</parameter>", re.DOTALL)


class MockLLM:
    """Mock LLM for offline dry runs and test suite execution."""

    def with_structured_output(self, schema: Type[BaseModel]):
        return MockStructuredOutput(schema)

    def invoke(self, messages: Any) -> Any:
        class MockResponse:
            content = "This is a mock AI Engineering response."
        return MockResponse()


class MockStructuredOutput:
    def __init__(self, schema: Type[BaseModel]):
        self.schema = schema

    def invoke(self, messages: Any) -> BaseModel:
        # Determine schema type and return realistic synthetic mock data
        schema_name = getattr(self.schema, "__name__", "")

        if "Topic" in schema_name:
            return self.schema(
                topic="Model Context Protocol (MCP) in Production Architectures",
                angle="Why standardized agent-to-tool protocols eliminate custom API glue code in multi-agent systems.",
                reasoning="Highly trending AI Engineering standard with immediate developer utility."
            )
        elif "Research" in schema_name:
            return self.schema(
                key_insights=[
                    "MCP standardizes how LLM applications connect to external tools, databases, and prompts.",
                    "Replaces bespoke tool schemas with JSON-RPC 2.0 based client-server architecture.",
                    "Improves security by isolating tool credentials in dedicated MCP host processes."
                ],
                sources=[
                    "https://modelcontextprotocol.io/introduction",
                    "Anthropic MCP Specification (2024-2026)"
                ]
            )
        elif "Script" in schema_name:
            return self.schema(
                hook="Stop hardcoding custom tool connectors for every single AI agent.",
                characters=[
                    {
                        "name": "Aarav (AI Architect)",
                        "role": "Lead Engineer / Host",
                        "visual_description": "Late-20s South Asian male software architect with short dark hair, thin titanium glasses, slate-blue hoodie over white tee, focused demeanor, cool cyan studio rim lighting, photorealistic 9:16 cinema style",
                    },
                    {
                        "name": "Core (MCP Hub)",
                        "role": "Visual AI Metaphor",
                        "visual_description": "Floating translucent crystal cube with glowing cyan and amber fiber-optic circuitry over a matte obsidian desk, crisp reflections, futuristic sci-fi realism",
                    },
                ],
                scenes=[
                    {
                        "scene_number": 1,
                        "duration_seconds": 9,
                        "narration": "Building AI agents today feels like writing fragile custom API drivers for every single database and tool.",
                        "visual_prompt": "Medium close-up 9:16 shot of Aarav (AI Architect) surrounded by tangled glowing cables in a moody dark server studio, camera slow push-in, cyan rim light",
                        "on_screen_text": "The Agent Integration Problem",
                        "export_filename": "scene_01.mp4"
                    },
                    {
                        "scene_number": 2,
                        "duration_seconds": 10,
                        "narration": "That is why Model Context Protocol, or MCP, is rapidly becoming the universal USB-C standard for LLMs.",
                        "visual_prompt": "Dynamic 9:16 tracking shot as Aarav (AI Architect) snaps a sleek glowing USB-C fiber connector into Core (MCP Hub), cables detangling into clean light beams",
                        "on_screen_text": "MCP = USB-C for AI",
                        "export_filename": "scene_02.mp4"
                    },
                    {
                        "scene_number": 3,
                        "duration_seconds": 10,
                        "narration": "Instead of writing bespoke glue code, your agent queries an MCP server that exposes tools, prompts, and resources uniformly.",
                        "visual_prompt": "Vertical 9:16 holographic architecture orbiting Core (MCP Hub), uniform glowing links snapping neatly to database, GitHub, and web tool nodes, smooth camera orbit",
                        "on_screen_text": "Standardized JSON-RPC Tools",
                        "export_filename": "scene_03.mp4"
                    },
                    {
                        "scene_number": 4,
                        "duration_seconds": 9,
                        "narration": "This decouples your runtime for instant modularity and sandboxed security. Subscribe for daily AI engineering breakdowns!",
                        "visual_prompt": "Aarav (AI Architect) smiling confidently beside glowing Core (MCP Hub) behind a clean security shield glass layer, crisp studio lighting, 9:16 vertical video",
                        "on_screen_text": "Subscribe for Daily AI Engineering",
                        "export_filename": "scene_04.mp4"
                    }
                ],
                cta="Subscribe for daily AI Engineering deep dives!"
            )
        elif "Metadata" in schema_name:
            return self.schema(
                title="Stop Hardcoding AI Tools: Why MCP Changes Everything",
                description="Model Context Protocol (MCP) is revolutionizing AI agent architecture by standardizing tool connections. Here is the full engineering breakdown.\n\n#AIEngineering #Agents #MCP #LLM",
                hashtags=["#AIEngineering", "#Agents", "#MCP", "#LLM"],
                tags=["mcp", "model context protocol", "ai engineering", "ai agents", "llm architecture", "anthropic mcp"],
                synthetic_media_disclosure=True
            )

        raise ValueError(f"Unknown mock schema: {schema_name}")


def _resolve_max_tokens(model_name: str, requested_max_tokens: Optional[int] = None) -> int:
    """Resolve safe max_tokens per model family (avoids Qwen 1000 OTPM limits while preventing Llama truncation)."""
    base = requested_max_tokens if requested_max_tokens is not None else settings.GROQ_MAX_TOKENS
    lowered = model_name.lower()
    if "qwen" in lowered:
        # Groq free-tier enforces a strict 1000 OTPM ceiling on qwen/qwen3.8-27b
        return max(256, min(base, 900))
    return max(base, 1800)


def get_llm(
    force_mock: bool = False,
    model_name: Optional[str] = None,
    max_tokens: Optional[int] = None,
):
    """Factory to retrieve Groq LLM or MockLLM if API key is unconfigured."""
    if force_mock or not settings.GROQ_API_KEY or "your_groq_api_key" in settings.GROQ_API_KEY:
        logger.info("Using MockLLM (no valid Groq API key configured).")
        return MockLLM()

    selected_model = (model_name or settings.GROQ_MODEL).strip() or settings.GROQ_MODEL
    resolved_max_tokens = _resolve_max_tokens(selected_model, requested_max_tokens=max_tokens)
    try:
        llm = ChatGroq(
            model=selected_model,
            api_key=settings.GROQ_API_KEY,
            temperature=0.7,
            max_tokens=resolved_max_tokens,
            max_retries=settings.MAX_RETRIES,
        )
        return llm
    except Exception as e:
        logger.warning("Failed to initialize ChatGroq (%s), falling back to MockLLM: %s", selected_model, e)
        return MockLLM()


def _extract_json_payload(raw_text: str) -> Any:
    """Extract and parse JSON from raw LLM text, stripping think blocks, fences, or XML tool wrappers."""
    cleaned = _THINK_BLOCK_RE.sub("", (raw_text or "").strip()).strip()

    # Recover <tool_call><parameter=...>...</parameter> style outputs if emitted verbatim
    param_matches = _PARAM_TAG_RE.findall(cleaned)
    if param_matches and not cleaned.startswith("{"):
        recovered: dict[str, Any] = {}
        for key, raw_val in param_matches:
            val_str = raw_val.strip()
            try:
                recovered[key] = json.loads(val_str)
            except Exception:
                recovered[key] = val_str
        if recovered:
            return recovered

    fence_match = _CODE_FENCE_RE.search(cleaned)
    if fence_match:
        cleaned = fence_match.group(1).strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    for idx, ch in enumerate(cleaned):
        if ch == "{":
            try:
                obj, _ = decoder.raw_decode(cleaned[idx:])
                return obj
            except json.JSONDecodeError:
                continue

    raise ValueError(f"No valid JSON object found in LLM response: {cleaned[:240]}")


def _build_json_prompt(prompt: str, schema: Type[BaseModel]) -> str:
    """Append concise JSON schema contract to support Groq json_mode and raw JSON parsing."""
    schema_json = json.dumps(schema.model_json_schema(), ensure_ascii=False)
    return (
        f"{prompt}\n\n"
        "OUTPUT FORMAT (STRICT JSON ONLY):\n"
        "- Respond ONLY with a single valid JSON object matching this JSON schema (no markdown, no XML, no prose):\n"
        f"{schema_json}"
    )


def _coerce_structured_result(result: Any, schema: Type[T]) -> T:
    """Validate structured output (dict or BaseModel) against schema, unwrapping tool containers if present."""
    if isinstance(result, schema):
        return result
    if isinstance(result, dict):
        if "parameters" in result and isinstance(result["parameters"], dict):
            result = result["parameters"]
        elif "arguments" in result and isinstance(result["arguments"], dict):
            result = result["arguments"]
        return schema.model_validate(result)
    return schema.model_validate(result)


def invoke_structured_output(
    schema: Type[T],
    prompt: str,
    force_mock: bool = False,
    model_name: Optional[str] = None,
    max_tokens: Optional[int] = None,
) -> T:
    """
    Invoke LLM with structured Pydantic output, handling Groq tool_use_failed (400),
    output token limits (429/truncation), and malformed responses via JSON-mode,
    raw JSON recovery, and model fallbacks.
    """
    primary_model = (model_name or settings.GROQ_MODEL).strip() or settings.GROQ_MODEL
    if force_mock or not settings.GROQ_API_KEY or "your_groq_api_key" in settings.GROQ_API_KEY:
        return MockLLM().with_structured_output(schema).invoke(prompt)

    candidate_models = [primary_model]
    for fb in FALLBACK_GROQ_MODELS:
        if fb not in candidate_models:
            candidate_models.append(fb)

    json_prompt = _build_json_prompt(prompt, schema)
    errors: list[str] = []

    for idx, candidate in enumerate(candidate_models):
        llm = get_llm(force_mock=force_mock, model_name=candidate, max_tokens=max_tokens)
        if isinstance(llm, MockLLM):
            return llm.with_structured_output(schema).invoke(prompt)

        # If tests/callers swapped get_llm to a custom non-ChatGroq stub, invoke its standard interface first
        if not isinstance(llm, ChatGroq):
            try:
                raw_out = llm.with_structured_output(schema).invoke(prompt)
                return _coerce_structured_result(raw_out, schema)
            except Exception as exc:
                errors.append(f"{candidate} (custom stub): {exc}")
                continue

        # 1. Prefer json_mode on Groq to avoid server-side XML <tool_call> tool_use_failed (400) errors
        try:
            structured_llm = llm.with_structured_output(schema, method="json_mode")
            raw_out = structured_llm.invoke(json_prompt)
            return _coerce_structured_result(raw_out, schema)
        except Exception as json_mode_exc:
            logger.warning(
                "Structured json_mode failed on model '%s' for %s (%s). Attempting raw JSON recovery...",
                candidate, schema.__name__, json_mode_exc,
            )
            errors.append(f"{candidate} [json_mode]: {json_mode_exc}")

        # 2. Raw text JSON recovery (handles <think> blocks, code fences, or tool wrappers)
        try:
            response = llm.invoke([
                SystemMessage(content="You return strictly valid JSON matching the requested schema. No extra text."),
                HumanMessage(content=json_prompt),
            ])
            content = getattr(response, "content", str(response))
            payload = _extract_json_payload(content if isinstance(content, str) else str(content))
            return _coerce_structured_result(payload, schema)
        except (Exception, ValidationError) as raw_exc:
            logger.warning(
                "Raw JSON recovery failed on model '%s' for %s (%s)%s",
                candidate,
                schema.__name__,
                raw_exc,
                "; switching to fallback model..." if idx + 1 < len(candidate_models) else ".",
            )
            errors.append(f"{candidate} [raw_json]: {raw_exc}")

    raise RuntimeError(
        f"All structured LLM attempts failed for {schema.__name__}: {' | '.join(errors)}"
    )
