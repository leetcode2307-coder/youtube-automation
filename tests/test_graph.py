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
    assert all(8 <= s.duration_seconds <= 15 for s in package.scenes)
    assert len(package.characters) >= 1
    assert package.metadata.title
    assert len(package.metadata.hashtags) >= 3

    # Check files created on disk
    pkg_file = settings.PACKAGES_DIR / f"{package.run_id}.json"
    assert pkg_file.exists()

    flow_file = settings.FLOW_EXPORTS_DIR / package.run_id / "FLOW_PROMPTS.txt"
    assert flow_file.exists()
    content = flow_file.read_text(encoding="utf-8")
    assert "GOOGLE FLOW VIDEO GENERATION BRIEF" in content
    assert "CONSISTENT CHARACTERS TO ADD IN GOOGLE FLOW FIRST" in content
    assert "Omni" in content
    assert "project ZIP" in content
    assert package.topic in content
    assert "SCENE 1" in content

    # Check topic history
    history = storage.get_topic_history()
    assert any(h["topic"] == package.topic for h in history)


def test_multilingual_pipeline_and_tts_voice_resolution(tmp_path, monkeypatch):
    from app.config import LANGUAGE_PROFILES, normalize_language, resolve_tts_voice

    assert set(LANGUAGE_PROFILES.keys()) == {"english", "telugu", "hindi", "tamil", "malayalam"}
    assert normalize_language("TELUGU") == "telugu"
    assert normalize_language("unknown") == "english"

    assert resolve_tts_voice("telugu", "en-US-ChristopherNeural") == "te-IN-MohanNeural"
    assert resolve_tts_voice("telugu", "te-IN-ShrutiNeural") == "te-IN-ShrutiNeural"
    assert resolve_tts_voice("hindi", "en-US-ChristopherNeural") == "hi-IN-MadhurNeural"
    assert resolve_tts_voice("tamil", "en-US-ChristopherNeural") == "ta-IN-ValluvarNeural"
    assert resolve_tts_voice("malayalam", "en-US-ChristopherNeural") == "ml-IN-MidhunNeural"

    test_data_dir = tmp_path / "data"
    monkeypatch.setattr(settings, "DATA_DIR", test_data_dir)
    monkeypatch.setattr(settings, "PACKAGES_DIR", test_data_dir / "packages")
    monkeypatch.setattr(settings, "FLOW_EXPORTS_DIR", test_data_dir / "flow_exports")
    monkeypatch.setattr(settings, "FINAL_OUTPUT_DIR", test_data_dir / "final")
    monkeypatch.setattr(settings, "TOPIC_HISTORY_FILE", test_data_dir / "topic_history.json")
    monkeypatch.setattr(settings, "UPLOAD_LEDGER_FILE", test_data_dir / "upload_ledger.json")
    monkeypatch.setattr(settings, "APPROVALS_FILE", test_data_dir / "approvals.json")
    settings.ensure_directories()

    pkg = run_content_pipeline("evening", force_mock=True, language_override="telugu")
    assert pkg.language == "telugu"
    flow_text = (settings.FLOW_EXPORTS_DIR / pkg.run_id / "FLOW_PROMPTS.txt").read_text(encoding="utf-8")
    assert "Telugu" in flow_text


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


def test_get_llm_resolves_max_tokens_with_real_factory(monkeypatch):
    """Ensure get_llm calls _resolve_max_tokens without keyword mismatch when GROQ_API_KEY is configured."""
    monkeypatch.setattr(settings, "GROQ_API_KEY", "gsk_test_key_valid_format")

    qwen_default = llm_module.get_llm(force_mock=False, model_name="qwen/qwen3.8-27b")
    assert isinstance(qwen_default, ChatGroq)
    assert qwen_default.max_tokens <= 900

    qwen_capped = llm_module.get_llm(force_mock=False, model_name="qwen/qwen3.8-27b", max_tokens=1500)
    assert isinstance(qwen_capped, ChatGroq)
    assert qwen_capped.max_tokens == 900

    qwen_small = llm_module.get_llm(force_mock=False, model_name="qwen/qwen3.8-27b", max_tokens=512)
    assert isinstance(qwen_small, ChatGroq)
    assert qwen_small.max_tokens == 512

    llama_default = llm_module.get_llm(force_mock=False, model_name="llama-3.3-70b-versatile")
    assert isinstance(llama_default, ChatGroq)
    assert llama_default.max_tokens >= 1800

    llama_large = llm_module.get_llm(force_mock=False, model_name="llama-3.3-70b-versatile", max_tokens=2048)
    assert isinstance(llama_large, ChatGroq)
    assert llama_large.max_tokens == 2048

    # Exercise invoke_structured_output end-to-end through real get_llm() without stubbing get_llm
    class FakeJsonModeRunnable:
        def invoke(self, _prompt):
            return TopicOutput(
                topic="Speculative Decoding in Production",
                angle="Cut LLM latency in half using draft models",
                reasoning="Strong AI Engineering appeal",
            )

    monkeypatch.setattr(ChatGroq, "with_structured_output", lambda self, schema, **kw: FakeJsonModeRunnable())
    topic_out = llm_module.invoke_structured_output(
        TopicOutput,
        "Pick a topic",
        force_mock=False,
        model_name="llama-3.3-70b-versatile",
        max_tokens=1200,
    )
    assert isinstance(topic_out, TopicOutput)
    assert topic_out.topic == "Speculative Decoding in Production"


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


