"""Evidence zpracovaných nahrávek — append-only JSONL, žádná databáze.

Zapisuje se až *po* vzniku poznámky. Když worker spadne mezi zápisem
poznámky a zápisem záznamu, další běh vyrobí poznámku s duplicitním
názvem — což je vidět. Opačné pořadí by nahrávku tiše ztratilo.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

log = logging.getLogger("voicenotes.worker.ledger")


@dataclass(frozen=True)
class Ledger:
    path: Path

    def processed_ids(self) -> set[str]:
        if not self.path.exists():
            return set()
        found: set[str] = set()
        with self.path.open("r", encoding="utf-8") as handle:
            for lineno, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    log.warning("%s:%d: nečitelný záznam, přeskakuji", self.path, lineno)
                    continue
                identifier = entry.get("id")
                if isinstance(identifier, str):
                    found.add(identifier)
        return found

    def record(self, identifier: str, *, note: str, status: str, when: datetime) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "id": identifier,
            "note": note,
            "status": status,
            "at": when.isoformat(timespec="seconds"),
        }
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
