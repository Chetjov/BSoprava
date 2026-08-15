"""Sestavení markdown poznámky. Frontmatter + tělo, nic víc.

Tvar souboru je daný zadáním; tenhle modul je jediné místo, kde se generuje,
aby se fáze 2 a 3 přidávaly doplněním polí, ne přepisem šablony.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

STATUS_INBOX = "inbox"
STATUS_NEEDS_REVIEW = "needs-review"
#: Jen do evidence — k zahozené nahrávce poznámka nevzniká.
STATUS_REJECTED = "rejected"

FALLBACK_TITLE = "Nahrávka bez titulku"

#: Znaky, po kterých už plain scalar v YAML není bezpečný.
_UNSAFE_START = set("-?:,[]{}#&*!|>'\"%@`")


def _quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def yaml_scalar(value: Any) -> str:
    """Skalár do frontmatteru — uvozovky jen tam, kde jsou opravdu potřeba."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if not text:
        return '""'
    unsafe = (
        text[0] in _UNSAFE_START
        or text != text.strip()
        or ": " in text
        or text.endswith(":")
        or " #" in text
        or "\n" in text
    )
    return _quote(text) if unsafe else text


def dump_frontmatter(fields: dict[str, Any]) -> str:
    """Mapa → YAML blok. Klíče s hodnotou ``None`` se vynechají.

    Radši chybějící klíč než ``null`` — frontmatter má říkat, co víme.
    """
    lines = ["---"]
    for key, value in fields.items():
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            inner = ", ".join(yaml_scalar(item) for item in value)
            lines.append(f"{key}: [{inner}]")
        else:
            lines.append(f"{key}: {yaml_scalar(value)}")
    lines.append("---")
    return "\n".join(lines)


def title_from_transcript(text: str, *, max_words: int = 8, max_chars: int = 70) -> str:
    """Nouzový titulek z prvních slov přepisu.

    Používá se, dokud (nebo když) nefunguje strukturování. Není to hezké,
    ale je to lepší než časová značka — podle titulku se rozhoduju, jestli
    poznámku vůbec otevřu.
    """
    if not text.strip():
        return FALLBACK_TITLE
    # Whisper interpunkci doplňuje, tak je první věta lepší hranice než
    # počet slov — jinak titulek končí uprostřed myšlenky ("...padá to na").
    sentence = re.split(r"(?<=[.!?…])\s", text.strip(), maxsplit=1)[0]
    if len(sentence.split()) < 3:
        sentence = text.strip()
    words = sentence.split()
    if not words:
        return FALLBACK_TITLE
    title = " ".join(words[:max_words])
    if len(title) > max_chars:
        title = title[:max_chars].rsplit(" ", 1)[0] or title[:max_chars]
    title = title.strip(" .,;:!?-–—…")
    if not title:
        return FALLBACK_TITLE
    return title[0].upper() + title[1:]


@dataclass
class NoteData:
    """Vše, co o nahrávce víme, ve tvaru nezávislém na fázi pipeline."""

    title: str
    created: datetime
    audio: str
    status: str = STATUS_INBOX
    duration_s: int | None = None
    speech_s: int | None = None
    transcript_model: str | None = None
    structure_model: str | None = None
    tags: list[str] = field(default_factory=list)
    summary: str | None = None
    tasks: list[str] = field(default_factory=list)
    transcript: str | None = None
    error: str | None = None

    def frontmatter(self) -> dict[str, Any]:
        return {
            "created": self.created.isoformat(timespec="seconds"),
            "source": "voice",
            "duration_s": self.duration_s,
            "speech_s": self.speech_s,
            "audio": self.audio,
            "transcript_model": self.transcript_model,
            "structure_model": self.structure_model,
            "tags": list(self.tags),
            "status": self.status,
        }


def render_note(note: NoteData) -> str:
    parts = [dump_frontmatter(note.frontmatter()), "", f"# {note.title}", ""]

    if note.summary:
        parts += [note.summary.strip(), ""]

    if note.tasks:
        parts.append("## Úkoly")
        parts.append("")
        parts += [f"- [ ] {task.strip()}" for task in note.tasks]
        parts.append("")

    if note.error:
        parts += ["## Chyba", "", "```", note.error.strip(), "```", ""]

    if note.transcript is not None:
        parts += ["## Přepis", "", note.transcript.strip(), ""]

    return "\n".join(parts).rstrip() + "\n"
