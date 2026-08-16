"""Ruční doběhnutí jedné nahrávky z archivu.

Na co to je:
  * poznámka skončila se `status: needs-review` a chceš ji zkusit znovu,
  * VAD nahrávku zahodil, i když jsi mluvil (`--no-vad`),
  * chceš vidět, co by z téže nahrávky udělal jiný model
    (`--model large-v3`, `--backend anthropic`).

Vzniká **nová poznámka**, původní zůstává. Pipeline soubory ve vaultu
needituje ani nemaže — kdyby to dělala, Syncthing by z toho vyrobil konflikt.
Obě poznámky spojuje stejná cesta v `audio:`.

    voicenotes-redo 2026-08-14T143211-a3f9c1
    voicenotes-redo 2026-08-14T143211-a3f9c1 --no-vad --model large-v3
    voicenotes-redo 2026-08-14T143211-a3f9c1 --dry-run
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import shutil
import sys
from datetime import datetime
from pathlib import Path

from .config import ConfigError, WorkerConfig, load_worker_config
from .ids import TS_FORMAT, parse_id, slugify
from .worker.ledger import Ledger
from .worker.notes import render_note
from .worker.remote import RemoteQueue, RemoteUnavailable, build_remote
from .worker.run import build_note, resolve_timezone
from .worker.structure import Structure, Structurer, StructuringFailed, build_structurer
from .worker.transcribe import (
    NoSpeechFound,
    Transcriber,
    TranscriberUnavailable,
    Transcript,
    TranscriptionFailed,
    build_transcriber,
)
from .worker.vault import VaultWriteError, write_note

log = logging.getLogger("voicenotes.redo")


class RedoFailed(RuntimeError):
    """Doběhnutí nedopadlo. Na rozdíl od workeru to hlásíme rovnou uživateli."""


def fetch_archived(config: WorkerConfig, identifier: str, remote: RemoteQueue) -> Path:
    """Stáhne nahrávku z `archive/` (nebo `archive/rejected/`) na notebook."""
    if parse_id(identifier) is None:
        raise RedoFailed(
            f"'{identifier}' není platné ID nahrávky "
            "(očekávám např. 2026-08-14T143211-a3f9c1)"
        )
    if not remote.is_available():
        raise RedoFailed("Pi je nedostupné")

    remote_path = remote.find_archived(identifier)
    if remote_path is None:
        raise RedoFailed(f"nahrávka {identifier} není v archivu na Pi")

    destination = config.work_dir / "redo"
    shutil.rmtree(destination, ignore_errors=True)
    try:
        local = remote.fetch_one(remote_path, destination)
    except RemoteUnavailable as exc:
        raise RedoFailed(f"stažení selhalo: {exc}") from exc
    log.info("staženo %s", remote_path)
    return local


def apply_overrides(
    config: WorkerConfig,
    *,
    model: str | None,
    backend: str | None,
    vad: bool | None,
) -> WorkerConfig:
    """Přepíše config volbami z příkazové řádky, jen pro tenhle jeden běh."""
    transcribe = config.transcribe
    if model:
        transcribe = dataclasses.replace(transcribe, model=model)
    if vad is not None:
        transcribe = dataclasses.replace(transcribe, vad=vad)

    structure = config.structure
    if backend:
        if backend not in {"ollama", "anthropic", "none"}:
            raise RedoFailed("--backend musí být 'ollama', 'anthropic' nebo 'none'")
        structure = (
            dataclasses.replace(structure, enabled=False)
            if backend == "none"
            else dataclasses.replace(structure, backend=backend, enabled=True)
        )
    return dataclasses.replace(config, transcribe=transcribe, structure=structure)


def reprocess(
    config: WorkerConfig,
    audio_path: Path,
    *,
    transcriber: Transcriber | None = None,
    structurer: Structurer | None = None,
) -> tuple[Transcript | None, Structure | None, str | None]:
    """Přepis a strukturování jedné nahrávky. Chyby vrací, nevyhazuje."""
    transcript: Transcript | None = None
    structure: Structure | None = None
    error: str | None = None

    if config.transcribe.enabled:
        transcriber = transcriber or build_transcriber(config.transcribe)
        try:
            transcript = transcriber.transcribe(audio_path)
            log.info("přepsáno: %d znaků", len(transcript.text))
        except NoSpeechFound as exc:
            # Při ručním doběhnutí je to informace, ne důvod nahrávku zahodit —
            # uživatel o ni požádal výslovně.
            raise RedoFailed(
                f"{exc} — zkus to znovu s --no-vad, pokud víš, že tam řeč je"
            ) from exc
        except TranscriptionFailed as exc:
            log.error("přepis selhal: %s", exc)
            error = str(exc)
        finally:
            transcriber.unload()

    if transcript is not None and config.structure.enabled:
        structurer = structurer or build_structurer(config.structure)
        try:
            structure = structurer.structure(transcript.text)
            log.info("strukturováno: %s", structure.title)
        except StructuringFailed as exc:
            log.error("strukturování selhalo: %s", exc)
            error = str(exc)
        finally:
            structurer.unload()

    return transcript, structure, error


def redo(
    config: WorkerConfig,
    identifier: str,
    *,
    remote: RemoteQueue | None = None,
    transcriber: Transcriber | None = None,
    structurer: Structurer | None = None,
    dry_run: bool = False,
) -> tuple[Path | None, str]:
    """Vrací cestu k nové poznámce (nebo `None` u `--dry-run`) a její obsah."""
    remote = remote or build_remote(
        config.remote, ssh_path=config.ssh_path, rsync_path=config.rsync_path
    )
    audio_path = fetch_archived(config, identifier, remote)

    parsed = parse_id(identifier)
    assert parsed is not None  # ověřeno ve fetch_archived
    stamp, digest_prefix = parsed
    tzinfo = resolve_timezone(config.timezone)

    transcript, structure, error = reprocess(
        config, audio_path, transcriber=transcriber, structurer=structurer
    )

    note = build_note(
        config,
        audio_path=audio_path,
        digest_prefix=digest_prefix,
        created=stamp.replace(tzinfo=tzinfo),
        transcript=transcript,
        structure=structure,
        error=error,
    )
    content = render_note(note)
    if dry_run:
        return None, content

    stem = f"{stamp.strftime(TS_FORMAT)}-{slugify(note.title)}"
    written = write_note(config.vault, stem, content)
    Ledger(config.ledger_path).record(
        identifier,
        note=str(written.relative_to(config.vault.root)),
        status=note.status,
        when=datetime.now(tzinfo),
    )
    audio_path.unlink(missing_ok=True)
    return written, content


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="ruční doběhnutí jedné nahrávky z archivu"
    )
    parser.add_argument("identifier", help="ID nahrávky, např. 2026-08-14T143211-a3f9c1")
    parser.add_argument("--config", default=None, help="cesta ke config.yaml")
    parser.add_argument("--model", default=None, help="jiný whisper model pro tenhle běh")
    parser.add_argument(
        "--backend", default=None, help="strukturování: ollama | anthropic | none"
    )
    parser.add_argument(
        "--no-vad",
        action="store_true",
        help="vypnout VAD — na nahrávky, které VAD zahodil neprávem",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="jen vypsat poznámku, nezapisovat"
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    try:
        config = apply_overrides(
            load_worker_config(args.config),
            model=args.model,
            backend=args.backend,
            vad=False if args.no_vad else None,
        )
        written, content = redo(config, args.identifier, dry_run=args.dry_run)
    except ConfigError as exc:
        print(f"chyba konfigurace: {exc}", file=sys.stderr)
        return 2
    except (RedoFailed, VaultWriteError, TranscriberUnavailable) as exc:
        print(f"chyba: {exc}", file=sys.stderr)
        return 1

    if written is None:
        print(content)
        print("--- --dry-run: nic se nezapsalo ---", file=sys.stderr)
    else:
        print(f"nová poznámka: {written}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
