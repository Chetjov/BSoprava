"""Strukturování přepisu: titulek, shrnutí, tagy, úkoly.

Dva vyměnitelné backendy za jedním rozhraním — `ollama` (lokálně) a
`anthropic` (Claude API). Oba dostávají **stejný prompt a stejné schéma**,
takže se dají porovnat na stejných datech; liší se jen model.

Co si pipeline hlídá sama, ne modelem:
  * tagy jen z uzavřeného seznamu v configu — jinak jich je za dva měsíce
    dvě stě a vault je k ničemu,
  * žádné wikilinks — model neví, co ve vaultu existuje, a vyrobil by
    odkazy na neexistující soubory,
  * titulek na jeden řádek a rozumnou délku, ať z něj jde udělat název souboru.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable

from ..config import AnthropicConfig, OllamaConfig, StructureConfig

log = logging.getLogger("voicenotes.worker.structure")

MAX_TITLE_CHARS = 100

_WIKILINK_RE = re.compile(r"\[\[([^\]|]*)(?:\|([^\]]*))?\]\]")
_WHITESPACE_RE = re.compile(r"\s+")
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


class StructuringFailed(RuntimeError):
    """Strukturování nedopadlo. Poznámka vznikne s titulkem z přepisu."""


@dataclass(frozen=True)
class Structure:
    title: str
    summary: str = ""
    tags: list[str] = field(default_factory=list)
    tasks: list[str] = field(default_factory=list)
    model: str = ""


# --- čištění výstupu modelu ----------------------------------------------


def strip_wikilinks(text: str) -> str:
    """`[[cíl|popis]]` → `popis`, `[[cíl]]` → `cíl`.

    Model o vaultu nic neví; kdyby odkazy prošly, Obsidian by je ukazoval
    jako prázdné poznámky.
    """
    return _WIKILINK_RE.sub(lambda m: (m.group(2) or m.group(1)).strip(), text)


def clean_line(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", strip_wikilinks(text)).strip()


def clean_title(text: str) -> str:
    """Titulek na jeden řádek — dělá se z něj i název souboru."""
    title = clean_line(text).strip("\"'`").strip()
    title = title.rstrip(" .;:,")
    if len(title) > MAX_TITLE_CHARS:
        title = title[:MAX_TITLE_CHARS].rsplit(" ", 1)[0] or title[:MAX_TITLE_CHARS]
    return title.strip()


def _fold(value: str) -> str:
    """Porovnávací tvar tagu: bez diakritiky, malými písmeny."""
    normalized = unicodedata.normalize("NFKD", value.strip().lstrip("#"))
    stripped = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return stripped.lower()


def normalize_tags(values: Any, allowed: tuple[str, ...]) -> list[str]:
    """Tagy mimo uzavřený seznam zahoď; `nápad` uznej jako `napad`."""
    if not isinstance(values, list):
        return []
    lookup = {_fold(tag): tag for tag in allowed}
    picked: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        match = lookup.get(_fold(value))
        if match is None:
            log.debug("zahozen tag mimo seznam: %r", value)
            continue
        if match not in picked:
            picked.append(match)
    return [tag for tag in allowed if tag in picked]


def normalize_tasks(values: Any, *, limit: int) -> list[str]:
    if not isinstance(values, list):
        return []
    tasks: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        task = clean_line(value).lstrip("-*[ ]").strip()
        if task and task not in tasks:
            tasks.append(task)
    return tasks[:limit]


def extract_json(raw: str) -> dict[str, Any]:
    """JSON z odpovědi modelu, i když ho obalí do ```json nebo do textu."""
    candidates = [raw.strip(), _FENCE_RE.sub("", raw.strip())]
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end > start:
        candidates.append(raw[start : end + 1])
    for candidate in candidates:
        if not candidate:
            continue
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    raise StructuringFailed(f"odpověď modelu není JSON: {raw[:200]!r}")


def parse_structure(raw: str, config: StructureConfig, *, model: str) -> Structure:
    payload = extract_json(raw)
    title = clean_title(str(payload.get("title") or ""))
    if not title:
        raise StructuringFailed("model nevrátil titulek")
    return Structure(
        title=title,
        summary=clean_line(str(payload.get("summary") or "")),
        tags=normalize_tags(payload.get("tags"), config.tags),
        tasks=normalize_tasks(payload.get("tasks"), limit=config.max_tasks),
        model=model,
    )


# --- prompt a schéma (sdílené oběma backendy) -----------------------------


def build_system_prompt(config: StructureConfig) -> str:
    tags = ", ".join(config.tags)
    return (
        "Jsi součást pipeline, která ze surových přepisů hlasových poznámek dělá "
        "strukturované poznámky do Obsidianu. Odpovídej výhradně jedním JSON objektem, "
        "bez komentáře okolo.\n\n"
        "Pole:\n"
        '- "title": výstižný český titulek, 3 až 8 slov, bez uvozovek a bez tečky na konci. '
        'Vystihni obsah, ne formu — "Přepracovat retry logiku v importu", '
        'ne "Poznámka o práci".\n'
        '- "summary": shrnutí obsahu v jedné až dvou větách.\n'
        f'- "tags": pole tagů výhradně z tohoto seznamu: {tags}. '
        "Vyber jen ty, které opravdu sedí; když nesedí žádný, vrať prázdné pole. "
        "Nevymýšlej si vlastní tagy.\n"
        '- "tasks": pole konkrétních úkolů, které v nahrávce zazněly, každý jako jedna '
        "věta. Když žádný úkol nezazněl, vrať prázdné pole.\n\n"
        "Piš česky. Vycházej jen z přepisu a nic si nedomýšlej. "
        "Nikdy nepoužívej odkazy ve dvojitých hranatých závorkách."
    )


def build_schema(config: StructureConfig) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "summary": {"type": "string"},
            "tags": {
                "type": "array",
                "items": {"type": "string", "enum": list(config.tags)},
            },
            "tasks": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["title", "summary", "tags", "tasks"],
        "additionalProperties": False,
    }


def build_user_prompt(transcript: str) -> str:
    return f"Přepis nahrávky:\n\n{transcript.strip()}"


# --- rozhraní -------------------------------------------------------------


class Structurer(ABC):
    @property
    @abstractmethod
    def model(self) -> str: ...

    @abstractmethod
    def available(self) -> bool:
        """Levá kontrola před během — ať se nečeká na timeout u každé poznámky."""

    @abstractmethod
    def structure(self, transcript: str) -> Structure: ...

    def unload(self) -> None:
        """Uvolní model z paměti. Výchozí implementace nedělá nic."""


# --- ollama ---------------------------------------------------------------

HttpPost = Callable[[str, dict[str, Any], int], dict[str, Any]]


def http_post_json(url: str, payload: dict[str, Any], timeout_s: int) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


class OllamaStructurer(Structurer):
    """Lokální model přes ollama. Bez závislostí — stačí stdlib."""

    def __init__(
        self,
        config: StructureConfig,
        ollama: OllamaConfig,
        *,
        post: HttpPost | None = None,
    ) -> None:
        self.config = config
        self.ollama = ollama
        self._post = post or http_post_json

    @property
    def model(self) -> str:
        return self.ollama.model

    def available(self) -> bool:
        try:
            with urllib.request.urlopen(  # noqa: S310
                f"{self.ollama.host}/api/tags", timeout=self.ollama.connect_timeout_s
            ) as response:
                return response.status == 200
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log.warning("ollama na %s neodpovídá: %s", self.ollama.host, exc)
            return False

    def request_payload(self, transcript: str) -> dict[str, Any]:
        return {
            "model": self.ollama.model,
            "stream": False,
            "format": build_schema(self.config),
            "keep_alive": self.ollama.keep_alive,
            "options": {"temperature": self.ollama.temperature, "num_ctx": self.ollama.num_ctx},
            "messages": [
                {"role": "system", "content": build_system_prompt(self.config)},
                {"role": "user", "content": build_user_prompt(transcript)},
            ],
        }

    def structure(self, transcript: str) -> Structure:
        try:
            response = self._post(
                f"{self.ollama.host}/api/chat",
                self.request_payload(transcript),
                self.ollama.timeout_s,
            )
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise StructuringFailed(f"ollama: {exc}") from exc

        content = (response.get("message") or {}).get("content")
        if not isinstance(content, str) or not content.strip():
            raise StructuringFailed(f"ollama vrátila prázdnou odpověď: {response}")
        return parse_structure(content, self.config, model=self.ollama.model)

    def unload(self) -> None:
        """`keep_alive: 0` řekne ollamě, ať model pustí z VRAM."""
        try:
            self._post(
                f"{self.ollama.host}/api/generate",
                {"model": self.ollama.model, "keep_alive": 0},
                self.ollama.connect_timeout_s,
            )
        except (urllib.error.URLError, OSError, ValueError) as exc:  # pragma: no cover
            log.debug("model se nepodařilo uvolnit: %s", exc)


# --- anthropic ------------------------------------------------------------


class AnthropicStructurer(Structurer):
    """Claude API. Klíč se čte z prostředí, v configu je jen jméno proměnné."""

    def __init__(
        self,
        config: StructureConfig,
        anthropic: AnthropicConfig,
        *,
        client_factory: Callable[[AnthropicConfig], Any] | None = None,
    ) -> None:
        self.config = config
        self.anthropic = anthropic
        self._factory = client_factory or default_anthropic_client
        self._client: Any | None = None

    @property
    def model(self) -> str:
        return self.anthropic.model

    def available(self) -> bool:
        # Jen kontrola klíče — zkušební dotaz by stál peníze při každém běhu.
        if not self.anthropic.api_key():
            log.warning(
                "proměnná %s není nastavená, strukturování přes Claude nepoběží",
                self.anthropic.api_key_env,
            )
            return False
        return True

    def request_kwargs(self, transcript: str) -> dict[str, Any]:
        return {
            "model": self.anthropic.model,
            "max_tokens": self.anthropic.max_tokens,
            "system": build_system_prompt(self.config),
            "messages": [{"role": "user", "content": build_user_prompt(transcript)}],
            "output_config": {
                "effort": self.anthropic.effort,
                "format": {"type": "json_schema", "schema": build_schema(self.config)},
            },
        }

    def _load(self) -> Any:
        if self._client is None:
            self._client = self._factory(self.anthropic)
        return self._client

    def structure(self, transcript: str) -> Structure:
        client = self._load()
        try:
            response = client.messages.create(**self.request_kwargs(transcript))
        except Exception as exc:  # noqa: BLE001 - typy výjimek závisí na SDK
            raise StructuringFailed(f"{type(exc).__name__}: {exc}") from exc

        stop_reason = getattr(response, "stop_reason", None)
        if stop_reason == "refusal":
            raise StructuringFailed("Claude odmítl nahrávku zpracovat")
        if stop_reason == "max_tokens":
            raise StructuringFailed("odpověď se nevešla do max_tokens")

        text = "".join(
            block.text
            for block in getattr(response, "content", [])
            if getattr(block, "type", None) == "text"
        )
        if not text.strip():
            raise StructuringFailed("Claude vrátil prázdnou odpověď")
        return parse_structure(text, self.config, model=self.anthropic.model)


def default_anthropic_client(config: AnthropicConfig) -> Any:
    try:
        from anthropic import Anthropic
    except ImportError as exc:  # pragma: no cover - závisí na prostředí
        raise StructuringFailed(
            "balíček anthropic není nainstalovaný — `pip install 'voicenotes[anthropic]'`"
        ) from exc
    return Anthropic(api_key=config.api_key(), timeout=config.timeout_s)


def build_structurer(config: StructureConfig) -> Structurer:
    if config.backend == "anthropic":
        return AnthropicStructurer(config, config.anthropic)
    return OllamaStructurer(config, config.ollama)
