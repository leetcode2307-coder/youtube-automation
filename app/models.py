from typing import List, Literal, Optional
from pydantic import BaseModel, Field, field_validator, model_validator


SlotType = Literal["morning", "evening"]
PrivacyStatus = Literal["private", "unlisted", "public"]
LanguageType = Literal["english", "telugu", "hindi", "tamil", "malayalam"]
AudioModeType = Literal["ai_voiceover", "ai_plus_clip_audio", "clip_audio_only"]
RunStatus = Literal[
    "scheduled",
    "generating",
    "generated",
    "awaiting_flow_export",
    "assembling",
    "awaiting_approval",
    "approved",
    "rejected",
    "uploaded_private",
    "uploaded",
    "failed",
]


class CharacterProfile(BaseModel):
    name: str = Field(min_length=2, description="Short character identifier to register in Google Flow Characters")
    role: str = Field(default="Main Character", description="Story role, e.g. Protagonist, Host, Narrator")
    visual_description: str = Field(
        min_length=15,
        description="Immutable appearance prompt (age, attire, facial features, palette, style) for Google Flow Character consistency",
    )


class Scene(BaseModel):
    scene_number: int = Field(ge=1, le=10, description="1-indexed sequence number of the scene")
    duration_seconds: int = Field(ge=3, le=15, description="Scene duration in seconds (8-15s for Google Flow Omni)")
    narration: str = Field(min_length=5, description="Spoken narration for this scene")
    visual_prompt: str = Field(min_length=15, description="Detailed visual prompt tailored for Google Flow Omni 9:16 vertical video")
    on_screen_text: str = Field(default="", description="Key text overlay to display during the scene")
    export_filename: str = Field(description="Expected exported clip filename, e.g. scene_01.mp4")

    @field_validator("export_filename")
    @classmethod
    def validate_export_filename(cls, v: str) -> str:
        if not v.endswith(".mp4"):
            raise ValueError(f"Export filename must end with .mp4: {v}")
        return v


class SEOMetadata(BaseModel):
    title: str = Field(min_length=10, max_length=100, description="Hook-driven, clickable title for YouTube Shorts")
    description: str = Field(min_length=20, max_length=5000, description="SEO-optimized description including breakdown")
    hashtags: List[str] = Field(min_length=3, max_length=8, description="YouTube hashtags starting with #")
    tags: List[str] = Field(min_length=4, max_length=15, description="YouTube keyword tags")
    category_id: str = Field(default="28", description="YouTube category ID (e.g. 28 = Science & Technology)")
    synthetic_media_disclosure: bool = Field(default=True, description="Disclose altered/synthetic AI-generated content")

    @field_validator("title")
    @classmethod
    def clean_title(cls, v: str) -> str:
        cleaned = v.strip().replace("\n", " ")
        if len(cleaned) > 100:
            cleaned = cleaned[:97] + "..."
        return cleaned

    @field_validator("hashtags")
    @classmethod
    def validate_hashtags(cls, v: List[str]) -> List[str]:
        cleaned = []
        for tag in v:
            tag = tag.strip()
            if not tag:
                continue
            if not tag.startswith("#"):
                tag = f"#{tag}"
            cleaned.append(tag)
        return cleaned


class ProductionPackage(BaseModel):
    run_id: str = Field(description="Unique immutable identifier, e.g. 20261125_morning_a1b2c3")
    slot: SlotType
    created_at_utc: str
    scheduled_at_local: str
    niche: str = Field(default="", description="Content niche used to generate this run")
    language: LanguageType = Field(default="english", description="Target language for script, voiceover, and YouTube metadata")
    topic: str = Field(min_length=5, description="Specific topic for this video")
    hook: str = Field(min_length=10, description="0-3s high-retention hook")
    narration_full: str = Field(min_length=30, description="Complete narration text for voiceover")
    characters: List[CharacterProfile] = Field(default_factory=list, description="Recurring character definitions for Google Flow consistency")
    scenes: List[Scene] = Field(min_length=3, max_length=8, description="Ordered scenes")
    total_duration_seconds: int = Field(ge=30, le=45, description="Total target duration strictly between 30 and 45s")
    metadata: SEOMetadata
    sources: List[str] = Field(default_factory=list, description="Source notes, documentation references, or citations")
    cta: str = Field(default="Subscribe for more daily insights!", description="Call to action")
    privacy_status: Optional[PrivacyStatus] = Field(default=None, description="Target YouTube upload privacy")
    style_examples_used: str = Field(default="", description="Optional creator style/description memory used to guide prompts")
    status: RunStatus = Field(default="generated")
    error_message: Optional[str] = None


class StudioConfig(BaseModel):
    """UI-editable configuration for niche, AI generation, voice, schedule, and YouTube upload defaults."""
    studio_name: str = Field(default="YouTube Shorts Studio", min_length=2, max_length=80)
    language: LanguageType = Field(default="english")
    niche: str = Field(default="AI Engineering", min_length=2, max_length=120)
    niche_domains: str = Field(default="LLMs, RAG, AI Agents, Model Context Protocol, Inference Optimization, Vector Databases")
    audience_tone: str = Field(default="Clear, engaging, high-retention storytelling")
    default_cta: str = Field(default="Subscribe for more daily insights!")
    category_id: str = Field(default="28")
    default_privacy_status: PrivacyStatus = Field(default="private")
    synthetic_media_default: bool = Field(default=True)
    groq_model: str = Field(default="llama-3.3-70b-versatile", min_length=2)
    llm_temperature: float = Field(default=0.9, ge=0.0, le=1.5)
    prompt_style_examples: str = Field(default="", max_length=3000)
    tts_voice: str = Field(default="en-US-ChristopherNeural", min_length=2)
    use_ai_voiceover: bool = Field(default=True)
    audio_mode: AudioModeType = Field(default="ai_voiceover")
    morning_cron: str = Field(default="0 7 * * *")
    evening_cron: str = Field(default="30 16 * * *")
    timezone: str = Field(default="Asia/Kolkata")
    min_duration_seconds: int = Field(default=30, ge=30, le=44)
    max_duration_seconds: int = Field(default=45, ge=31, le=45)
    min_scenes: int = Field(default=3, ge=3, le=7)
    max_scenes: int = Field(default=5, ge=3, le=8)
    auto_assemble_on_upload: bool = Field(default=True)
    auto_open_review_on_assemble: bool = Field(default=True)
    send_email_on_ready: bool = Field(default=True)

    @model_validator(mode="after")
    def _validate_ranges(self) -> "StudioConfig":
        if self.min_duration_seconds > self.max_duration_seconds:
            raise ValueError("min_duration_seconds cannot exceed max_duration_seconds")
        if self.min_scenes > self.max_scenes:
            raise ValueError("min_scenes cannot exceed max_scenes")
        return self


class ApprovalRecord(BaseModel):
    approval_id: str
    run_id: str
    token_hash: str
    approver_email: str
    decision: Optional[Literal["approved", "rejected"]] = None
    created_at_utc: str
    expires_at_utc: str
    consumed_at_utc: Optional[str] = None
    review_comments: Optional[str] = None


class UploadRecord(BaseModel):
    run_id: str
    video_sha256: str
    youtube_video_id: str
    privacy_status: str = "private"
    uploaded_at_utc: str
    response_json: dict = Field(default_factory=dict)