def test_temperature_and_optional_style_memory(tmp_path, monkeypatch):
    """Verify higher creative temperature wiring and optional style/description memory."""
    test_data_dir = tmp_path / "data"
    monkeypatch.setattr(settings, "DATA_DIR", test_data_dir)
    monkeypatch.setattr(settings, "PACKAGES_DIR", test_data_dir / "packages")
    monkeypatch.setattr(settings, "FLOW_EXPORTS_DIR", test_data_dir / "flow_exports")
    monkeypatch.setattr(settings, "FINAL_OUTPUT_DIR", test_data_dir / "final")
    monkeypatch.setattr(settings, "TOPIC_HISTORY_FILE", test_data_dir / "topic_history.json")
    monkeypatch.setattr(settings, "UPLOAD_LEDGER_FILE", test_data_dir / "upload_ledger.json")
    monkeypatch.setattr(settings, "APPROVALS_FILE", test_data_dir / "approvals.json")
    monkeypatch.setattr(settings, "STUDIO_SETTINGS_FILE", test_data_dir / "studio_settings.json")
    monkeypatch.setattr(settings, "GROQ_API_KEY", "gsk_test_key_valid_format")
    settings.ensure_directories()

    default_llm = llm_module.get_llm(force_mock=False)
    assert isinstance(default_llm, ChatGroq)
    assert default_llm.temperature == pytest.approx(settings.GROQ_TEMPERATURE)
    assert default_llm.temperature >= 0.85

    custom_llm = llm_module.get_llm(force_mock=False, temperature=1.15)
    assert isinstance(custom_llm, ChatGroq)
    assert custom_llm.temperature == pytest.approx(1.15)

    captured_calls = []

    def _fake_invoke(schema, prompt, force_mock=False, model_name=None, max_tokens=None, temperature=None):
        captured_calls.append({"schema": schema.__name__, "prompt": prompt, "temperature": temperature})
        return llm_module.MockStructuredOutput(schema).invoke(prompt)

    monkeypatch.setattr(graph_module, "invoke_structured_output", _fake_invoke)

    # 1. Without style examples -> completely optional, no style memory block injected
    graph_module.select_topic_node({"niche": "Science", "language": "english", "llm_temperature": 1.05})
    assert captured_calls[-1]["temperature"] == pytest.approx(1.05)
    assert "CREATOR STYLE & DESCRIPTION MEMORY" not in captured_calls[-1]["prompt"]

    # 2. With style examples -> injected into creative topic + script prompts
    graph_module.draft_script_node({
        "niche": "Science",
        "language": "english",
        "topic": "Quantum Tunneling",
        "angle": "How particles walk through walls",
        "key_insights": ["Wave probability passes barriers"],
        "llm_temperature": 1.1,
        "style_examples": "Mystery noir hook in 2s, neon rim light, twist payoff at 32s.",
    })
    assert captured_calls[-1]["temperature"] == pytest.approx(1.1)
    assert "Mystery noir hook in 2s" in captured_calls[-1]["prompt"]

    # 3. Pipeline remember_style persistence and optional bypass
    pkg1 = run_content_pipeline(
        "morning",
        force_mock=True,
        style_examples_override="Dramatic whisper intro with fast montage cuts.",
        remember_style=True,
    )
    assert pkg1.style_examples_used == "Dramatic whisper intro with fast montage cuts."
    assert storage.get_studio_config().prompt_style_examples == "Dramatic whisper intro with fast montage cuts."

    pkg2 = run_content_pipeline("evening", force_mock=True)
    assert pkg2.style_examples_used == "Dramatic whisper intro with fast montage cuts."

    pkg3 = run_content_pipeline("morning", force_mock=True, style_examples_override="")
    assert pkg3.style_examples_used == ""
