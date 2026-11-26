import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, TypedDict
from pydantic import BaseModel, Field
from langgraph.graph import StateGraph, START, END

from app.config import LANGUAGE_PROFILES, normalize_language, settings
from app.llm import invoke_structured_output
from app.logging_config import logger
from app.models import CharacterProfile, PrivacyStatus, ProductionPackage, Scene, SEOMetadata, SlotType


# --- Structured Output Schemas for LLM ---

class TopicOutput(BaseModel):
    topic: str = Field(description="Specific, compelling topic tailored to the configured niche")
    angle: str = Field(description="Unique angle or takeaway for the target audience")
    reasoning: str = Field(description="Why this is timely and high-value for YouTube Shorts")


class ResearchOutput(BaseModel):
    key_insights: List[str] = Field(min_length=2, max_length=5, description="Key points and takeaways")
    sources: List[str] = Field(default_factory=list, description="Authoritative reference URLs or citations")


class ScriptOutput(BaseModel):
    hook: str = Field(description="High-retention 0-3 second opening hook line")
    characters: List[CharacterProfile] = Field(
        default_factory=list,
        max_length=3,
        description="1-3 recurring characters to add in Google Flow Characters for consistent video clips",
    )
    scenes: List[Scene] = Field(min_length=3, max_length=6, description="Sequential 8-15s Google Flow Omni scenes matching target duration")
    cta: str = Field(description="Closing call to action, e.g. subscribe")


class MetadataOutput(BaseModel):
    title: str = Field(
        min_length=10,
        max_length=100,
        description="Clickable YouTube Shorts title (15 to 90 characters, never a single word)",
    )
    description: str = Field(
        min_length=20,
        description="Engaging YouTube description with summary and hashtags (at least 20 characters)",
    )
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
    niche: str
    language: str
    niche_domains: str
    audience_tone: str
    default_cta: str
    category_id: str
    groq_model: str
    min_duration_seconds: int
    max_duration_seconds: int
    min_scenes: int
    max_scenes: int
    synthetic_media_default: bool
    privacy_status: PrivacyStatus
    custom_topic: str
    topic: str
    angle: str
    sources: List[str]
    key_insights: List[str]
    hook: str
    characters: List[CharacterProfile]
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

def _normalize_metadata_output(
    result: MetadataOutput,
    *,
    topic: str,
    niche: str,
    hook: str,
    narration: str,
    category_id: str,
    synthetic_default: bool,
) -> SEOMetadata:
    """Coerce and validate LLM metadata so short titles or sparse fields never crash SEOMetadata validation."""
    raw_title = (result.title or "").strip().strip('"\'').replace("\n", " ")
    topic_clean = (topic or "").strip().replace("\n", " ")
    if len(raw_title) < 10:
        logger.warning(
            "Generated title '%s' is shorter than 10 chars; enriching from topic/niche.",
            raw_title,
        )
        if raw_title and topic_clean and raw_title.lower() not in topic_clean.lower():
            raw_title = f"{raw_title} — {topic_clean}"
        elif topic_clean and len(topic_clean) >= 10:
            raw_title = topic_clean
        else:
            raw_title = f"{raw_title or niche} | YouTube Short Story"
    if len(raw_title) < 10:
        raw_title = f"{raw_title} | YouTube Short"
    if len(raw_title) > 100:
        raw_title = raw_title[:97].rstrip() + "..."

    raw_desc = (result.description or "").strip()
    if len(raw_desc) < 20:
        fallback_parts = [p.strip() for p in (topic_clean, hook, narration) if p and p.strip()]
        raw_desc = " — ".join(fallback_parts)[:400] or f"{raw_title} in the {niche} niche."
        if len(raw_desc) < 20:
            raw_desc = f"{raw_title} — Daily {niche} Short."

    normalized_hashtags: List[str] = []
    seen_tags_lower = set()
    for tag in result.hashtags or []:
        clean = str(tag).strip().replace(" ", "")
        if not clean:
            continue
        if not clean.startswith("#"):
            clean = f"#{clean}"
        if clean.lower() not in seen_tags_lower and len(clean) > 1:
            normalized_hashtags.append(clean)
            seen_tags_lower.add(clean.lower())
    for default_ht in (f"#{re.sub(r'[^A-Za-z0-9]', '', niche) or 'Shorts'}", "#Shorts", "#YouTubeShorts"):
        if len(normalized_hashtags) >= 3:
            break
        if default_ht.lower() not in seen_tags_lower:
            normalized_hashtags.append(default_ht)
            seen_tags_lower.add(default_ht.lower())
    normalized_hashtags = normalized_hashtags[:8]

    normalized_tags: List[str] = []
    seen_kw = set()
    for kw in result.tags or []:
        clean_kw = str(kw).strip()
        if clean_kw and clean_kw.lower() not in seen_kw:
            normalized_tags.append(clean_kw)
            seen_kw.add(clean_kw.lower())
    for fallback_kw in (niche, "youtube shorts", "short story", "daily shorts"):
        if len(normalized_tags) >= 4:
            break
        if fallback_kw and fallback_kw.lower() not in seen_kw:
            normalized_tags.append(fallback_kw)
            seen_kw.add(fallback_kw.lower())
    normalized_tags = normalized_tags[:15]

    return SEOMetadata(
        title=raw_title,
        description=raw_desc,
        hashtags=normalized_hashtags,
        tags=normalized_tags,
        category_id=str(category_id),
        synthetic_media_disclosure=bool(synthetic_default),
    )


