"""Worker: cesta fronta → vault a co se stane, když něco selže."""

from __future__ import annotations

import hashlib
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from conftest import audio_bytes, upload

from voicenotes.ids import make_id
from voicenotes.worker import run as worker_run
from voicenotes.worker.ledger import Ledger
from voicenotes.worker.remote import LocalRemoteQueue, RemoteUnavailable, build_remote
from voicenotes.worker.run import acquire_lock, run


def queue_recording(env, data: bytes, *, when: str = "2026-08-14T143211") -> str:
    """Položí nahrávku rovnou do fronty na 'Pi', se správným názvem."""
    env.queue_dir.mkdir(parents=True, exist_ok=True)
    name = f"{when}-{hashlib.sha256(data).hexdigest()[:6]}.m4a"
    (env.queue_dir / name).write_bytes(data)
    return name


# --- happy path ----------------------------------------------------------


def test_recording_becomes_a_note(env, client):
    """Telefon → gateway → worker → vault, celá cesta v jednom testu."""
    response = upload(client, audio_bytes(b"myslenka"))
    name = response.json()["id"]

    stats = run(env.worker)

    assert (stats.fetched, stats.created, stats.failed) == (1, 1, 0)
    notes = env.notes()
    assert len(notes) == 1

    text = notes[0].read_text(encoding="utf-8")
    assert text.startswith("---\n")
    assert "source: voice" in text
    assert "status: needs-review" in text
    assert f"audio: pi:{env.archive_dir / name}" in text
    assert "created: 2" in text
    assert text.count("---\n") == 2  # frontmatter má právě dva oddělovače

    # Originál zůstal na Pi, jen se přesunul do archivu.
    assert (env.archive_dir / name).read_bytes() == (audio_bytes(b"myslenka"))
    assert list(env.queue_dir.iterdir()) == []
    # Lokální kopie se po zpracování uklidí — audio do notebooku nepatří.
    assert list(env.worker.incoming_dir.iterdir()) == []
    assert Ledger(env.worker.ledger_path).processed_ids() == {Path(name).stem}


def test_note_filename_carries_timestamp_and_slug(env):
    queue_recording(env, audio_bytes(), when="2026-08-14T143211")

    run(env.worker)

    (note,) = env.notes()
    assert note.name.startswith("2026-08-14T143211-")
    assert note.name.endswith(".md")
    assert "nezpracovana-nahravka" in note.name


def test_empty_queue_does_nothing(env):
    env.queue_dir.mkdir(parents=True, exist_ok=True)

    stats = run(env.worker)

    assert (stats.fetched, stats.created) == (0, 0)
    assert env.notes() == []


# --- idempotence ---------------------------------------------------------


def test_second_run_skips_processed_recording(env):
    queue_recording(env, audio_bytes(b"jednou"))

    first = run(env.worker)
    second = run(env.worker)

    assert first.created == 1
    assert second.created == 0
    assert len(env.notes()) == 1


def test_recording_back_in_queue_is_skipped(env):
    """I když se soubor do fronty vrátí, druhá poznámka nevznikne."""
    name = queue_recording(env, audio_bytes(b"vraceno"))
    run(env.worker)
    (env.queue_dir / name).write_bytes(audio_bytes(b"vraceno"))

    stats = run(env.worker)

    assert stats.skipped == 1
    assert len(env.notes()) == 1
    assert (env.archive_dir / name).exists()
    assert list(env.queue_dir.iterdir()) == []


# --- kolize názvů --------------------------------------------------------


def test_existing_note_is_never_overwritten(env):
    queue_recording(env, audio_bytes(b"kolize"), when="2026-08-14T143211")
    env.inbox.mkdir(parents=True, exist_ok=True)
    squatter = env.inbox / "2026-08-14T143211-nezpracovana-nahravka-{}.md".format(
        hashlib.sha256(audio_bytes(b"kolize")).hexdigest()[:6]
    )
    squatter.write_text("ruční poznámka, nesahat\n", encoding="utf-8")

    run(env.worker)

    assert squatter.read_text(encoding="utf-8") == "ruční poznámka, nesahat\n"
    notes = env.notes()
    assert len(notes) == 2
    assert any(note.name.endswith("-2.md") for note in notes)


def test_vault_staging_is_left_empty(env):
    queue_recording(env, audio_bytes())

    run(env.worker)

    assert list(env.worker.vault.staging_dir.iterdir()) == []


# --- chybové stavy -------------------------------------------------------


def test_unreachable_pi_is_quiet(env):
    """Fronta neexistuje = Pi je vypnuté. Žádná výjimka, žádná poznámka."""
    stats = run(env.worker)

    assert (stats.fetched, stats.created, stats.failed) == (0, 0, 0)
    assert env.notes() == []


def test_fetch_failure_is_quiet(env, monkeypatch):
    env.queue_dir.mkdir(parents=True, exist_ok=True)

    def boom(self, destination):
        raise RemoteUnavailable("ssh: connection closed")

    monkeypatch.setattr(LocalRemoteQueue, "fetch_into", boom)

    stats = run(env.worker)

    assert (stats.created, stats.failed) == (0, 0)
    assert env.notes() == []


def test_truncated_transfer_does_not_create_a_note(env):
    """Obsah nesedí na hash v názvu → originál zůstává ve frontě na Pi."""
    env.queue_dir.mkdir(parents=True, exist_ok=True)
    name = f"{make_id(datetime(2026, 8, 14, 14, 32, 11), 'abcdef0')}.m4a"
    (env.queue_dir / name).write_bytes(b"useknuty prenos")

    stats = run(env.worker)

    assert (stats.created, stats.failed) == (0, 1)
    assert env.notes() == []
    assert (env.queue_dir / name).exists()
    assert not (env.archive_dir / name).exists()


