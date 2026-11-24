import pytest
from pydantic import ValidationError
from app.models import ProductionPackage, Scene, SEOMetadata


def test_scene_validation():
    scene = Scene(
        scene_number=1,
        duration_seconds=7,
        narration="Here is why RAG is changing enterprise AI architectures.",
        visual_prompt="Close up of neural network vector embeddings connecting across a 3D database graph, 9:16 portrait",
        on_screen_text="Vector DB Architecture",
        export_filename="scene_01.mp4"
    )
    assert scene.scene_number == 1
    assert scene.duration_seconds == 7
    assert scene.export_filename == "scene_01.mp4"

    with pytest.raises(ValidationError):
        Scene(
            scene_number=1,
            duration_seconds=7,
            narration="Valid narration.",
            visual_prompt="Valid visual prompt.",
            export_filename="scene_01.avi"  # must be .mp4
        )


def test_metadata_validation():
    metadata = SEOMetadata(
        title="Why GraphRAG Beats Vector Search in 2026",
        description="A complete technical breakdown of GraphRAG vs Vector embeddings.",
        hashtags=["AIEngineering", "#RAG", "LLM"],
        tags=["graphrag", "vector search", "ai engineering", "llm"]
    )
    assert all(tag.startswith("#") for tag in metadata.hashtags)
    assert len(metadata.title) <= 100


def test_package_duration_bounds():
    metadata = SEOMetadata(
        title="Understanding Model Context Protocol (MCP)",
        description="Deep dive into MCP architecture.",
        hashtags=["#MCP", "#Agents", "#AI"],
        tags=["mcp", "model context protocol", "ai agents", "anthropic"]
    )
    scenes = [
        Scene(
            scene_number=i,
            duration_seconds=7,
            narration=f"Narration for scene {i}",
            visual_prompt=f"Visual prompt for scene {i} with high quality 9:16 render",
            export_filename=f"scene_0{i}.mp4"
        )
        for i in range(1, 6)
    ]
    # Total duration = 35 seconds (valid: between 30 and 45)
    pkg = ProductionPackage(
        run_id="20261125_morning_test1",
        slot="morning",
        created_at_utc="2026-11-25T01:30:00Z",
        scheduled_at_local="2026-11-25 07:00:00 IST",
        topic="Model Context Protocol Architecture",
        hook="Stop hardcoding tools into your AI agents—MCP just changed everything.",
        narration_full="Full script text across all scenes.",
        scenes=scenes,
        total_duration_seconds=35,
        metadata=metadata
    )
    assert pkg.total_duration_seconds == 35

    # Out of bounds: 25s (<30)
    data = pkg.model_dump()
    data["total_duration_seconds"] = 25
    with pytest.raises(ValidationError):
        ProductionPackage.model_validate(data)

    # Out of bounds: 50s (>45)
    data["total_duration_seconds"] = 50
    with pytest.raises(ValidationError):
        ProductionPackage.model_validate(data)