def select_topic_node(state: GraphState) -> Dict[str, Any]:
    logger.info("Executing select_topic_node for run %s (%s)", state.get("run_id"), state.get("slot"))
    force_mock = state.get("force_mock", False)
    groq_model = state.get("groq_model")
    niche = state.get("niche") or settings.DEFAULT_NICHE
    language = normalize_language(state.get("language"))
    lang_profile = LANGUAGE_PROFILES[language]
    domains = state.get("niche_domains") or settings.DEFAULT_NICHE_DOMAINS
    tone = state.get("audience_tone") or settings.DEFAULT_AUDIENCE_TONE
    custom_topic = (state.get("custom_topic") or "").strip()

    if custom_topic:
        if force_mock:
            return {
                "topic": custom_topic,
                "angle": f"Practical, high-retention {niche} ({lang_profile['label']}) breakdown on {custom_topic}",
            }
        prompt = (
            f"You are a YouTube Shorts strategist specializing in '{niche}' for {lang_profile['label']} audiences.\n"
            f"Tone/Audience: {tone}\n"
            f"Target Language: {lang_profile['label']}. {lang_profile['script_instruction']}\n"
            f"The creator specifically requested this topic: '{custom_topic}'.\n"
            "Keep the topic aligned to the requested subject and propose a sharp, visual storytelling angle (<= 40 words) ideal for 8-15s Google Flow Omni scenes."
        )
        try:
            result: TopicOutput = invoke_structured_output(
                TopicOutput,
                prompt,
                force_mock=force_mock,
                model_name=groq_model,
            )
            return {
                "topic": custom_topic,
                "angle": result.angle,
            }
        except Exception as exc:
            logger.warning("select_topic custom angle generation failed (%s); using direct angle.", exc)
            return {
                "topic": custom_topic,
                "angle": f"Engaging 30-45s {niche} story in {lang_profile['label']} focused on {custom_topic}",
            }

    recent = state.get("recent_topics", [])
    recent_str = "\n".join(f"- {t}" for t in recent[-20:]) if recent else "None (first run)"

    prompt = (
        f"You are a Lead Content Strategist for a YouTube Shorts channel in the '{niche}' niche.\n"
        f"Target Language: {lang_profile['label']}.\n"
        f"Audience & Tone: {tone}\n"
        f"Select ONE compelling, highly visual topic for a YouTube Short (keep topic under 120 characters) suited to 8-15s AI video scenes.\n"
        f"Focus Areas / Subtopics: {domains}\n\n"
        f"Recently covered topics (DO NOT DUPLICATE THESE):\n{recent_str}\n\n"
        "Provide a unique topic, concise visual angle (1-2 sentences), and brief reasoning."
    )

    try:
        result = invoke_structured_output(
            TopicOutput,
            prompt,
            force_mock=force_mock,
            model_name=groq_model,
        )
    except Exception as exc:
        raise RuntimeError(f"Stage 'select_topic' failed for run {state.get('run_id')}: {exc}") from exc

    logger.info("Selected topic: '%s' (Angle: %s)", result.topic, result.angle)
    return {
        "topic": result.topic,
        "angle": result.angle,
    }


