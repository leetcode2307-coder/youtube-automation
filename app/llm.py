import json
from typing import Any, Optional, Type
from pydantic import BaseModel
from langchain_groq import ChatGroq
from langchain_core.messages import BaseMessage, SystemMessage, HumanMessage
from app.config import settings
from app.logging_config import logger


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
                scenes=[
                    {
                        "scene_number": 1,
                        "duration_seconds": 6,
                        "narration": "Building AI agents today feels like writing custom API drivers for every single database and tool.",
                        "visual_prompt": "Cinematic 3D render of tangled glowing data cables connecting haphazardly to an AI brain, dark background, neon accents, 9:16 vertical video",
                        "on_screen_text": "The Agent Integration Problem",
                        "export_filename": "scene_01.mp4"
                    },
                    {
                        "scene_number": 2,
                        "duration_seconds": 8,
                        "narration": "That's why Model Context Protocol, or MCP, is becoming the USB-C standard for LLMs.",
                        "visual_prompt": "Futuristic sleek glowing USB-C connector snapping effortlessly into a crystalline AI core, seamless connection, 9:16 vertical video",
                        "on_screen_text": "MCP = USB-C for AI",
                        "export_filename": "scene_02.mp4"
                    },
                    {
                        "scene_number": 3,
                        "duration_seconds": 8,
                        "narration": "Instead of writing bespoke code, your agent queries an MCP server that exposes tools, prompts, and resources uniformly.",
                        "visual_prompt": "Diagram animation showing an AI Agent connecting uniformly to database, github, and web tools via clean modular blocks, 9:16 vertical video",
                        "on_screen_text": "Standardized JSON-RPC Tools",
                        "export_filename": "scene_03.mp4"
                    },
                    {
                        "scene_number": 4,
                        "duration_seconds": 8,
                        "narration": "This decouples your agent runtime from the tools it uses, giving you instant modularity and sandboxed security.",
                        "visual_prompt": "Clean glowing security barrier with encrypted data flowing smoothly between isolated microservice layers, 9:16 vertical video",
                        "on_screen_text": "Sandboxed & Decoupled",
                        "export_filename": "scene_04.mp4"
                    },
                    {
                        "scene_number": 5,
                        "duration_seconds": 6,
                        "narration": "Are you already using MCP in production? Drop your thoughts below and subscribe for daily AI engineering breakdowns!",
                        "visual_prompt": "Dynamic motion graphics with animated subscribe icon, code terminal, and modern minimalist typography, 9:16 vertical video",
                        "on_screen_text": "Subscribe for Daily AI Engineering",
                        "export_filename": "scene_05.mp4"
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


def get_llm(force_mock: bool = False):
    """Factory to retrieve Groq LLM or MockLLM if API key is unconfigured."""
    if force_mock or not settings.GROQ_API_KEY or "your_groq_api_key" in settings.GROQ_API_KEY:
        logger.info("Using MockLLM (no valid Groq API key configured).")
        return MockLLM()

    try:
        llm = ChatGroq(
            model=settings.GROQ_MODEL,
            api_key=settings.GROQ_API_KEY,
            temperature=0.7,
            max_retries=settings.MAX_RETRIES
        )
        return llm
    except Exception as e:
        logger.warning("Failed to initialize ChatGroq, falling back to MockLLM: %s", e)
        return MockLLM()
