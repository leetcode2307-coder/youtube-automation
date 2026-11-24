from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional, TypedDict
from pydantic import BaseModel, Field
from langgraph.graph import StateGraph, START, END

from app.config import settings
from app.llm import get_llm
from app.logging_config import logger
from app.models import ProductionPackage, Scene, SEOMetadata, SlotType
from app.storage import storage


# --- Structured Output Schemas for LLM ---

class TopicOutput(BaseModel):
    topic: str = Field(description="Specific, compelling AI Engineering topic")
    angle: str = Field(description="Unique technical angle or takeaway for software engineers")
    reasoning: str = Field(description="Why this is timely and high-value for YouTube Shorts")


class ResearchOutput(BaseModel):
    key_insights: List[str] = Field(min_length=2, max_length=5, description="Key technical points and takeaways")
    sources: List[str] = Field(default_factory=list, description="Authoritative reference URLs or documentation sources")


class ScriptOutput(BaseModel):
    hook: str = Field(description="High-retention 0-3 second opening hook line")
    scenes: List[Scene] = Field(min_length=3, max_length=7, description="4-6 sequential scenes with durations summing to 30-45s")
    cta: str = Field(description="Closing call to action, e.g. subscribe")


class MetadataOutput(BaseModel):
    title: str = Field(max_length=100, description="Clickable YouTube Shorts title")
    description: str = Field(description="Description with summary and resource notes")
    hashtags: List[str] = Field(min_length=3, max_length=6, description="3-5 hashtags starting with #")
    tags: List[str] = Field(min_length=4, max_length=12, description="Search keyword tags")
    synthetic_media_disclosure: bool = Field(default=True)


# --- LangGraph State Contract ---

class GraphState(TypedDict, total=False):
    run_id: str
    slot: SlotType
    scheduled_at_local: str
    created_at_utc: str
    recent_topics: List[str]
    topic: str
    angle: str
    sources: List[str]
    key_insights: List[str]
    hook: str
    scenes: List[Scene]
    narration_full: str
    total_duration_seconds: int
    cta: str
    metadata: Optional[SEOMetadata]
    flow_prompts_file_content: str
    validation_errors: List[str]
    retry_count: int
    package: Optional[ProductionPackage]
    force_mock: bool


# --- Node Implementations ---

def select_topic_node(state: GraphState) -> Dict[str, Any]:
    logger.info("Executing select_topic_node for run %s (%s)", state.get("run_id"), state.get("slot"))
    llm = get_llm(force_mock=state.get("force_mock", False))
    recent = state.get("recent_topics", [])
    recent_str = "\n".join(f"- {t}" for t in recent[-20:]) if recent else "None (first run)"

    prompt = (
        "You are a Lead AI Systems Engineer and YouTube Shorts strategist.\n"
        "Select ONE compelling, highly technical topic for an AI Engineering YouTube Short.\n"
        "Target Domains: LLMs, RAG, AI Agents, Model Context Protocol (MCP), Inference Optimization, "
        "Vector Databases, LLM Evaluation, Guardrails, AI Infrastructure, or Model Quantization.\n\n"
        f"Recently covered topics (DO NOT DUPLICATE THESE):\n{recent_str}\n\n"
        "Provide a unique topic and technical angle that offers clear, non-trivial engineering value."
    )

    structured_llm = llm.with_structured_output(TopicOutput)
    result: TopicOutput = structured_llm.invoke(prompt)

    logger.info("Selected topic: '%s' (Angle: %s)", result.topic, result.angle)
    return {
        "topic": result.topic,
        "angle": result.angle,
    }


def research_topic_node(state: GraphState) -> Dict[str, Any]:
    logger.info("Executing research_topic_node for topic: %s", state.get("topic"))
    llm = get_llm(force_mock=state.get("force_mock", False))

    prompt = (
        f"You are researching the AI Engineering topic: '{state.get('topic')}'.\n"
        f"Technical angle: {state.get('angle')}\n\n"
        "Extract 2-4 critical engineering takeaways, architectural tradeoffs, or implementation details. "
        "Include 1-3 official documentation links, GitHub repo URLs, or paper citations."
    )

    structured_llm = llm.with_structured_output(ResearchOutput)
    result: ResearchOutput = structured_llm.invoke(prompt)

    return {
        "key_insights": result.key_insights,
        "sources": result.sources,
    }