def test_unknown_filename_is_left_alone(env):
    env.queue_dir.mkdir(parents=True, exist_ok=True)
    (env.queue_dir / "rucne-nakopirovano.m4a").write_bytes(audio_bytes())

    stats = run(env.worker)

    assert (stats.created, stats.failed) == (0, 1)
    assert env.notes() == []
    assert (env.queue_dir / "rucne-nakopirovano.m4a").exists()


def test_failed_note_write_keeps_audio_in_queue(env, monkeypatch):
    """Když zápis poznámky selže, nahrávka se neztratí — zůstane ve frontě."""
    name = queue_recording(env, audio_bytes())

    def boom(*args, **kwargs):
        raise OSError("disk je plný")

    monkeypatch.setattr(worker_run, "write_note", boom)

    stats = run(env.worker)

    assert (stats.created, stats.failed) == (0, 1)
    assert (env.queue_dir / name).exists()
    assert Ledger(env.worker.ledger_path).processed_ids() == set()


def test_one_broken_recording_does_not_block_the_rest(env):
    queue_recording(env, audio_bytes(b"prvni"), when="2026-08-14T100000")
    (env.queue_dir / "nesmysl.m4a").write_bytes(b"x")
    queue_recording(env, audio_bytes(b"druha"), when="2026-08-14T110000")

    stats = run(env.worker)

    assert stats.created == 2
    assert stats.failed == 1
    assert len(env.notes()) == 2


def test_overlapping_runs_are_skipped(env):
    queue_recording(env, audio_bytes())
    env.worker.work_dir.mkdir(parents=True, exist_ok=True)
    held = acquire_lock(env.worker.lock_path)
    assert held is not None

    try:
        stats = run(env.worker)
    finally:
        held.close()

    assert (stats.fetched, stats.created) == (0, 0)
    assert env.notes() == []


def test_timezone_mismatch_is_reported(env, caplog):
    """Špatná zóna na Pi by jinak tiše posunula každé `created:`."""
    name = queue_recording(env, audio_bytes(), when="2026-08-14T143211")
    # Pi běží v UTC, ale config tvrdí Prahu → `created:` by bylo o dvě hodiny vedle.
    real = datetime(2026, 8, 14, 14, 32, 11, tzinfo=timezone.utc).timestamp()
    os.utime(env.queue_dir / name, (real, real))

    with caplog.at_level("WARNING"):
        stats = run(env.worker)

    assert stats.created == 1  # poznámka přesto vznikne
    assert "worker.timezone" in caplog.text


def test_matching_timezone_is_quiet(env, caplog):
    """Pi razítkuje v pražském čase — název i mtime ukazují na týž okamžik."""
    name = queue_recording(env, audio_bytes(), when="2026-08-14T143211")
    real = datetime(2026, 8, 14, 14, 32, 11, tzinfo=timezone(timedelta(hours=2)))
    os.utime(env.queue_dir / name, (real.timestamp(), real.timestamp()))

    with caplog.at_level("WARNING"):
        run(env.worker)

    assert "worker.timezone" not in caplog.text


def test_missing_vault_fails_loudly(env, capsys):
    """Nepřipojený disk se nesmí spravit tím, že se cesta prostě vyrobí."""
    queue_recording(env, audio_bytes())
    shutil.rmtree(env.vault_root)

    assert worker_run.main(["--config", str(env.config_path)]) == 1
    assert "vault neexistuje" in capsys.readouterr().err
    assert not env.vault_root.exists()


def test_ledger_survives_a_damaged_line(env):
    ledger = Ledger(env.worker.ledger_path)
    ledger.path.parent.mkdir(parents=True, exist_ok=True)
    ledger.path.write_text('{"id": "a"}\nnedopsany zaznam\n', encoding="utf-8")

    assert ledger.processed_ids() == {"a"}


# --- rsync / ssh ---------------------------------------------------------


def test_ssh_remote_builds_expected_commands(env):
    from voicenotes.config import RemoteConfig

    config = RemoteConfig.from_dict(
        {
            "kind": "ssh",
            "host": "pi-voicenotes",
            "user": "voicenotes",
            "root": "/srv/voicenotes",
            "identity_file": "/home/me/.ssh/id_ed25519",
        }
    )
    remote = build_remote(config)

    rsync = remote.rsync_command(Path("/tmp/incoming"))
    assert rsync[0] == "rsync"
    assert "voicenotes@pi-voicenotes:/srv/voicenotes/queue/" in rsync
    assert "/tmp/incoming/" in rsync
    assert "BatchMode=yes" in rsync[rsync.index("-e") + 1]

    archive = remote.archive_command("2026-08-14T143211-a3f9c1.m4a")
    script = archive[-1]
    assert "/srv/voicenotes/queue/2026-08-14T143211-a3f9c1.m4a" in script
    assert "/srv/voicenotes/archive/2026-08-14T143211-a3f9c1.m4a" in script
    # Archivace nesmí mazat a musí projít i podruhé.
    assert "rm " not in script
    assert script.startswith("mkdir -p")


def test_ssh_remote_reports_missing_tools_as_unavailable(env):
    from voicenotes.config import RemoteConfig

    remote = build_remote(
        RemoteConfig.from_dict({"kind": "ssh", "host": "pi", "root": "/srv/voicenotes"}),
        ssh_path="ssh-neexistuje",
        rsync_path="rsync-neexistuje",
    )

    assert remote.is_available() is False
    with pytest.raises(RemoteUnavailable):
        remote.fetch_into(env.work_dir / "incoming")
