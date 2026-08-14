"""Zápis do Obsidian vaultu.

Dvě pravidla, obě kvůli Syncthingu:
  * pipeline **jen vytváří** nové soubory, nikdy needituje existující,
  * rozepsaný soubor se ve vaultu nesmí objevit — proto staging + `os.link`.
"""

from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path

from ..config import VaultConfig

log = logging.getLogger("voicenotes.worker.vault")

MAX_COLLISION_SUFFIX = 100


class VaultWriteError(RuntimeError):
    pass


def ensure_vault(vault: VaultConfig) -> None:
    # Kořen vaultu musí existovat. Kdyby se vyrobil, skončily by poznámky
    # v prázdné složce místo v nepřipojeném disku a nikdo by si nevšiml.
    if not vault.root.is_dir():
        raise VaultWriteError(
            f"vault neexistuje: {vault.root} (nepřipojený disk? překlep v configu?)"
        )
    vault.inbox_dir.mkdir(parents=True, exist_ok=True)
    vault.staging_dir.mkdir(parents=True, exist_ok=True)
    if vault.inbox_dir.stat().st_dev != vault.staging_dir.stat().st_dev:
        raise VaultWriteError(
            "staging adresář musí být na stejném svazku jako inbox, "
            "jinak nejde poznámku dokončit jedním atomickým krokem"
        )


def _candidates(inbox: Path, stem: str) -> list[Path]:
    names = [f"{stem}.md"] + [f"{stem}-{n}.md" for n in range(2, MAX_COLLISION_SUFFIX + 1)]
    return [inbox / name for name in names]


def write_note(vault: VaultConfig, stem: str, content: str) -> Path:
    """Vytvoří `<stem>.md` v inboxu. Když existuje, přidá suffix — nikdy nepřepíše.

    Obsah se nejdřív celý zapíše do staging adresáře a do vaultu se objeví
    až hotový, jedním `os.link`. Ten selže, pokud cíl existuje, takže mezi
    kontrolou a zápisem není okno, kterým by šlo přepsat cizí soubor.
    """
    ensure_vault(vault)
    staged = vault.staging_dir / f"{uuid.uuid4().hex}.md.part"
    try:
        with staged.open("w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())

        for target in _candidates(vault.inbox_dir, stem):
            try:
                os.link(staged, target)
            except FileExistsError:
                continue
            log.info("poznámka zapsána: %s", target)
            return target

        raise VaultWriteError(
            f"nelze vytvořit poznámku pro '{stem}' — "
            f"vyčerpáno {MAX_COLLISION_SUFFIX} variant názvu"
        )
    finally:
        staged.unlink(missing_ok=True)
