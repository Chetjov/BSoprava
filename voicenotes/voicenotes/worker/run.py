"""Hlavní běh workeru — spouští ho systemd timer každých pár minut.

Fáze 1 (transport): stáhnout frontu, vyrobit poznámku, uklidit.
Přepis a strukturování přibydou v dalších fázích na místě označeném níž.

Prázdná fronta i nedostupné Pi končí tiše nulou; systemd timer nemá
o čem hlásit, když se nic nestalo.
"""

from __future__ import annotations

import argparse
import fcntl
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import IO
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..config import ConfigError, WorkerConfig, load_worker_config
from ..ids import TS_FORMAT, parse_id, sha256_file, slugify
from .audio import probe_duration_s
from .ledger import Ledger
from .notes import (
    STATUS_INBOX,
    STATUS_NEEDS_REVIEW,
    STATUS_REJECTED,
    NoteData,
    render_note,
    title_from_transcript,
)
from .remote import RemoteQueue, RemoteUnavailable, build_remote
from .transcribe import (
    NoSpeechFound,
    Transcriber,
    TranscriberUnavailable,
    TranscriptionFailed,
    Transcript,
    build_transcriber,
)
from .vault import VaultWriteError, ensure_vault, write_note

log = logging.getLogger("voicenotes.worker")

#: Nad tímhle rozdílem mezi názvem a mtime už jde nejspíš o špatnou časovou zónu.
CLOCK_SKEW_TOLERANCE_S = 5 * 60

PLACEHOLDER_TITLE = "Nezpracovaná nahrávka {hash}"

PLACEHOLDER_SUMMARY = (
    "Přepis je vypnutý (`worker.transcribe.enabled: false`). "
    "Audio je uložené na serveru, cesta je ve frontmatteru."
)

FAILED_SUMMARY = (
    "Přepis selhal, audio ale zůstalo na serveru — cesta je ve frontmatteru "
    "a přepis jde spustit znovu ručně."
)


@dataclass
class RunStats:
    fetched: int = 0
    created: int = 0
    skipped: int = 0
    rejected: int = 0
    failed: int = 0

    def as_line(self) -> str:
        return (
            f"staženo={self.fetched} poznámek={self.created} "
            f"přeskočeno={self.skipped} bez řeči={self.rejected} "
            f"chyb={self.failed}"
        )


