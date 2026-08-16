"""Počítadlo pokusů o přepis, aby přechodná chyba nezpůsobila trvalou škodu.

Když přepis selže kvůli něčemu, co za chvíli přejde (obsazená GPU, zaseknutý
dekodér), nemá smysl hned vyrábět poznámku se `status: needs-review` —
nahrávka zůstane ve frontě na Pi a příští běh to zkusí znovu. Po vyčerpání
pokusů poznámka vznikne, aby se nahrávka neztratila v nekonečné smyčce.

Zvlášť od evidence zpracovaného (`ledger.py`): tam patří jen hotové věci,
tohle je stav rozdělané práce.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("voicenotes.worker.attempts")


@dataclass(frozen=True)
class Attempts:
    path: Path

    def _load(self) -> dict[str, int]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.warning("%s je nečitelný, začínám od nuly", self.path)
            return {}
        if not isinstance(data, dict):
            return {}
        return {key: value for key, value in data.items() if isinstance(value, int)}

    def _save(self, data: dict[str, int]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Přes dočasný soubor: rozepsaný stav by se při pádu četl jako prázdný.
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)

    def pending(self) -> dict[str, int]:
        """Nahrávky, které čekají na další pokus. Prázdné = nic nevisí."""
        return self._load()

    def count(self, identifier: str) -> int:
        return self._load().get(identifier, 0)

    def bump(self, identifier: str) -> int:
        """Zvýší počet pokusů a vrátí nový součet."""
        data = self._load()
        data[identifier] = data.get(identifier, 0) + 1
        self._save(data)
        return data[identifier]

    def clear(self, identifier: str) -> None:
        data = self._load()
        if data.pop(identifier, None) is not None:
            self._save(data)