def research_topic_node(state: GraphState) -> Dict[str, Any]:
    logger.info("Executing research_topic_node for topic: %s", state.get("topic"))
    force_mock = state.get("force_mock", False)
    groq_model = state.get("groq_model")
    niche = state.get("niche") or settings.DEFAULT_NICHE

    prompt = (
        f"You are researching the '{niche}' topic: '{state.get('topic')}'.\n"
        f"Angle: {state.get('angle')}\n\n"
        "Extract 2-4 concise insights, facts, or storytelling beats. "
        "Include 0-3 authoritative reference links or citations if applicable."
    )

    try:
        result: ResearchOutput = invoke_structured_output(
            ResearchOutput,
            prompt,
            force_mock=force_mock,
            model_name=groq_model,
        )
        return {
            "key_insights": result.key_insights,
            "sources": result.sources,
        }
    except Exception as exc:
        logger.warning(
            "Stage 'research_topic' failed (%s); falling back to topic/angle context.",
            exc,
        )
        return {
            "key_insights": [
                f"Core premise: {state.get('topic')}",
                f"Storytelling angle: {state.get('angle')}",
            ],
            "sources": [],
        }


def draft_script_node(state: GraphState) -> Dict[str, Any]:
    logger.info("Executing draft_script_node (Attempt %d)", state.get("retry_count", 0) + 1)
    force_mock = state.get("force_mock", False)
    groq_model = state.get("groq_model")
    niche = state.get("niche") or settings.DEFAULT_NICHE
    language = normalize_language(state.get("language"))
    lang_profile = LANGUAGE_PROFILES[language]
    tone = state.get("audience_tone") or settings.DEFAULT_AUDIENCE_TONE
    default_cta = state.get("default_cta") or settings.DEFAULT_CTA
    min_dur = state.get("min_duration_seconds", settings.MIN_VIDEO_DURATION)
    max_dur = state.get("max_duration_seconds", settings.MAX_VIDEO_DURATION)
    # Google AI Pro Omni clips run 8-15s per scene; clamp scene bounds so total stays within min_dur..max_dur
    max_scenes = max(3, min(state.get("max_scenes", 5), max_dur // 8))
    min_scenes = min(max_scenes, max(3, state.get("min_scenes", 3), (min_dur + 14) // 15))

    retry_feedback = ""
    if state.get("validation_errors"):
        retry_feedback = (
            "\nPREVIOUS ATTEMPT FAILED VALIDATION WITH THESE ERRORS:\n"
            + "\n".join(f"- {e}" for e in state["validation_errors"])
            + "\nPlease strictly fix these errors in this generation.\n"
        )

    prompt = (
        f"You are writing a YouTube Short script and Google Flow Omni video prompts ({min_dur}-{max_dur}s total) in '{niche}'.\n"
        f"Target Language: {lang_profile['label']}. {lang_profile['script_instruction']}\n"
        f"Audience & Tone: {tone}\n"
        f"Topic: {state.get('topic')}\n"
        f"Angle: {state.get('angle')}\n"
        f"Key Insights: {', '.join(state.get('key_insights', []))}\n"
        f"{retry_feedback}\n"
        "STRICT CONSTRAINTS (GOOGLE AI PRO — GOOGLE FLOW OMNI MODEL):\n"
        f"1. Total duration MUST be between {min_dur} and {max_dur} seconds.\n"
        f"2. Break the video into {min_scenes} to {max_scenes} sequential scenes.\n"
        "3. Each scene duration_seconds MUST be an integer between 8 and 15 (optimized for Google Flow Omni 8-15s clips).\n"
        f"4. The SUM of all scene duration_seconds MUST EQUAL between {min_dur} and {max_dur}.\n"
        "5. CONSISTENT CHARACTERS (CRITICAL): Define 1 to 3 recurring characters in 'characters' with:\n"
        "   - name (short reusable label, e.g. 'Bujji Hanuman' or 'Aarav')\n"
        "   - role (e.g. 'Protagonist', 'Host')\n"
        "   - visual_description (immutable English appearance prompt for Google Flow Characters: age, face, hair, clothing, colors, art/cinematic style)\n"
        "6. For each scene, provide:\n"
        f"   - narration: natural spoken lines in {lang_profile['label']} matching an 8-15s beat (2-3 crisp sentences)\n"
        f"   - on_screen_text: punchy 2-6 word caption in {lang_profile['label']}\n"
        "   - visual_prompt: VIVID ENGLISH cinematic prompt (under 240 characters) for Google Flow Omni 9:16 vertical video. Reference the exact character 'name' from 'characters', camera angle/motion, lighting, environment, and action progression.\n"
        "   - export_filename: 'scene_01.mp4', 'scene_02.mp4', etc.\n"
        f"7. High-retention opening hook in {lang_profile['label']} for scene 1 (0-3s), and punchy CTA in {lang_profile['label']} (inspired by: '{default_cta}').\n"
        "Keep prompts crisp so the complete JSON finishes cleanly without truncation."
    )

    try:
        result: ScriptOutput = invoke_structured_output(
            ScriptOutput,
            prompt,
            force_mock=force_mock,
            model_name=groq_model,
        )
    except Exception as exc:
        raise RuntimeError(f"Stage 'draft_script' failed for run {state.get('run_id')}: {exc}") from exc

    # Normalize scene ordering and export filenames (e.g., scene_01.mp4)
    normalized_scenes: List[Scene] = []
    for idx, scene in enumerate(result.scenes, start=1):
        normalized_scenes.append(
            scene.model_copy(
                update={
                    "scene_number": idx,
                    "export_filename": f"scene_{idx:02d}.mp4",
                }
            )
        )

    total_duration = sum(s.duration_seconds for s in normalized_scenes)
    full_narration = " ".join(s.narration for s in normalized_scenes)

    logger.info(
        "Drafted script (%s): %d scenes, %d characters, %d seconds total duration",
        language, len(normalized_scenes), len(result.characters), total_duration,
    )
    return {
        "hook": result.hook,
        "characters": result.characters,
        "scenes": normalized_scenes,
        "cta": result.cta or default_cta,
        "narration_full": full_narration,
        "total_duration_seconds": total_duration,
    }


def generate_metadata_node(state: GraphState) -> Dict[str, Any]:
    logger.info("Executing generate_metadata_node for topic: %s", state.get("topic"))
    force_mock = state.get("force_mock", False)
    groq_model = state.get("groq_model")
    niche = state.get("niche") or settings.DEFAULT_NICHE
    language = normalize_language(state.get("language"))
    lang_profile = LANGUAGE_PROFILES[language]
    category_id = state.get("category_id") or settings.DEFAULT_YOUTUBE_CATEGORY_ID
    synthetic_default = state.get("synthetic_media_default", settings.DEFAULT_SYNTHETIC_MEDIA)

    prompt = (
        f"Create YouTube Shorts SEO metadata for a '{niche}' video in {lang_profile['label']}.\n"
        f"Language Rule: {lang_profile['script_instruction']}\n"
        f"Topic: {state.get('topic')}\n"
        f"Hook: {state.get('hook')}\n"
        f"Full Narration: {state.get('narration_full')}\n\n"
        "Requirements (STRICT):\n"
        f"- title: MUST be 20 to 90 characters in {lang_profile['label']} (minimum 10 characters; NEVER return a single short word).\n"
        f"- description: MUST be at least 30 characters in {lang_profile['label']} with summary, keywords, and story notes.\n"
        "- hashtags: 3 to 5 relevant hashtags starting with #.\n"
        "- tags: 5 to 10 YouTube search keyword tags.\n"
        f"- synthetic_media_disclosure: {str(synthetic_default).lower()}."
    )

    try:
        result: MetadataOutput = invoke_structured_output(
            MetadataOutput,
            prompt,
            force_mock=force_mock,
            model_name=groq_model,
        )
    except Exception as exc:
        logger.warning("Metadata structured generation hit error (%s); synthesizing metadata from script.", exc)
        result = MetadataOutput.model_construct(
            title=(state.get("topic") or f"{niche} Short")[:95],
            description=state.get("narration_full") or state.get("hook") or f"{state.get('topic')} — {niche} Short",
            hashtags=[f"#{re.sub(r'[^A-Za-z0-9]', '', niche) or 'Shorts'}", "#Shorts", "#YouTubeShorts"],
            tags=[niche, "shorts", "youtube shorts", "storytelling"],
            synthetic_media_disclosure=bool(synthetic_default),
        )

    metadata = _normalize_metadata_output(
        result,
        topic=state.get("topic", ""),
        niche=niche,
        hook=state.get("hook", ""),
        narration=state.get("narration_full", ""),
        category_id=str(category_id),
        synthetic_default=bool(synthetic_default),
    )

    return {"metadata": metadata}


def create_flow_prompts_node(state: GraphState) -> Dict[str, Any]:
    logger.info("Executing create_flow_prompts_node for run %s", state.get("run_id"))
    run_id = state.get("run_id", "unknown_run")
    slot = state.get("slot", "morning")
    niche = state.get("niche") or settings.DEFAULT_NICHE
    language = normalize_language(state.get("language"))
    lang_label = LANGUAGE_PROFILES[language]["label"]
    topic = state.get("topic", "")
    total_dur = state.get("total_duration_seconds", 0)
    characters = state.get("characters", [])
    scenes = state.get("scenes", [])

    lines = [
        "=" * 70,
        f"GOOGLE FLOW VIDEO GENERATION BRIEF - RUN ID: {run_id}",
        f"Niche: {niche} | Language: {lang_label} | Slot: {slot.upper()} | Topic: {topic}",
        f"Target Model: Google AI Pro (Google Flow Omni, 8-15s/scene) | Aspect: 9:16 (Portrait)",
        f"Target Total Duration: {total_dur} seconds across {len(scenes)} scenes",
        "=" * 70,
        "",
        "STEP-BY-STEP INSTRUCTIONS FOR GOOGLE AI PRO (FLOW OMNI):",
        f"1. Open Google Flow and create a new 9:16 Portrait project for Run ID '{run_id}'.",
        "2. Select the Omni video model and set clip length to 8-15 seconds per scene.",
        "3. Open 'Characters' in Google Flow FIRST and add each character below so characters stay consistent across all video clips.",
        "4. Generate each scene clip ONE BY ONE using its FLOW VISUAL PROMPT (selecting the matching character(s) and duration).",
        "5. After generating all scenes, download the project ZIP from Google Flow.",
        "6. Extract the ZIP on your device, ensure clips are named scene_01.mp4 .. scene_0N.mp4 in order, and upload them into the Studio.",
        "   (Note: After YouTube publish finishes, intermediate scene_xx.mp4 files are automatically deleted from storage; final Short & captions remain.)",
        "",
    ]

    if characters:
        lines.extend([
            "-" * 70,
            "CONSISTENT CHARACTERS TO ADD IN GOOGLE FLOW FIRST:",
            "-" * 70,
        ])
        for c in characters:
            lines.extend([
                f"* {c.name} ({c.role})",
                f"  Character Prompt: {c.visual_description}",
                "",
            ])

    lines.extend([
        "-" * 70,
        "SCENE-BY-SCENE GENERATION PROMPTS (GENERATE ONE BY ONE):",
        "-" * 70,
    ])

    for s in scenes:
        lines.extend([
            f"\n[SCENE {s.scene_number}] -> Export Filename: {s.export_filename}",
            f"Target Duration: {s.duration_seconds} seconds (Omni 8-15s clip)",
            f"On-Screen Text ({lang_label}): {s.on_screen_text}",
            f"Voiceover Text ({lang_label}): \"{s.narration}\"",
            f"FLOW VISUAL PROMPT:\n{s.visual_prompt}",
            "-" * 40,
        ])

    lines.extend([
        "\nPOST-EXPORT VERIFICATION CHECKLIST:",
        f"[ ] Downloaded project ZIP from Google Flow and extracted all {len(scenes)} clip(s)",
        f"[ ] Filenames match exactly: {', '.join(s.export_filename for s in scenes)}",
        "[ ] Uploaded clips in the Studio (optional: include voiceover.mp3 for custom audio)",
        "=" * 70,
    ])

    flow_content = "\n".join(lines)
    return {"flow_prompts_file_content": flow_content}


def validate_package_node(state: GraphState) -> Dict[str, Any]:
    logger.info("Executing validate_package_node for run %s", state.get("run_id"))
    errors = []
    scenes = state.get("scenes", [])
    total_duration = sum(s.duration_seconds for s in scenes)
    min_dur = state.get("min_duration_seconds", settings.MIN_VIDEO_DURATION)
    max_dur = state.get("max_duration_seconds", settings.MAX_VIDEO_DURATION)

    if not (min_dur <= total_duration <= max_dur):
        errors.append(f"Total duration ({total_duration}s) outside allowed {min_dur}-{max_dur}s range.")

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

    niche = state.get("niche") or settings.DEFAULT_NICHE
    default_cta = state.get("default_cta") or settings.DEFAULT_CTA

    # All valid -> Assemble final ProductionPackage
    pkg = ProductionPackage(
        run_id=state["run_id"],
        slot=state["slot"],
        created_at_utc=state.get("created_at_utc", datetime.now(timezone.utc).isoformat()),
        scheduled_at_local=state.get("scheduled_at_local", ""),
        niche=niche,
        language=normalize_language(state.get("language")),
        topic=state["topic"],
        hook=state["hook"],
        narration_full=state.get("narration_full", ""),
        characters=state.get("characters", []),
        scenes=scenes,
        total_duration_seconds=total_duration,
        metadata=state["metadata"],
        sources=state.get("sources", []),
        cta=state.get("cta", default_cta),
        privacy_status=state.get("privacy_status", settings.DEFAULT_PRIVACY_STATUS),
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
