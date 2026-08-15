"""Fáze 3 — strukturování. Uzavřené tagy, žádné wikilinks, oba backendy."""

from __future__ import annotations

import hashlib
import json

import pytest
from conftest import FakeStructurer, FakeTranscriber, audio_bytes

from voicenotes.config import AnthropicConfig, OllamaConfig, StructureConfig
from voicenotes.worker.notes import STATUS_INBOX, STATUS_NEEDS_REVIEW
from voicenotes.worker.run import run
from voicenotes.worker.structure import (
    AnthropicStructurer,
    OllamaStructurer,
    Structure,
    StructuringFailed,
    build_structurer,
    build_system_prompt,
    clean_title,
    extract_json,
    normalize_tags,
    normalize_tasks,
    parse_structure,
    strip_wikilinks,
)

TAGS = ("napad", "ukol", "poznamka", "otazka", "prace", "osobni")


def queue_recording(env, data: bytes, *, when: str = "2026-08-14T143211") -> str:
    env.queue_dir.mkdir(parents=True, exist_ok=True)
    name = f"{when}-{hashlib.sha256(data).hexdigest()[:6]}.m4a"
    (env.queue_dir / name).write_bytes(data)
    return name


def payload(**overrides) -> str:
    body = {
        "title": "Přepracovat retry logiku v importu",
        "summary": "Import padá na timeoutu.",
        "tags": ["napad"],
        "tasks": ["zvýšit timeout"],
    }
    body.update(overrides)
    return json.dumps(body, ensure_ascii=False)


# --- uzavřený seznam tagů -------------------------------------------------


@pytest.mark.parametrize(
    ("returned", "expected"),
    [
        (["napad"], ["napad"]),
        # Diakritiku a mřížku modelu odpustíme, vymyšlený tag ne.
        (["nápad", "#prace"], ["napad", "prace"]),
        (["NAPAD", "Napad"], ["napad"]),
        (["retro", "brainstorming"], []),
        (["napad", "vymyšlený", "ukol"], ["napad", "ukol"]),
        ([], []),
        ("napad", []),
        ([1, None, {"a": 1}], []),
    ],
)
def test_tags_outside_the_list_are_dropped(returned, expected):
    assert normalize_tags(returned, TAGS) == expected


def test_tags_keep_config_order():
    assert normalize_tags(["osobni", "napad", "ukol"], TAGS) == ["napad", "ukol", "osobni"]


def test_allowed_tags_reach_the_prompt():
    prompt = build_system_prompt(StructureConfig(tags=("napad", "ukol")))

    assert "napad, ukol" in prompt
    assert "Nevymýšlej si vlastní tagy" in prompt


# --- žádné wikilinks ------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("odkaz na [[Poznámku]]", "odkaz na Poznámku"),
        ("[[cíl|popisek]] uprostřed", "popisek uprostřed"),
        ("[[a]] a [[b|c]]", "a a c"),
        ("bez odkazu", "bez odkazu"),
    ],
)
def test_wikilinks_are_stripped(text, expected):
    assert strip_wikilinks(text) == expected


def test_wikilinks_never_reach_the_note():
    """Model neví, co ve vaultu existuje — odkazy by mířily do prázdna."""
    structure = parse_structure(
        payload(
            title="Dodělat [[Import]]",
            summary="Navazuje na [[Poznámku o retry|retry]].",
            tasks=["projít [[Import]]"],
        ),
        StructureConfig(),
        model="qwen3:8b",
    )

    assert "[[" not in structure.title
    assert "[[" not in structure.summary
    assert "[[" not in structure.tasks[0]
    assert structure.title == "Dodělat Import"
    assert structure.tasks == ["projít Import"]


# --- titulek --------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Přepracovat retry logiku", "Přepracovat retry logiku"),
        ('"Titulek v uvozovkách"', "Titulek v uvozovkách"),
        ("Titulek s tečkou.", "Titulek s tečkou"),
        ("víc\nřádků\nnajednou", "víc řádků najednou"),
        ("   mezery   navíc   ", "mezery navíc"),
    ],
)
def test_clean_title(raw, expected):
    assert clean_title(raw) == expected


def test_long_title_is_trimmed_at_a_word_boundary():
    title = clean_title("velmi " * 40)

    assert len(title) <= 100
    assert not title.endswith(" ")
    assert title.split()[-1] == "velmi"


