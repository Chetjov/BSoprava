"""Sdílené primitivy pro pojmenování souborů ve frontě.

Gateway i worker musí na tvar ID vidět stejně — proto je to jeden modul
a ne zkopírovaný regex na dvou místech.

ID nahrávky:  ``2026-08-14T143211-a3f9c1``
              └── lokální čas přijetí ─┘└ prefix sha256 obsahu ┘
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime
from pathlib import Path

#: Kolik hex znaků ze sha256 jde do názvu souboru.
HASH_PREFIX_LEN = 6

#: Formát časové značky v názvu — bez dvojteček, aby název přežil i FAT/exFAT.
TS_FORMAT = "%Y-%m-%dT%H%M%S"

#: Volitelný ocásek na konci řeší nepravděpodobný případ, kdy dvě různé
#: nahrávky ve stejné sekundě trefí stejný prefix hashe. Bez něj by takový
#: soubor neprošel přes `parse_id` a zůstal by ve frontě navždy.
ID_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{6})-(?P<hash>[0-9a-f]{%d})(?:-[0-9a-f]{4})?$"
    % HASH_PREFIX_LEN
)

#: Přípony, které gateway přijme. Cokoli jiného se uloží jako .m4a.
ALLOWED_SUFFIXES = frozenset({".m4a", ".mp4", ".mp3", ".wav", ".caf", ".aac", ".ogg", ".flac"})

DEFAULT_SUFFIX = ".m4a"

_CHUNK = 1024 * 1024


def hash_prefix(digest_hex: str) -> str:
    return digest_hex[:HASH_PREFIX_LEN]


def make_id(when: datetime, digest_hex: str) -> str:
    """``2026-08-14T143211-a3f9c1``"""
    return f"{when.strftime(TS_FORMAT)}-{hash_prefix(digest_hex)}"


def parse_id(value: str) -> tuple[datetime, str] | None:
    """Rozloží ID (nebo název souboru) na naivní timestamp a hash prefix.

    Vrací ``None``, když název neodpovídá — takový soubor do fronty nepatří
    a worker ho nechá ležet, místo aby hádal.
    """
    stem = Path(value).stem if "." in Path(value).name else value
    match = ID_RE.match(stem)
    if match is None:
        return None
    return datetime.strptime(match["ts"], TS_FORMAT), match["hash"]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def safe_suffix(filename: str | None) -> str:
    """Přípona z názvu od klienta — jen z whitelistu, nikdy ne cesta.

    Název souboru od klienta je neověřený vstup; bereme z něj výhradně
    příponu, a to jen pokud je ve známé sadě.
    """
    if not filename:
        return DEFAULT_SUFFIX
    suffix = Path(filename.replace("\\", "/")).suffix.lower()
    return suffix if suffix in ALLOWED_SUFFIXES else DEFAULT_SUFFIX


def slugify(text: str, *, max_length: int = 60, fallback: str = "bez-nazvu") -> str:
    """Titulek → část názvu souboru. Diakritika pryč, ať se to dobře hledá."""
    normalized = unicodedata.normalize("NFKD", text)
    ascii_text = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    ascii_text = ascii_text.encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-")
    if len(slug) > max_length:
        slug = slug[:max_length].rsplit("-", 1)[0] or slug[:max_length]
    return slug.strip("-") or fallback
