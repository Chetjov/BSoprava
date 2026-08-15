"""Fáze 4 — benchmark whisper modelů."""

from __future__ import annotations

from pathlib import Path

import pytest

from voicenotes.bench import find_audio, measure_model, render_report, run_benchmark
from voicenotes.config import ConfigError, TranscribeConfig
from voicenotes.worker.transcribe import (
    NoSpeechFound,
    Transcriber,
    TranscriberUnavailable,
    Transcript,
    TranscriptionFailed,
)


class StubTranscriber(Transcriber):
    """Přepisovač, který vrací předem daný text a 'trvá' zadaný počet vteřin."""

    def __init__(self, model: str, texts: dict[str, str | Exception], seconds: float = 2.0):
        self._model = model
        self.texts = texts
        self.seconds = seconds
        self.unloaded = 0

    def transcribe(self, path: Path) -> Transcript:
        outcome = self.texts.get(path.name, f"přepis {path.name} z {self._model}")
        if isinstance(outcome, Exception):
            raise outcome
        return Transcript(
            text=outcome, model=self._model, duration_s=20, speech_s=15, segments=1
        )

    def unload(self) -> None:
        self.unloaded += 1


@pytest.fixture
def audio_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "nahravky"
    directory.mkdir()
    (directory / "z-kapsy.m4a").write_bytes(b"a")
    (directory / "ve-vetru.m4a").write_bytes(b"b")
    (directory / "poznamky.txt").write_text("tohle není zvuk", encoding="utf-8")
    return directory


class FakeClock:
    """Deterministický čas — každý dotaz posune hodiny o `step`."""

    def __init__(self, step: float = 3.0):
        self.step = step
        self.now = 0.0

    def __call__(self) -> float:
        value = self.now
        self.now += self.step
        return value


def test_only_audio_files_are_picked_up(audio_dir):
    names = [path.name for path in find_audio(audio_dir)]

    assert names == ["ve-vetru.m4a", "z-kapsy.m4a"]


def test_missing_directory_fails_loudly(tmp_path):
    with pytest.raises(ConfigError, match="neexistuje"):
        find_audio(tmp_path / "nikde")


def test_every_model_sees_the_same_config(audio_dir):
    """Porovnání dává smysl jen když se liší model, ne jazyk nebo VAD."""
    seen: list[TranscribeConfig] = []

    def factory(config):
        seen.append(config)
        return StubTranscriber(config.model, {})

    base = TranscribeConfig(
        language="cs", vad=True, initial_prompt_terms=("Obsidian",), model="ignorovaný"
    )
    run_benchmark(base, find_audio(audio_dir), ["large-v3", "medium"], factory=factory)

    assert [config.model for config in seen] == ["large-v3", "medium"]
    assert all(config.language == "cs" for config in seen)
    assert all(config.vad for config in seen)
    assert all(config.initial_prompt_terms == ("Obsidian",) for config in seen)


def test_measurements_carry_time_and_rtf(audio_dir):
    results = run_benchmark(
        TranscribeConfig(),
        find_audio(audio_dir),
        ["large-v3"],
        factory=lambda config: StubTranscriber(config.model, {}),
        clock=FakeClock(step=5.0),
    )

    assert len(results) == 2
    assert all(item.elapsed_s == 5.0 for item in results)
    # 5 s přepisu na 20 s zvuku
    assert all(item.rtf == 0.25 for item in results)


def test_model_is_released_between_models(audio_dir):
    made: list[StubTranscriber] = []

    def factory(config):
        made.append(StubTranscriber(config.model, {}))
        return made[-1]

    run_benchmark(TranscribeConfig(), find_audio(audio_dir), ["a", "b"], factory=factory)

    assert [stub.unloaded for stub in made] == [1, 1]


def test_a_broken_model_does_not_stop_the_others(audio_dir):
    def factory(config):
        if config.model == "rozbity":
            raise TranscriberUnavailable("váhy chybí")
        return StubTranscriber(config.model, {})

    results = run_benchmark(
        TranscribeConfig(), find_audio(audio_dir), ["rozbity", "medium"], factory=factory
    )

    broken = [item for item in results if item.model == "rozbity"]
    working = [item for item in results if item.model == "medium"]
    assert len(broken) == 2 and all(item.error for item in broken)
    assert len(working) == 2 and not any(item.error for item in working)


def test_a_failing_recording_is_recorded_not_raised(audio_dir):
    results = measure_model(
        TranscribeConfig(),
        "large-v3",
        find_audio(audio_dir),
        factory=lambda config: StubTranscriber(
            config.model,
            {
                "z-kapsy.m4a": NoSpeechFound("ticho"),
                "ve-vetru.m4a": TranscriptionFailed("dekódování selhalo"),
            },
        ),
    )

    errors = {item.audio: item.error for item in results}
    assert errors["z-kapsy.m4a"] == "VAD nenašel řeč"
    assert "dekódování selhalo" in errors["ve-vetru.m4a"]


def test_report_puts_the_texts_side_by_side(audio_dir):
    models = ["large-v3", "large-v3-turbo"]
    results = run_benchmark(
        TranscribeConfig(),
        find_audio(audio_dir),
        models,
        factory=lambda config: StubTranscriber(
            config.model, {"z-kapsy.m4a": f"co slyšel {config.model}"}
        ),
        clock=FakeClock(step=4.0),
    )

    report = render_report(results, models, TranscribeConfig())

    assert "# Porovnání whisper modelů" in report
    assert "| large-v3 (s / RTF) | large-v3-turbo (s / RTF) |" in report
    assert "### z-kapsy.m4a" in report
    # Oba přepisy téže nahrávky vedle sebe k ručnímu porovnání.
    assert "co slyšel large-v3" in report
    assert "co slyšel large-v3-turbo" in report
    assert "## Souhrn" in report
    assert "4.0" in report


def test_report_shows_errors_instead_of_hiding_them(audio_dir):
    results = run_benchmark(
        TranscribeConfig(),
        find_audio(audio_dir),
        ["medium"],
        factory=lambda config: StubTranscriber(
            config.model, {"z-kapsy.m4a": TranscriptionFailed("CUDA out of memory")}
        ),
    )

    report = render_report(results, ["medium"], TranscribeConfig())

    assert "CUDA out of memory" in report


def test_report_escapes_pipes_in_filenames(tmp_path):
    directory = tmp_path / "audio"
    directory.mkdir()
    (directory / "divny|nazev.m4a").write_bytes(b"a")

    results = run_benchmark(
        TranscribeConfig(),
        find_audio(directory),
        ["medium"],
        factory=lambda config: StubTranscriber(config.model, {}),
    )
    report = render_report(results, ["medium"], TranscribeConfig())

    # Neescapovaná roura by rozbila tabulku.
    assert "divny\\|nazev.m4a" in report
