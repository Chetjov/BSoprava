"""Porovnání whisper modelů na vlastních nahrávkách.

Turbo je výrazně rychlejší, ale destilace ubližuje menším jazykům víc než
angličtině — tenhle skript to změří na nahrávkách z kapsy a z větru, ne na
čistém záznamu od stolu.

Všechny modely dostávají **stejný config** (jazyk, VAD, initial_prompt),
takže se liší jen model. Vedle času přepisu se počítá RTF (poměr času
přepisu k délce nahrávky) — to je jediné číslo, které jde srovnat napříč
různě dlouhými nahrávkami.

    voicenotes-bench --audio-dir ~/nahravky --models large-v3,large-v3-turbo,medium
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

from .config import ConfigError, TranscribeConfig, load_worker_config
from .worker.transcribe import (
    NoSpeechFound,
    Transcriber,
    TranscriberUnavailable,
    TranscriptionFailed,
    build_transcriber,
)

log = logging.getLogger("voicenotes.bench")

DEFAULT_MODELS = ("large-v3", "large-v3-turbo", "medium")
AUDIO_SUFFIXES = (".m4a", ".mp3", ".wav", ".caf", ".mp4", ".aac", ".ogg", ".flac")

TranscriberFactory = Callable[[TranscribeConfig], Transcriber]


@dataclass
class Measurement:
    model: str
    audio: str
    elapsed_s: float = 0.0
    duration_s: int | None = None
    speech_s: int | None = None
    text: str = ""
    error: str | None = None

    @property
    def rtf(self) -> float | None:
        """Real-time factor: pod 1.0 je přepis rychlejší než poslech."""
        if not self.duration_s:
            return None
        return self.elapsed_s / self.duration_s


def find_audio(directory: Path) -> list[Path]:
    if not directory.is_dir():
        raise ConfigError(f"složka s nahrávkami neexistuje: {directory}")
    return sorted(
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() in AUDIO_SUFFIXES
    )


def measure_model(
    base: TranscribeConfig,
    model: str,
    audio_files: Sequence[Path],
    *,
    factory: TranscriberFactory,
    clock: Callable[[], float] = time.perf_counter,
) -> list[Measurement]:
    """Jeden model přes všechny nahrávky. Model se po dojetí uvolní z paměti."""
    config = dataclasses.replace(base, model=model)
    results: list[Measurement] = []
    try:
        transcriber = factory(config)
    except TranscriberUnavailable as exc:
        log.error("model %s se nepodařilo připravit: %s", model, exc)
        return [Measurement(model=model, audio=path.name, error=str(exc)) for path in audio_files]

    try:
        for path in audio_files:
            started = clock()
            try:
                transcript = transcriber.transcribe(path)
            except NoSpeechFound:
                results.append(
                    Measurement(
                        model=model,
                        audio=path.name,
                        elapsed_s=clock() - started,
                        error="VAD nenašel řeč",
                    )
                )
                continue
            except (TranscriptionFailed, TranscriberUnavailable) as exc:
                results.append(
                    Measurement(
                        model=model,
                        audio=path.name,
                        elapsed_s=clock() - started,
                        error=str(exc),
                    )
                )
                continue
            results.append(
                Measurement(
                    model=model,
                    audio=path.name,
                    elapsed_s=clock() - started,
                    duration_s=transcript.duration_s,
                    speech_s=transcript.speech_s,
                    text=transcript.text,
                )
            )
            log.info("%s / %s: %.1f s", model, path.name, results[-1].elapsed_s)
    finally:
        transcriber.unload()
    return results


def run_benchmark(
    base: TranscribeConfig,
    audio_files: Sequence[Path],
    models: Iterable[str],
    *,
    factory: TranscriberFactory = build_transcriber,
    clock: Callable[[], float] = time.perf_counter,
) -> list[Measurement]:
    results: list[Measurement] = []
    for model in models:
        log.info("--- %s ---", model)
        results.extend(measure_model(base, model, audio_files, factory=factory, clock=clock))
    return results


# --- výstup ---------------------------------------------------------------


def _cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def render_report(
    results: Sequence[Measurement], models: Sequence[str], base: TranscribeConfig
) -> str:
    audio_names: list[str] = []
    for item in results:
        if item.audio not in audio_names:
            audio_names.append(item.audio)
    index = {(item.model, item.audio): item for item in results}

    lines = [
        "# Porovnání whisper modelů",
        "",
        f"Nahrávek: {len(audio_names)} · jazyk: `{base.language}` · "
        f"VAD: `{'zapnutý' if base.vad else 'vypnutý'}`",
        "",
        "Všechny modely běžely se stejným configem, liší se jen model.",
        "RTF (real-time factor) = čas přepisu ÷ délka nahrávky; pod 1.0 je",
        "přepis rychlejší než poslech.",
        "",
        "## Rychlost",
        "",
        "| nahrávka | délka | " + " | ".join(f"{model} (s / RTF)" for model in models) + " |",
        "|---|---|" + "---|" * len(models),
    ]

    for audio in audio_names:
        duration = next(
            (
                index[(model, audio)].duration_s
                for model in models
                if (model, audio) in index and index[(model, audio)].duration_s
            ),
            None,
        )
        cells = []
        for model in models:
            item = index.get((model, audio))
            if item is None:
                cells.append("—")
            elif item.error:
                cells.append(f"chyba: {_cell(item.error)}")
            else:
                rtf = f"{item.rtf:.2f}" if item.rtf is not None else "?"
                cells.append(f"{item.elapsed_s:.1f} / {rtf}")
        lines.append(
            f"| {_cell(audio)} | {duration or '?'} s | " + " | ".join(cells) + " |"
        )

    lines += ["", "## Souhrn", "", "| model | celkem s | průměrné RTF | chyb |", "|---|---|---|---|"]
    for model in models:
        items = [item for item in results if item.model == model]
        rtfs = [item.rtf for item in items if item.rtf is not None]
        errors = sum(1 for item in items if item.error)
        average = f"{sum(rtfs) / len(rtfs):.2f}" if rtfs else "?"
        lines.append(
            f"| {model} | {sum(item.elapsed_s for item in items):.1f} | {average} | {errors} |"
        )

    lines += ["", "## Přepisy k ručnímu porovnání", ""]
    for audio in audio_names:
        lines += [f"### {audio}", ""]
        for model in models:
            item = index.get((model, audio))
            lines.append(f"**{model}**")
            lines.append("")
            if item is None:
                lines.append("> (neproběhlo)")
            elif item.error:
                lines.append(f"> chyba: {item.error}")
            else:
                speech = f", řeč {item.speech_s} s" if item.speech_s is not None else ""
                lines.append(f"> {item.text or '(prázdný přepis)'}")
                lines.append("")
                lines.append(f"_{item.elapsed_s:.1f} s{speech}_")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="porovnání whisper modelů")
    parser.add_argument("--config", default=None, help="cesta ke config.yaml")
    parser.add_argument("--audio-dir", required=True, help="složka s nahrávkami")
    parser.add_argument(
        "--models",
        default=",".join(DEFAULT_MODELS),
        help=f"modely oddělené čárkou (výchozí: {','.join(DEFAULT_MODELS)})",
    )
    parser.add_argument("--out", default=None, help="kam zapsat markdown report")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    try:
        worker = load_worker_config(args.config)
        audio_files = find_audio(Path(args.audio_dir).expanduser())
    except ConfigError as exc:
        print(f"chyba: {exc}", file=sys.stderr)
        return 2

    if not audio_files:
        print(f"ve složce {args.audio_dir} nejsou žádné nahrávky", file=sys.stderr)
        return 1

    models = [model.strip() for model in args.models.split(",") if model.strip()]
    results = run_benchmark(worker.transcribe, audio_files, models)
    report = render_report(results, models, worker.transcribe)

    if args.out:
        Path(args.out).expanduser().write_text(report, encoding="utf-8")
        print(f"report zapsán do {args.out}")
    else:
        print(report)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