def test_missing_title_is_a_failure():
    with pytest.raises(StructuringFailed, match="titulek"):
        parse_structure(payload(title="   "), StructureConfig(), model="m")


# --- rozbitý JSON ---------------------------------------------------------


def test_json_in_a_markdown_fence_still_parses():
    raw = '```json\n{"title": "Něco", "summary": "s", "tags": [], "tasks": []}\n```'

    assert extract_json(raw)["title"] == "Něco"


def test_json_with_chatter_around_it_still_parses():
    raw = 'Jasně, tady je výsledek:\n{"title": "Něco", "tags": ["napad"]}\nDoufám že pomůže.'

    assert extract_json(raw)["title"] == "Něco"


@pytest.mark.parametrize("raw", ["", "vůbec žádný json", "[1, 2, 3]", "{nedopsaný"])
def test_unparseable_answers_fail_loudly(raw):
    with pytest.raises(StructuringFailed):
        extract_json(raw)


def test_missing_fields_degrade_instead_of_failing():
    structure = parse_structure('{"title": "Jen titulek"}', StructureConfig(), model="m")

    assert structure.title == "Jen titulek"
    assert structure.summary == ""
    assert structure.tags == []
    assert structure.tasks == []


def test_tasks_are_cleaned_and_capped():
    tasks = normalize_tasks(
        ["- první", "* první", "  druhý  ", "", 42, "třetí"], limit=2
    )

    assert tasks == ["první", "druhý"]


# --- poznámka z přepisu ---------------------------------------------------


def test_structured_note_has_everything(env):
    queue_recording(env, audio_bytes(), when="2026-08-14T143211")
    fake = FakeStructurer()

    stats = run(
        env.worker,
        transcriber=FakeTranscriber("Import padá na timeoutu při velkém souboru."),
        structurer=fake,
    )

    assert (stats.created, stats.failed) == (1, 0)
    (note,) = env.notes()
    text = note.read_text(encoding="utf-8")

    assert note.name == "2026-08-14T143211-prepracovat-retry-logiku-v-importu.md"
    assert "# Přepracovat retry logiku v importu" in text
    assert "Import padá na timeoutu u velkých souborů." in text
    assert "tags: [napad, prace]" in text
    assert "structure_model: qwen3:8b" in text
    assert "status: inbox" in text
    assert "- [ ] zvýšit timeout v importu" in text
    # Surový přepis zůstává, i když ho model shrnul.
    assert "## Přepis" in text
    assert "Import padá na timeoutu při velkém souboru." in text
    assert fake.calls == ["Import padá na timeoutu při velkém souboru."]


def test_failed_structuring_falls_back_to_the_transcript(env):
    """Titulek z prvních slov přepisu, needs-review, přepis zachovaný."""
    queue_recording(env, audio_bytes())

    stats = run(
        env.worker,
        transcriber=FakeTranscriber("Přepracovat retry logiku v importu. Padá to."),
        structurer=FakeStructurer(StructuringFailed("model vrátil nesmysl")),
    )

    assert (stats.created, stats.failed) == (1, 1)
    text = env.notes()[0].read_text(encoding="utf-8")

    assert f"status: {STATUS_NEEDS_REVIEW}" in text
    assert "# Přepracovat retry logiku v importu" in text
    assert "model vrátil nesmysl" in text
    assert "structure_model" not in text
    assert "Přepracovat retry logiku v importu. Padá to." in text


def test_unavailable_backend_is_reported_once(env, caplog):
    """Nedostupná ollama nesmí u každé poznámky čekat na timeout."""
    queue_recording(env, audio_bytes(b"jedna"), when="2026-08-14T100000")
    queue_recording(env, audio_bytes(b"dva"), when="2026-08-14T110000")
    fake = FakeStructurer(available=False)

    with caplog.at_level("WARNING"):
        stats = run(env.worker, transcriber=FakeTranscriber(), structurer=fake)

    assert stats.created == 2
    assert fake.calls == []  # ani jeden pokus
    assert caplog.text.count("není dostupný") == 1
    bodies = [note.read_text(encoding="utf-8") for note in env.notes()]
    assert all(STATUS_NEEDS_REVIEW in body for body in bodies)


