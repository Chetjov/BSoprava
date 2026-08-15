"""Přepis nahrávky přes faster-whisper.

Tři druhy selhání, každý s jiným následkem:

  * `TranscriberUnavailable` — model se vůbec nenačte. Je to systémová chyba,
    která by potkala každou nahrávku ve frontě, tak se běh zastaví a fronta
    zůstane nedotčená. Pipeline poznámky needituje ani nemaže, takže sto
    rozbitých poznámek by byl ruční úklid.
  * `TranscriptionFailed` — selhala tahle jedna nahrávka. Poznámka vznikne
    se `status: needs-review` a chybou v těle.
  * `NoSpeechFound` — VAD nenašel řeč (spuštění v kapse). Nahrávka jde do
    `archive/rejected/`, poznámka nevzniká.
"""

from __future__ import annotations

import gc
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..config import TranscribeConfig

log = logging.getLogger("voicenotes.worker.transcribe")


class TranscriberUnavailable(RuntimeError):
    """Model nejde načíst — zastavit celý běh, ne vyrábět poznámky s chybou."""


class TranscriptionFailed(RuntimeError):
    """Tahle nahrávka se nepřepsala. Poznámka vznikne s chybou v těle."""


class NoSpeechFound(RuntimeError):
    """VAD nenašel žádnou řeč."""


@dataclass(frozen=True)
class Transcript:
    text: str
    model: str
    language: str | None = None
    #: Celková délka nahrávky.
    duration_s: int | None = None
    #: Délka řeči po ořezu VAD. Velký rozdíl proti `duration_s` znamená
    #: hodně ticha v nahrávce a stojí za to se podívat proč.
    speech_s: int | None = None
    segments: int = 0


class Transcriber(ABC):
    @abstractmethod
    def transcribe(self, path: Path) -> Transcript: ...

    @abstractmethod
    def unload(self) -> None:
        """Uvolní model z paměti. Na 6 GB VRAM se whisper a LLM nevejdou zároveň."""


ModelFactory = Callable[[TranscribeConfig], Any]


def default_model_factory(config: TranscribeConfig) -> Any:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:  # pragma: no cover - závisí na prostředí
        raise TranscriberUnavailable(
            "faster-whisper není nainstalovaný — `pip install 'voicenotes[worker]'`"
        ) from exc
    kwargs: dict[str, Any] = {
        "device": config.device,
        "compute_type": config.compute_type,
    }
    if config.download_root:
        kwargs["download_root"] = str(config.download_root)
    return WhisperModel(config.model, **kwargs)


class FasterWhisperTranscriber(Transcriber):
    """Model se načítá až u první nahrávky — prázdná fronta nesmí sáhnout na disk."""

    def __init__(
        self,
        config: TranscribeConfig,
        *,
        model_factory: ModelFactory | None = None,
    ) -> None:
        self.config = config
        self._factory = model_factory or default_model_factory
        self._model: Any | None = None

    def transcribe_options(self) -> dict[str, Any]:
        """Parametry pro `WhisperModel.transcribe`.

        `language` je vždy explicitní — autodetekce u krátkých českých
        nahrávek občas přepne na slovenštinu.
        """
        options: dict[str, Any] = {
            "language": self.config.language,
            "beam_size": self.config.beam_size,
            "condition_on_previous_text": self.config.condition_on_previous_text,
            "vad_filter": self.config.vad,
        }
        if self.config.vad:
            # Bez VAD model v tichých pasážích halucinuje opakující se nesmysly,
            # což je u nahrávek z venku běžné.
            options["vad_parameters"] = {
                "threshold": self.config.vad_threshold,
                "min_silence_duration_ms": self.config.vad_min_silence_ms,
            }
        prompt = self.config.build_initial_prompt()
        if prompt:
            options["initial_prompt"] = prompt
        return options

    def _load(self) -> Any:
        if self._model is None:
            log.info("načítám model %s (%s)", self.config.model, self.config.device)
            try:
                self._model = self._factory(self.config)
            except TranscriberUnavailable:
                raise
            except Exception as exc:
                raise TranscriberUnavailable(
                    f"model {self.config.model} se nepodařilo načíst: {exc}"
                ) from exc
        return self._model

    def transcribe(self, path: Path) -> Transcript:
        model = self._load()
        try:
            segments, info = model.transcribe(str(path), **self.transcribe_options())
            # `segments` je generátor — přepis proběhne až tady.
            texts = [segment.text.strip() for segment in segments]
        except Exception as exc:
            raise TranscriptionFailed(f"{type(exc).__name__}: {exc}") from exc

        text = " ".join(part for part in texts if part).strip()
        if not text:
            raise NoSpeechFound(f"VAD nenašel řeč v {path.name}")

        duration = getattr(info, "duration", None)
        # Bez VAD se nic neořezává a `duration_after_vad` je jen kopie
        # celkové délky — takový údaj do frontmatteru nepatří.
        speech = getattr(info, "duration_after_vad", None) if self.config.vad else None
        return Transcript(
            text=text,
            model=self.config.model,
            language=getattr(info, "language", None),
            duration_s=round(duration) if duration else None,
            speech_s=round(speech) if speech else None,
            segments=len(texts),
        )

    def unload(self) -> None:
        if self._model is None:
            return
        # ctranslate2 drží váhy v objektu modelu; uvolní je až jeho úklid.
        self._model = None
        gc.collect()
        log.debug("model uvolněn z paměti")


def build_transcriber(
    config: TranscribeConfig, *, model_factory: ModelFactory | None = None
) -> Transcriber:
    return FasterWhisperTranscriber(config, model_factory=model_factory)