def acquire_lock(path: Path) -> IO[str] | None:
    """Nepřekrývat běhy — timer tiká rychleji, než bude trvat přepis."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def resolve_timezone(name: str) -> timezone | ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("časová zóna '%s' není k dispozici, používám systémovou", name)
        return datetime.now().astimezone().tzinfo or timezone.utc


def warn_on_clock_skew(audio_path: Path, created: datetime) -> bool:
    """Ohlídá, že `worker.timezone` odpovídá zóně, ve které razítkuje Pi.

    Název souboru nese jen naivní čas — když má Pi jinou zónu než config,
    každé `created:` je tiše posunuté. Čas poslední změny souboru je oproti
    tomu absolutní, takže rozdíl mezi nimi ten překlep odhalí.
    """
    try:
        modified = datetime.fromtimestamp(audio_path.stat().st_mtime, tz=created.tzinfo)
    except OSError:  # pragma: no cover - soubor právě zmizel
        return False
    skew = abs((modified - created).total_seconds())
    if skew < CLOCK_SKEW_TOLERANCE_S:
        return False
    log.warning(
        "%s: časová značka v názvu se liší od času souboru o %.0f min — "
        "sedí worker.timezone (%s) na časovou zónu Pi?",
        audio_path.name,
        skew / 60,
        created.tzinfo,
    )
    return True


def build_note(
    config: WorkerConfig,
    *,
    audio_path: Path,
    digest_prefix: str,
    created: datetime,
    transcript: Transcript | None,
    error: str | None = None,
) -> NoteData:
    """Poznámka z toho, co o nahrávce víme.

    Fáze 3 sem doplní shrnutí, tagy a úkoly; titulek pak přijde od modelu
    místo z prvních slov přepisu.
    """
    note = NoteData(
        title=PLACEHOLDER_TITLE.format(hash=digest_prefix),
        created=created,
        audio=f"{config.remote.display}:{config.remote.archive_dir / audio_path.name}",
        status=STATUS_NEEDS_REVIEW,
        duration_s=probe_duration_s(audio_path, ffprobe_path=config.ffprobe_path),
    )

    if error is not None:
        # Přepis selhal: poznámka přesto vznikne, chyba je v těle.
        note.title = f"Nepřepsaná nahrávka {digest_prefix}"
        note.summary = FAILED_SUMMARY
        note.error = error
        return note

    if transcript is None:
        # Přepis je vypnutý (fáze 1 – ověření transportu).
        note.summary = PLACEHOLDER_SUMMARY
        return note

    note.title = title_from_transcript(transcript.text)
    note.status = STATUS_INBOX
    note.transcript = transcript.text
    note.transcript_model = transcript.model
    note.duration_s = transcript.duration_s or note.duration_s
    return note


def process_one(
    config: WorkerConfig,
    remote: RemoteQueue,
    ledger: Ledger,
    processed: set[str],
    audio_path: Path,
    tzinfo: timezone | ZoneInfo,
    stats: RunStats,
    transcriber: Transcriber | None,
) -> None:
    name = audio_path.name
    parsed = parse_id(name)
    if parsed is None:
        log.warning("neznámý tvar názvu, nechávám na Pi: %s", name)
        stats.failed += 1
        return

    stamp, digest_prefix = parsed
    identifier = audio_path.stem

    if identifier in processed:
        log.info("%s už zpracováno, přeskakuji", identifier)
        remote.archive(name)
        audio_path.unlink(missing_ok=True)
        stats.skipped += 1
        return

    if not sha256_file(audio_path).startswith(digest_prefix):
        # Poškozený nebo useknutý přenos. Originál na Pi zůstává, příště znovu.
        log.warning("hash nesedí na %s, stahuji znovu příští běh", name)
        audio_path.unlink(missing_ok=True)
        stats.failed += 1
        return

    created = stamp.replace(tzinfo=tzinfo)
    warn_on_clock_skew(audio_path, created)

    transcript: Transcript | None = None
    error: str | None = None
    if transcriber is not None:
        try:
            transcript = transcriber.transcribe(audio_path)
            log.info(
                "%s přepsáno (%d s, %d segmentů)",
                name,
                transcript.duration_s or 0,
                transcript.segments,
            )
        except NoSpeechFound as exc:
            # Falešné spuštění v kapse. Poznámka nevzniká, audio se schová
            # do rejected/ — ať se dá zpětně ověřit, že tam opravdu nic nebylo.
            log.info("%s: %s → archive/rejected/", name, exc)
            ledger.record(
                identifier, note=None, status=STATUS_REJECTED, when=datetime.now(tzinfo)
            )
            processed.add(identifier)
            remote.reject(name)
            audio_path.unlink(missing_ok=True)
            stats.rejected += 1
            return
        except TranscriptionFailed as exc:
            log.error("přepis %s selhal: %s", name, exc)
            error = str(exc)

    note = build_note(
        config,
        audio_path=audio_path,
        digest_prefix=digest_prefix,
        created=created,
        transcript=transcript,
        error=error,
    )
    stem = f"{stamp.strftime(TS_FORMAT)}-{slugify(note.title)}"

    try:
        written = write_note(config.vault, stem, render_note(note))
    except (VaultWriteError, OSError):
        log.exception("zápis poznámky pro %s selhal, audio zůstává ve frontě", name)
        stats.failed += 1
        return

    ledger.record(
        identifier,
        note=str(written.relative_to(config.vault.root)),
        status=note.status,
        when=datetime.now(tzinfo),
    )
    processed.add(identifier)
    remote.archive(name)
    audio_path.unlink(missing_ok=True)
    stats.created += 1
    if error is not None:
        stats.failed += 1


def run(
    config: WorkerConfig,
    *,
    remote: RemoteQueue | None = None,
    transcriber: Transcriber | None = None,
) -> RunStats:
    stats = RunStats()
    remote = remote or build_remote(
        config.remote, ssh_path=config.ssh_path, rsync_path=config.rsync_path
    )
    if transcriber is None and config.transcribe.enabled:
        # Model se načte až u první nahrávky; prázdná fronta na něj nesáhne.
        transcriber = build_transcriber(config.transcribe)

    config.work_dir.mkdir(parents=True, exist_ok=True)
    lock = acquire_lock(config.lock_path)
    if lock is None:
        log.info("běží jiná instance workeru, končím")
        return stats

    try:
        ensure_vault(config.vault)

        if not remote.is_available():
            log.info("Pi nedostupné, zkusím to příště")
            return stats

        try:
            files = remote.fetch_into(config.incoming_dir)
        except RemoteUnavailable as exc:
            log.info("stažení fronty selhalo (%s), zkusím to příště", exc)
            return stats

        stats.fetched = len(files)
        if not files:
            log.debug("fronta je prázdná")
            return stats

        ledger = Ledger(config.ledger_path)
        processed = ledger.processed_ids()
        tzinfo = resolve_timezone(config.timezone)
        for audio_path in files:
            try:
                process_one(
                    config,
                    remote,
                    ledger,
                    processed,
                    audio_path,
                    tzinfo,
                    stats,
                    transcriber,
                )
            except TranscriberUnavailable:
                # Systémová chyba: potkala by každou nahrávku. Radši zastavit
                # a nechat frontu být, než vysypat do vaultu samé chyby.
                raise
            except Exception:  # noqa: BLE001 - jedna vadná nahrávka nesmí zastavit frontu
                log.exception("nezachycená chyba u %s", audio_path.name)
                stats.failed += 1

        log.info("hotovo: %s", stats.as_line())
        return stats
    finally:
        # Fáze 3 si tady vyzvedne volnou VRAM pro strukturovací model.
        if transcriber is not None:
            transcriber.unload()
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="voicenotes worker")
    parser.add_argument("--config", default=None, help="cesta ke config.yaml")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    try:
        config = load_worker_config(args.config)
    except ConfigError as exc:
        print(f"chyba konfigurace: {exc}", file=sys.stderr)
        return 2

    try:
        run(config)
    except VaultWriteError as exc:
        print(f"vault není použitelný: {exc}", file=sys.stderr)
        return 1
    except TranscriberUnavailable as exc:
        # Fronta zůstala nedotčená, další běh to zkusí znovu.
        print(f"přepis není k dispozici: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