def draft_script_node(state: GraphState) -> Dict[str, Any]:
    logger.info("Executing draft_script_node (Attempt %d)", state.get("retry_count", 0) + 1)
    llm = get_llm(force_mock=state.get("force_mock", False))

    retry_feedback = ""
    if state.get("validation_errors"):
        retry_feedback = (
            "\nPREVIOUS ATTEMPT FAILED VALIDATION WITH THESE ERRORS:\n"
            + "\n".join(f"- {e}" for e in state["validation_errors"])
            + "\nPlease strictly fix these errors in this generation.\n"
        )

    prompt = (
        "You are writing a script for a 30 to 45 second technical YouTube Short about AI Engineering.\n"
        f"Topic: {state.get('topic')}\n"
        f"Angle: {state.get('angle')}\n"
        f"Key Insights: {', '.join(state.get('key_insights', []))}\n"
        f"{retry_feedback}\n"
        "STRICT CONSTRAINTS:\n"
        "1. Video duration MUST be between 30 and 45 seconds total.\n"
        "2. Break the video into 4 to 6 sequential scenes.\n"
        "3. Each scene duration must be between 5 and 10 seconds.\n"
        "4. The SUM of all scene durations MUST EQUAL between 30 and 45 seconds.\n"
        "5. For each scene, provide:\n"
        "   - narration (clear spoken English)\n"
        "   - on_screen_text (punchy 2-4 word summary)\n"
        "   - visual_prompt (describing cinematic, 9:16 vertical 3D graphics for Google Flow video generation)\n"
        "   - export_filename: 'scene_01.mp4', 'scene_02.mp4', etc.\n"
        "6. High retention hook for scene 1 (0-3s).\n"
        "7. Punchy call to action at the end."
    )

    structured_llm = llm.with_structured_output(ScriptOutput)
    result: ScriptOutput = structured_llm.invoke(prompt)

    total_duration = sum(s.duration_seconds for s in result.scenes)
    full_narration = " ".join(s.narration for s in result.scenes)

    logger.info("Drafted script: %d scenes, %d seconds total duration", len(result.scenes), total_duration)
    return {
        "hook": result.hook,
        "scenes": result.scenes,
        "cta": result.cta,
        "narration_full": full_narration,
        "total_duration_seconds": total_duration,
    }


def generate_metadata_node(state: GraphState) -> Dict[str, Any]:
    logger.info("Executing generate_metadata_node for topic: %s", state.get("topic"))
    llm = get_llm(force_mock=state.get("force_mock", False))

    prompt = (
        f"Create YouTube Shorts SEO metadata for an AI Engineering video.\n"
        f"Topic: {state.get('topic')}\n"
        f"Hook: {state.get('hook')}\n"
        f"Full Narration: {state.get('narration_full')}\n\n"
        "Requirements:\n"
        "- title: Under 100 characters. Clickable, curiosity-inducing yet technically accurate.\n"
        "- description: Engaging summary with technical keywords and source notes.\n"
        "- hashtags: Exactly 3 to 5 relevant hashtags starting with # (e.g. #AIEngineering, #LLM, #RAG).\n"
        "- tags: 5 to 10 YouTube search keyword tags.\n"
        "- synthetic_media_disclosure: true (as video uses AI visual generation)."
    )

    structured_llm = llm.with_structured_output(MetadataOutput)
    result: MetadataOutput = structured_llm.invoke(prompt)

    metadata = SEOMetadata(
        title=result.title,
        description=result.description,
        hashtags=result.hashtags,
        tags=result.tags,
        category_id="28",
        synthetic_media_disclosure=result.synthetic_media_disclosure,
    )

    return {"metadata": metadata}


