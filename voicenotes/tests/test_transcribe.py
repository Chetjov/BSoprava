"""Fáze 2 — přepis. Co se stane, když whisper nenajde řeč, spadne nebo se nenačte."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest
from conftest import FakeTranscriber, audio_bytes, tweak_worker

from voicenotes.config import TranscribeConfig
from voicenotes.worker import run as worker_run
from voicenotes.worker.ledger import Ledger
from voicenotes.worker.notes import STATUS_REJECTED
from voicenotes.worker.run import run
from voicenotes.worker.transcribe import (
    FasterWhisperTranscriber,
    NoSpeechFound,
    TranscriberUnavailable,
    TranscriptionFailed,
    default_model_factory,
)


def queue_recording(env, data: bytes, *, when: str = "2026-08-14T143211") -> str:
    env.queue_dir.mkdir(parents=True, exist_ok=True)
    name = f"{when}-{hashlib.sha256(data).hexdigest()[:6]}.m4a"
    (env.queue_dir / name).write_bytes(data)
    return name


def fake_model(
    segments: list[str],
    *,
    duration: float = 52.4,
    duration_after_vad: float = 47.3,
    raises=None,
):
    """Napodobenina `WhisperModel` — zapamatuje si, s čím ji zavolali."""

    class Model:
        def __init__(self) -> None:
            self.kwargs: dict = {}
            self.path: str | None = None

        def transcribe(self, path, **kwargs):
            self.path = path
            self.kwargs = kwargs
            if raises is not None:
                raise raises
            info = SimpleNamespace(
                duration=duration, duration_after_vad=duration_after_vad, language="cs"
            )
            return (SimpleNamespace(text=text) for text in segments), info

    return Model()


# --- poznámka z přepisu --------------------------------------------------


def test_transcript_becomes_a_note(env):
    name = queue_recording(env, audio_bytes())
    fake = FakeTranscriber("Přepracovat retry logiku v importu. Padá to na timeoutu.")

    stats = run(env.worker, transcriber=fake)

    assert (stats.created, stats.rejected, stats.failed) == (1, 0, 0)
    (note,) = env.notes()
    text = note.read_text(encoding="utf-8")

    assert "status: inbox" in text
    assert "transcript_model: large-v3" in text
    assert "duration_s: 52" in text
    assert "speech_s: 47" in text
    assert "## Přepis" in text
    # Surový přepis je jediná pravda — musí být v poznámce doslova.
    assert "Přepracovat retry logiku v importu. Padá to na timeoutu." in text
    assert fake.calls == [name]


def test_title_and_filename_come_from_the_transcript(env):
    queue_recording(env, audio_bytes(), when="2026-08-14T143211")

    run(env.worker, transcriber=FakeTranscriber("Přepracovat retry logiku v importu."))

    (note,) = env.notes()
    assert note.name == "2026-08-14T143211-prepracovat-retry-logiku-v-importu.md"
    assert "# Přepracovat retry logiku v importu" in note.read_text(encoding="utf-8")


def test_transcription_is_skipped_when_disabled(env):
    """Fáze 1 zůstává dostupná pro ověření transportu bez modelu."""
    queue_recording(env, audio_bytes())

    stats = run(env.worker)  # fixture má transcribe.enabled: false

    assert stats.created == 1
    text = env.notes()[0].read_text(encoding="utf-8")
    assert "## Přepis" not in text
    assert "transcript_model" not in text
    assert "status: needs-review" in text


# --- prázdná nahrávka (VAD) ----------------------------------------------


def test_recording_without_speech_creates_no_note(env, caplog):
    """Falešné spuštění v kapse: do archive/rejected/, poznámka nevzniká, log."""
    name = queue_recording(env, audio_bytes())
    fake = FakeTranscriber(NoSpeechFound("VAD nenašel řeč"))

    with caplog.at_level("INFO"):
        stats = run(env.worker, transcriber=fake)

    assert (stats.created, stats.rejected) == (0, 1)
    assert env.notes() == []
    assert (env.archive_dir / "rejected" / name).exists()
    assert not (env.archive_dir / name).exists()
    assert list(env.queue_dir.iterdir()) == []
    assert "rejected" in caplog.text


def test_rejected_recording_is_not_processed_again(env):
    name = queue_recording(env, audio_bytes())
    run(env.worker, transcriber=FakeTranscriber(NoSpeechFound("ticho")))
    # Nahrávka se do fronty vrátí (ruční kopie, obnova ze zálohy).
    (env.queue_dir / name).write_bytes(audio_bytes())

    stats = run(env.worker, transcriber=FakeTranscriber("Tentokrát je tam řeč."))

    assert (stats.created, stats.skipped) == (0, 1)
    assert env.notes() == []


def test_rejected_recording_is_recorded_in_the_ledger(env):
    name = queue_recording(env, audio_bytes())

    run(env.worker, transcriber=FakeTranscriber(NoSpeechFound("ticho")))

    ledger = Ledger(env.worker.ledger_path)
    assert ledger.processed_ids() == {name.removesuffix(".m4a")}
    entry = ledger.path.read_text(encoding="utf-8")
    assert f'"status": "{STATUS_REJECTED}"' in entry
    assert '"note": null' in entry


# --- selhání přepisu -----------------------------------------------------


def test_failed_transcription_still_creates_a_note(env):
    """Po vyčerpání pokusů poznámka vznikne s chybou v těle — tiše zmizet nesmí."""
    name = queue_recording(env, audio_bytes())
    tweak_worker(env, transcribe={"enabled": False, "max_attempts": 1})
    fake = FakeTranscriber(TranscriptionFailed("RuntimeError: CUDA out of memory"))

    stats = run(env.worker, transcriber=fake)

    assert (stats.created, stats.failed, stats.rejected) == (1, 1, 0)
    (note,) = env.notes()
    text = note.read_text(encoding="utf-8")

    assert "status: needs-review" in text
    assert "## Chyba" in text
    assert "CUDA out of memory" in text
    assert "transcript_model" not in text
    # Audio zůstává na Pi v archivu, ne ve frontě donekonečna.
    assert (env.archive_dir / name).exists()
    assert list(env.queue_dir.iterdir()) == []


def test_failed_transcription_does_not_block_the_others(env):
    good = queue_recording(env, audio_bytes(b"dobra"), when="2026-08-14T100000")
    bad = queue_recording(env, audio_bytes(b"spatna"), when="2026-08-14T110000")
    tweak_worker(env, transcribe={"enabled": False, "max_attempts": 1})
    fake = FakeTranscriber(
        "Tahle nahrávka je v pořádku.",
        {bad: TranscriptionFailed("dekódování selhalo")},
    )

    stats = run(env.worker, transcriber=fake)

    assert stats.created == 2
    assert sorted(fake.calls) == sorted([good, bad])
    bodies = "\n".join(note.read_text(encoding="utf-8") for note in env.notes())
    assert "dekódování selhalo" in bodies
    assert "Tahle nahrávka je v pořádku." in bodies


# --- model se nenačte ----------------------------------------------------


def test_model_load_failure_aborts_the_run(env):
    """Systémová chyba nesmí vysypat do vaultu sto poznámek s chybou."""
    name = queue_recording(env, audio_bytes(b"jedna"), when="2026-08-14T100000")
    other = queue_recording(env, audio_bytes(b"dva"), when="2026-08-14T110000")
    fake = FakeTranscriber(TranscriberUnavailable("model se nenačetl"))

    with pytest.raises(TranscriberUnavailable):
        run(env.worker, transcriber=fake)

    assert env.notes() == []
    # Fronta zůstala nedotčená, další běh to zkusí znovu.
    assert (env.queue_dir / name).exists()
    assert (env.queue_dir / other).exists()
    assert len(fake.calls) == 1  # skončilo se u první nahrávky
    assert Ledger(env.worker.ledger_path).processed_ids() == set()


def test_model_load_failure_is_reported_by_the_cli(env, monkeypatch, capsys):
    queue_recording(env, audio_bytes())
    monkeypatch.setattr(
        worker_run,
        "build_transcriber",
        lambda config: FakeTranscriber(TranscriberUnavailable("chybí faster-whisper")),
    )
    config_text = env.config_path.read_text(encoding="utf-8")
    env.config_path.write_text(
        config_text.replace("enabled: false", "enabled: true"), encoding="utf-8"
    )

    assert worker_run.main(["--config", str(env.config_path)]) == 1
    assert "chybí faster-whisper" in capsys.readouterr().err
    assert env.notes() == []


def test_model_is_released_after_the_run(env):
    """Na 6 GB VRAM se whisper a LLM nevejdou zároveň."""
    queue_recording(env, audio_bytes())
    fake = FakeTranscriber()

    run(env.worker, transcriber=fake)

    assert fake.unloaded == 1


def test_model_is_released_even_when_the_run_blows_up(env):
    queue_recording(env, audio_bytes())
    fake = FakeTranscriber(TranscriberUnavailable("bum"))

    with pytest.raises(TranscriberUnavailable):
        run(env.worker, transcriber=fake)

    assert fake.unloaded == 1


def test_empty_queue_never_touches_the_model(env):
    """Worker běží každých pár minut — model se nesmí načítat pro nic."""
    env.queue_dir.mkdir(parents=True, exist_ok=True)
    loads = []
    transcriber = FasterWhisperTranscriber(
        TranscribeConfig(), model_factory=lambda config: loads.append(config)
    )

    stats = run(env.worker, transcriber=transcriber)

    assert (stats.fetched, stats.created) == (0, 0)
    assert loads == []


# --- parametry pro whisper -----------------------------------------------


def test_transcribe_options_pin_down_the_risky_defaults():
    config = TranscribeConfig(initial_prompt_terms=("Obsidian", "Tailscale"))
    options = FasterWhisperTranscriber(config).transcribe_options()

    # Autodetekce občas přepne na slovenštinu.
    assert options["language"] == "cs"
    # Bez VAD model v tichu halucinuje.
    assert options["vad_filter"] is True
    assert options["vad_parameters"]["min_silence_duration_ms"] == 500
    assert options["vad_parameters"]["threshold"] == 0.5
    # Zabraňuje zacyklení na jedné frázi.
    assert options["condition_on_previous_text"] is False
    assert options["initial_prompt"] == "Obsidian, Tailscale."


def test_vad_can_be_turned_off():
    options = FasterWhisperTranscriber(TranscribeConfig(vad=False)).transcribe_options()

    assert options["vad_filter"] is False
    assert "vad_parameters" not in options


def test_initial_prompt_is_omitted_without_a_glossary():
    options = FasterWhisperTranscriber(TranscribeConfig()).transcribe_options()

    assert "initial_prompt" not in options


def test_initial_prompt_can_be_written_by_hand():
    config = TranscribeConfig(
        initial_prompt_terms=("Obsidian",), initial_prompt_override="Vlastní nápověda."
    )

    assert config.build_initial_prompt() == "Vlastní nápověda."


def test_transcribe_options_match_the_real_faster_whisper():
    """Kontrakt s knihovnou — překlep v názvu parametru by jinak vyšel najevo
    až na notebooku s načteným modelem."""
    faster_whisper = pytest.importorskip("faster_whisper")
    import inspect

    signature = inspect.signature(faster_whisper.WhisperModel.transcribe)
    options = FasterWhisperTranscriber(
        TranscribeConfig(initial_prompt_terms=("Obsidian",))
    ).transcribe_options()

    unknown = set(options) - set(signature.parameters)
    assert unknown == set()


def test_model_constructor_matches_the_real_faster_whisper(monkeypatch):
    """Totéž pro konstruktor modelu — `download_root` a spol. musí sedět."""
    faster_whisper = pytest.importorskip("faster_whisper")
    import inspect

    # Podpis skutečné třídy je potřeba získat dřív, než ji nahradí špion.
    signature = inspect.signature(faster_whisper.WhisperModel.__init__)
    captured: dict = {}

    class Spy:
        def __init__(self, model_size_or_path, **kwargs):
            captured["model"] = model_size_or_path
            captured["kwargs"] = kwargs

    monkeypatch.setattr(faster_whisper, "WhisperModel", Spy)
    default_model_factory(
        TranscribeConfig(model="large-v3", device="cuda", compute_type="float16")
    )

    assert captured["model"] == "large-v3"
    assert captured["kwargs"] == {"device": "cuda", "compute_type": "float16"}
    assert set(captured["kwargs"]) - set(signature.parameters) == set()


def test_model_factory_gets_the_configured_model(tmp_path):
    captured = {}

    def factory(config):
        captured["model"] = config.model
        captured["device"] = config.device
        return fake_model(["ahoj"])

    transcriber = FasterWhisperTranscriber(
        TranscribeConfig(model="large-v3-turbo", device="cuda"), model_factory=factory
    )
    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"x")
    transcriber.transcribe(audio)

    assert captured == {"model": "large-v3-turbo", "device": "cuda"}


# --- chování přepisovače -------------------------------------------------


def test_segments_are_joined_into_one_transcript(tmp_path):
    model = fake_model([" Ahoj, ", "tady je ", "myšlenka. "])
    transcriber = FasterWhisperTranscriber(TranscribeConfig(), model_factory=lambda c: model)
    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"x")

    result = transcriber.transcribe(audio)

    assert result.text == "Ahoj, tady je myšlenka."
    assert result.duration_s == 52
    assert result.speech_s == 47
    assert result.language == "cs"
    assert result.segments == 3
    assert model.kwargs["language"] == "cs"


def test_speech_length_is_omitted_without_vad(tmp_path):
    """Bez VAD se nic neořezává, takže `speech_s` nemá co říct."""
    transcriber = FasterWhisperTranscriber(
        TranscribeConfig(vad=False), model_factory=lambda c: fake_model(["ahoj"])
    )
    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"x")

    result = transcriber.transcribe(audio)

    assert result.duration_s == 52
    assert result.speech_s is None


def test_note_shows_speech_next_to_total_length(env):
    """Velký rozdíl mezi délkami znamená hodně ticha v nahrávce."""
    queue_recording(env, audio_bytes())

    run(env.worker, transcriber=FakeTranscriber(duration_s=305, speech_s=42))

    text = env.notes()[0].read_text(encoding="utf-8")
    assert "duration_s: 305" in text
    assert "speech_s: 42" in text


def test_model_is_loaded_once_for_the_whole_queue(tmp_path):
    loads = []

    def factory(config):
        loads.append(config)
        return fake_model(["ahoj"])

    transcriber = FasterWhisperTranscriber(TranscribeConfig(), model_factory=factory)
    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"x")

    transcriber.transcribe(audio)
    transcriber.transcribe(audio)

    assert len(loads) == 1


def test_silence_from_vad_is_no_speech(tmp_path):
    """VAD ustřihne všechno → segmenty jsou prázdné, poznámka nevzniká."""
    transcriber = FasterWhisperTranscriber(
        TranscribeConfig(), model_factory=lambda c: fake_model(["  ", ""])
    )
    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"x")

    with pytest.raises(NoSpeechFound):
        transcriber.transcribe(audio)


def test_decoding_error_is_a_per_file_failure(tmp_path):
    transcriber = FasterWhisperTranscriber(
        TranscribeConfig(),
        model_factory=lambda c: fake_model([], raises=RuntimeError("nelze dekódovat")),
    )
    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"x")

    with pytest.raises(TranscriptionFailed, match="nelze dekódovat"):
        transcriber.transcribe(audio)


def test_broken_model_load_is_a_run_level_failure(tmp_path):
    def factory(config):
        raise OSError("cuda knihovna chybí")

    transcriber = FasterWhisperTranscriber(TranscribeConfig(), model_factory=factory)
    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"x")

    with pytest.raises(TranscriberUnavailable, match="cuda knihovna chybí"):
        transcriber.transcribe(audio)


def test_unload_releases_the_model(tmp_path):
    loads = []
    transcriber = FasterWhisperTranscriber(
        TranscribeConfig(),
        model_factory=lambda c: (loads.append(c), fake_model(["ahoj"]))[1],
    )
    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"x")

    transcriber.transcribe(audio)
    transcriber.unload()
    transcriber.transcribe(audio)

    assert len(loads) == 2
