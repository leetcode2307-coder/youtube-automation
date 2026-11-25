from pathlib import Path
import pytest
from langchain_groq import ChatGroq
from app import llm as llm_module
from app import graph as graph_module
from app.config import settings
from app.graph import (
    MetadataOutput,
    ScriptOutput,
    TopicOutput,
    generate_metadata_node,
    research_topic_node,
)
from app.pipeline import run_content_pipeline
from app.storage import storage


def test_content_pipeline_execution(tmp_path, monkeypatch):
    test_data_dir = tmp_path / "data"
    monkeypatch.setattr(settings, "DATA_DIR", test_data_dir)
    monkeypatch.setattr(settings, "PACKAGES_DIR", test_data_dir / "packages")
    monkeypatch.setattr(settings, "FLOW_EXPORTS_DIR", test_data_dir / "flow_exports")
    monkeypatch.setattr(settings, "FINAL_OUTPUT_DIR", test_data_dir / "final")
    monkeypatch.setattr(settings, "TOPIC_HISTORY_FILE", test_data_dir / "topic_history.json")
    monkeypatch.setattr(settings, "UPLOAD_LEDGER_FILE", test_data_dir / "upload_ledger.json")
    monkeypatch.setattr(settings, "APPROVALS_FILE", test_data_dir / "approvals.json")

    settings.ensure_directories()

    package = run_content_pipeline("morning", force_mock=True)

    assert package is not None
    assert "morning" in package.run_id
    assert package.status == "awaiting_flow_export"
    assert 30 <= package.total_duration_seconds <= 45
    assert len(package.scenes) >= 3
    assert package.metadata.title
    assert len(package.metadata.hashtags) >= 3

    # Check files created on disk
    pkg_file = settings.PACKAGES_DIR / f"{package.run_id}.json"
    assert pkg_file.exists()

    flow_file = settings.FLOW_EXPORTS_DIR / package.run_id / "FLOW_PROMPTS.txt"
    assert flow_file.exists()
    content = flow_file.read_text(encoding="utf-8")
    assert "GOOGLE FLOW VIDEO GENERATION BRIEF" in content
    assert package.topic in content
    assert "SCENE 1" in content

    # Check topic history
    history = storage.get_topic_history()
    assert any(h["topic"] == package.topic for h in history)


def test_short_title_metadata_normalization_recovery(monkeypatch):
    """Reproduce short LLM title ('Maataani', 8 chars) and verify clean SEOMetadata recovery."""
    short_meta = MetadataOutput.model_construct(
        title="Maataani",
        description="Short",
        hashtags=["Hanuman", "#Telugu"],
        tags=["hanuman", "moral"],
        synthetic_media_disclosure=True,
    )
    monkeypatch.setattr(graph_module, "invoke_structured_output", lambda *a, **kw: short_meta)

    state = {
        "topic": "Bujji Hanuman: The One Word That Saved Sita",
        "niche": "Devotional/moral",
        "hook": "One humble word changed everything.",
        "narration_full": "Sometimes the strongest word in Telugu wisdom is Maataani.",
        "category_id": "24",
        "synthetic_media_default": True,
        "force_mock": False,
    }
    out = generate_metadata_node(state)
    metadata = out["metadata"]
    assert 10 <= len(metadata.title) <= 100
    assert "Maataani" in metadata.title
    assert len(metadata.description) >= 20
    assert len(metadata.hashtags) >= 3
    assert all(h.startswith("#") for h in metadata.hashtags)
    assert len(metadata.tags) >= 4


def test_structured_llm_tool_use_failed_fallback_and_xml_recovery(monkeypatch):
    """Verify Qwen tool_use_failed / OTPM protection and JSON fallback to Llama."""
    assert llm_module._resolve_max_tokens("qwen/qwen3.8-27b") <= 900
    assert llm_module._resolve_max_tokens("llama-3.3-70b-versatile") >= 1800

    xml_wrapped = (
        "<think>reasoning</think>\n"
        "<tool_call>\n<function=TopicOutput>\n"
        "<parameter=topic>Hanuman Wisdom</parameter>\n"
        "<parameter=angle>Concise Telugu proverb breakdown</parameter>\n"
        "<parameter=reasoning>High retention devotional short</parameter>\n"
        "</tool_call>"
    )
    parsed = llm_module._extract_json_payload(xml_wrapped)
    assert parsed["topic"] == "Hanuman Wisdom"

    class FakeStructuredRunnable:
        def __init__(self, model_id: str):
            self.model_id = model_id

        def invoke(self, _prompt):
            if "qwen" in self.model_id:
                raise RuntimeError("Error code: 400 - {'code': 'tool_use_failed'}")
            return TopicOutput(
                topic="Power of Patience in Telugu Proverbs",
                angle="30-second devotional moral story",
                reasoning="Strong shareability across families",
            )

    class FakeChatGroq(ChatGroq):
        def __init__(self, model_id: str):
            object.__setattr__(self, "model_id", model_id)

        def with_structured_output(self, schema, **kwargs):
            return FakeStructuredRunnable(self.model_id)

        def invoke(self, _messages):
            if "qwen" in self.model_id:
                raise RuntimeError("qwen truncated at 900 tokens")
            raise RuntimeError("unused")

    monkeypatch.setattr(settings, "GROQ_API_KEY", "gsk_test_key_valid_format")
    monkeypatch.setattr(
        llm_module,
        "get_llm",
        lambda force_mock=False, model_name=None, max_tokens=None: FakeChatGroq(model_name or "qwen/qwen3.8-27b"),
    )

    result = llm_module.invoke_structured_output(
        TopicOutput,
        "Pick a topic",
        force_mock=False,
        model_name="qwen/qwen3.8-27b",
    )
    assert isinstance(result, TopicOutput)
    assert result.topic == "Power of Patience in Telugu Proverbs"


def test_research_node_graceful_fallback(monkeypatch):
    """Verify research_topic_node falls back cleanly instead of aborting LangGraph."""
    def _boom(*args, **kwargs):
        raise RuntimeError("Simulated Groq 429 rate limit")

    monkeypatch.setattr(graph_module, "invoke_structured_output", _boom)
    res = research_topic_node({
        "topic": "Bujji Hanuman Courage",
        "angle": "Protecting the vulnerable",
        "niche": "Devotional/moral",
        "force_mock": False,
    })
    assert len(res["key_insights"]) == 2
    assert res["sources"] == []