def create_flow_prompts_node(state: GraphState) -> Dict[str, Any]:
    logger.info("Executing create_flow_prompts_node for run %s", state.get("run_id"))
    run_id = state.get("run_id", "unknown_run")
    slot = state.get("slot", "morning")
    topic = state.get("topic", "")
    total_dur = state.get("total_duration_seconds", 0)
    scenes = state.get("scenes", [])

    lines = [
        "=" * 70,
        f"GOOGLE FLOW VIDEO GENERATION BRIEF - RUN ID: {run_id}",
        f"Slot: {slot.upper()} | Topic: {topic}",
        f"Target Total Duration: {total_dur} seconds | Target Aspect Ratio: 9:16 (Portrait)",
        "=" * 70,
        "",
        "INSTRUCTIONS FOR OPERATOR (GOOGLE AI PRO FLOW):",
        f"1. Open Google Flow and create a new project for Run ID '{run_id}'.",
        "2. For each scene below, generate a vertical (9:16) video clip using the Visual Prompt.",
        "3. Set clip duration to match the target scene duration.",
        f"4. Export each clip to: data/flow_exports/{run_id}/",
        "5. Name each file strictly as specified (e.g. scene_01.mp4, scene_02.mp4, etc.).",
        "",
        "-" * 70,
        "SCENE-BY-SCENE GENERATION PROMPTS:",
        "-" * 70,
    ]

    for s in scenes:
        lines.extend([
            f"\n[SCENE {s.scene_number}] -> Export Filename: {s.export_filename}",
            f"Duration: {s.duration_seconds} seconds",
            f"On-Screen Text: {s.on_screen_text}",
            f"Voiceover Text: \"{s.narration}\"",
            f"FLOW VISUAL PROMPT:\n{s.visual_prompt}",
            "-" * 40,
        ])

    lines.extend([
        "\nPOST-EXPORT VERIFICATION CHECKLIST:",
        f"[ ] All {len(scenes)} clips exported to data/flow_exports/{run_id}/",
        f"[ ] Filenames match exactly: {', '.join(s.export_filename for s in scenes)}",
        "[ ] Optional: place voiceover.mp3 in the folder if using custom audio.",
        "=" * 70,
    ])

    flow_content = "\n".join(lines)
    return {"flow_prompts_file_content": flow_content}


def validate_package_node(state: GraphState) -> Dict[str, Any]:
    logger.info("Executing validate_package_node for run %s", state.get("run_id"))
    errors = []
    scenes = state.get("scenes", [])
    total_duration = sum(s.duration_seconds for s in scenes)

    if not (30 <= total_duration <= 45):
        errors.append(f"Total duration ({total_duration}s) outside allowed 30-45s range.")

    if not (3 <= len(scenes) <= 8):
        errors.append(f"Scene count ({len(scenes)}) must be between 3 and 8 scenes.")

    if not state.get("hook"):
        errors.append("Hook is missing or empty.")

    if not state.get("topic"):
        errors.append("Topic is missing or empty.")

    metadata = state.get("metadata")
    if not metadata or not metadata.title:
        errors.append("Metadata title is missing or empty.")
    elif len(metadata.title) > 100:
        errors.append(f"Metadata title exceeds 100 characters ({len(metadata.title)}).")

    retry_count = state.get("retry_count", 0)

    if errors:
        logger.warning("Validation failed with %d error(s): %s", len(errors), errors)
        return {
            "validation_errors": errors,
            "retry_count": retry_count + 1,
        }

    # All valid -> Assemble final ProductionPackage
    pkg = ProductionPackage(
        run_id=state["run_id"],
        slot=state["slot"],
        created_at_utc=state.get("created_at_utc", datetime.now(timezone.utc).isoformat()),
        scheduled_at_local=state.get("scheduled_at_local", ""),
        topic=state["topic"],
        hook=state["hook"],
        narration_full=state.get("narration_full", ""),
        scenes=scenes,
        total_duration_seconds=total_duration,
        metadata=state["metadata"],
        sources=state.get("sources", []),
        cta=state.get("cta", "Subscribe for daily AI Engineering deep dives!"),
        status="generated",
    )

    logger.info("Validation passed! Package %s generated successfully.", pkg.run_id)
    return {
        "validation_errors": [],
        "package": pkg,
    }


def route_after_validation(state: GraphState) -> str:
    errors = state.get("validation_errors", [])
    retry_count = state.get("retry_count", 0)

    if errors and retry_count < settings.MAX_RETRIES:
        logger.info("Routing back to draft_script_node for retry %d/%d", retry_count, settings.MAX_RETRIES)
        return "draft_script"
    return END


# --- Graph Construction ---

def build_content_graph() -> StateGraph:
    """Assemble and compile the LangGraph workflow."""
    workflow = StateGraph(GraphState)

    workflow.add_node("select_topic", select_topic_node)
    workflow.add_node("research_topic", research_topic_node)
    workflow.add_node("draft_script", draft_script_node)
    workflow.add_node("generate_metadata", generate_metadata_node)
    workflow.add_node("create_flow_prompts", create_flow_prompts_node)
    workflow.add_node("validate_package", validate_package_node)

    workflow.add_edge(START, "select_topic")
    workflow.add_edge("select_topic", "research_topic")
    workflow.add_edge("research_topic", "draft_script")
    workflow.add_edge("draft_script", "generate_metadata")
    workflow.add_edge("generate_metadata", "create_flow_prompts")
    workflow.add_edge("create_flow_prompts", "validate_package")

    workflow.add_conditional_edges(
        "validate_package",
        route_after_validation,
        {
            "draft_script": "draft_script",
            END: END,
        }
    )

    return workflow.compile()


content_graph = build_content_graph()
