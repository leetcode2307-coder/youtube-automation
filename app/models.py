from typing import List, Literal, Optional
from pydantic import BaseModel, Field, field_validator


SlotType = Literal["morning", "evening"]
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
    "failed",
]


class Scene(BaseModel):
    scene_number: int = Field(ge=1, le=10, description="1-indexed sequence number of the scene")
    duration_seconds: int = Field(ge=3, le=15, description="Scene duration in seconds")
    narration: str = Field(min_length=5, description="Spoken narration for this scene")
    visual_prompt: str = Field(min_length=15, description="Detailed visual prompt tailored for Google Flow 9:16 vertical video")
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
    hashtags: List[str] = Field(min_length=3, max_length=8, description="YouTube hashtags starting with #, e.g. #AIEngineering")
    tags: List[str] = Field(min_length=4, max_length=15, description="YouTube keyword tags")
    category_id: str = Field(default="28", description="YouTube category ID (28 = Science & Technology)")
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
            if not tag.startswith("#"):
                tag = f"#{tag}"
            cleaned.append(tag)
        return cleaned


class ProductionPackage(BaseModel):
    run_id: str = Field(description="Unique immutable identifier, e.g. 20261125_morning_a1b2c3")
    slot: SlotType
    created_at_utc: str
    scheduled_at_local: str
    topic: str = Field(min_length=5, description="Specific AI Engineering topic")
    hook: str = Field(min_length=10, description="0-3s high-retention hook")
    narration_full: str = Field(min_length=30, description="Complete narration text for voiceover")
    scenes: List[Scene] = Field(min_length=3, max_length=8, description="Ordered scenes")
    total_duration_seconds: int = Field(ge=30, le=45, description="Total target duration strictly between 30 and 45s")
    metadata: SEOMetadata
    sources: List[str] = Field(default_factory=list, description="Source notes, documentation references, or paper citations")
    cta: str = Field(default="Subscribe for more daily AI Engineering breakdowns!", description="Call to action")
    status: RunStatus = Field(default="generated")
    error_message: Optional[str] = None


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