def test_structuring_disabled_keeps_phase_two_behaviour(env):
    queue_recording(env, audio_bytes())

    stats = run(env.worker, transcriber=FakeTranscriber("Nějaká myšlenka o importu."))

    assert stats.created == 1
    text = env.notes()[0].read_text(encoding="utf-8")
    assert f"status: {STATUS_INBOX}" in text
    assert "structure_model" not in text
    assert "## Přepis" in text


def test_rejected_recording_never_reaches_the_llm(env):
    """Nahrávka bez řeči se nesmí posílat modelu — není co strukturovat."""
    from voicenotes.worker.transcribe import NoSpeechFound

    queue_recording(env, audio_bytes())
    fake = FakeStructurer()

    stats = run(
        env.worker,
        transcriber=FakeTranscriber(NoSpeechFound("ticho")),
        structurer=fake,
    )

    assert stats.rejected == 1
    assert fake.calls == []


def test_whisper_is_unloaded_before_the_llm_runs(env):
    """Na 6 GB VRAM se whisper a LLM nevejdou zároveň."""
    queue_recording(env, audio_bytes(b"jedna"), when="2026-08-14T100000")
    queue_recording(env, audio_bytes(b"dva"), when="2026-08-14T110000")
    order: list[str] = []

    class Watched(FakeTranscriber):
        def transcribe(self, path):
            order.append("transcribe")
            return super().transcribe(path)

        def unload(self):
            order.append("unload-whisper")
            super().unload()

    class WatchedStructurer(FakeStructurer):
        def structure(self, transcript):
            order.append("structure")
            return super().structure(transcript)

    run(env.worker, transcriber=Watched(), structurer=WatchedStructurer())

    # Oba přepisy proběhnou, pak se whisper uvolní, teprve pak jde LLM.
    assert order.index("unload-whisper") > order.index("transcribe")
    assert order.index("unload-whisper") < order.index("structure")
    assert order.count("transcribe") == 2
    assert order.count("unload-whisper") == 1


def test_structurer_is_released_after_the_run(env):
    queue_recording(env, audio_bytes())
    fake = FakeStructurer()

    run(env.worker, transcriber=FakeTranscriber(), structurer=fake)

    assert fake.unloaded == 1


# --- backend: ollama ------------------------------------------------------


def make_ollama(config: StructureConfig | None = None, *, response=None, error=None):
    calls: list[dict] = []

    def post(url, payload, timeout):
        calls.append({"url": url, "payload": payload, "timeout": timeout})
        if error is not None:
            raise error
        return response or {"message": {"content": payload_default()}}

    structurer = OllamaStructurer(config or StructureConfig(), OllamaConfig(), post=post)
    return structurer, calls


def payload_default() -> str:
    return payload()


def test_ollama_request_carries_prompt_and_schema():
    structurer, calls = make_ollama()

    structurer.structure("Nějaký přepis.")

    (call,) = calls
    assert call["url"].endswith("/api/chat")
    body = call["payload"]
    assert body["model"] == "qwen3:8b"
    assert body["stream"] is False
    # Uzavřený seznam tagů jde do schématu i do promptu.
    assert body["format"]["properties"]["tags"]["items"]["enum"] == list(TAGS)
    assert body["messages"][0]["role"] == "system"
    assert "napad" in body["messages"][0]["content"]
    assert "Nějaký přepis." in body["messages"][1]["content"]


def test_ollama_parses_the_answer():
    structurer, _ = make_ollama()

    structure = structurer.structure("cokoliv")

    assert structure.title == "Přepracovat retry logiku v importu"
    assert structure.tags == ["napad"]
    assert structure.model == "qwen3:8b"


def test_ollama_connection_error_is_a_structuring_failure():
    structurer, _ = make_ollama(error=OSError("connection refused"))

    with pytest.raises(StructuringFailed, match="connection refused"):
        structurer.structure("cokoliv")


def test_ollama_empty_answer_is_a_failure():
    structurer, _ = make_ollama(response={"message": {"content": "  "}})

    with pytest.raises(StructuringFailed):
        structurer.structure("cokoliv")


def test_ollama_unload_releases_vram():
    structurer, calls = make_ollama()

    structurer.unload()

    (call,) = calls
    assert call["url"].endswith("/api/generate")
    assert call["payload"]["keep_alive"] == 0


# --- backend: anthropic ---------------------------------------------------


class FakeMessage:
    def __init__(self, text: str, stop_reason: str = "end_turn"):
        self.stop_reason = stop_reason
        self.content = [type("Block", (), {"type": "text", "text": text})()]


class FakeClient:
    def __init__(self, message):
        self.message = message
        self.kwargs: dict = {}
        self.messages = self

    def create(self, **kwargs):
        self.kwargs = kwargs
        if isinstance(self.message, Exception):
            raise self.message
        return self.message


def make_anthropic(message, config: StructureConfig | None = None):
    client = FakeClient(message)
    structurer = AnthropicStructurer(
        config or StructureConfig(backend="anthropic"),
        AnthropicConfig(),
        client_factory=lambda _config: client,
    )
    return structurer, client


def test_anthropic_request_uses_structured_outputs():
    structurer, client = make_anthropic(FakeMessage(payload()))

    structure = structurer.structure("Nějaký přepis.")

    assert structure.title == "Přepracovat retry logiku v importu"
    assert structure.model == "claude-opus-5"
    kwargs = client.kwargs
    assert kwargs["model"] == "claude-opus-5"
    assert kwargs["output_config"]["format"]["type"] == "json_schema"
    assert kwargs["output_config"]["format"]["schema"]["properties"]["tags"]["items"][
        "enum"
    ] == list(TAGS)
    # Sampling parametry jsou na claude-opus-5 odmítané.
    assert "temperature" not in kwargs
    assert "top_p" not in kwargs


def test_anthropic_refusal_is_a_structuring_failure():
    structurer, _ = make_anthropic(FakeMessage("", stop_reason="refusal"))

    with pytest.raises(StructuringFailed, match="odmítl"):
        structurer.structure("cokoliv")


def test_anthropic_truncated_answer_is_a_failure():
    """Useknutá odpověď by dala rozbitý JSON — radši rovnou fallback."""
    structurer, _ = make_anthropic(FakeMessage(payload()[:40], stop_reason="max_tokens"))

    with pytest.raises(StructuringFailed, match="max_tokens"):
        structurer.structure("cokoliv")


def test_anthropic_api_error_is_a_structuring_failure():
    structurer, _ = make_anthropic(RuntimeError("connection reset"))

    with pytest.raises(StructuringFailed, match="connection reset"):
        structurer.structure("cokoliv")


def test_anthropic_without_a_key_is_unavailable(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    structurer, _ = make_anthropic(FakeMessage(payload()))

    assert structurer.available() is False


def test_anthropic_with_a_key_is_available(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    structurer, _ = make_anthropic(FakeMessage(payload()))

    assert structurer.available() is True


def test_anthropic_request_matches_the_real_sdk():
    """Kontrakt s SDK — překlep v parametru by jinak vyšel najevo až v provozu."""
    anthropic = pytest.importorskip("anthropic")
    import inspect

    signature = inspect.signature(anthropic.Anthropic(api_key="x").messages.create)
    structurer, _ = make_anthropic(FakeMessage(payload()))

    unknown = set(structurer.request_kwargs("text")) - set(signature.parameters)
    assert unknown == set()


# --- výběr backendu -------------------------------------------------------


def test_backend_is_switched_by_one_config_key():
    assert isinstance(build_structurer(StructureConfig(backend="ollama")), OllamaStructurer)
    assert isinstance(
        build_structurer(StructureConfig(backend="anthropic")), AnthropicStructurer
    )


def test_both_backends_get_the_same_prompt_and_schema():
    """Porovnání dává smysl jen když se liší model, ne zadání."""
    config = StructureConfig()
    ollama = OllamaStructurer(config, OllamaConfig(), post=lambda *a: {})
    claude = AnthropicStructurer(config, AnthropicConfig(), client_factory=lambda c: None)

    ollama_body = ollama.request_payload("stejný přepis")
    claude_body = claude.request_kwargs("stejný přepis")

    assert ollama_body["messages"][0]["content"] == claude_body["system"]
    assert ollama_body["messages"][1]["content"] == claude_body["messages"][0]["content"]
    assert ollama_body["format"] == claude_body["output_config"]["format"]["schema"]


def test_structure_survives_a_model_that_ignores_the_schema():
    """I když model vrátí tagy jako text a úkoly jako čísla, poznámka vznikne."""
    structure = parse_structure(
        '{"title": "Něco", "summary": "s", "tags": "napad", "tasks": [1, 2]}',
        StructureConfig(),
        model="m",
    )

    assert structure == Structure(title="Něco", summary="s", tags=[], tasks=[], model="m")
