"""Ruční doběhnutí jedné nahrávky z archivu."""

from __future__ import annotations

import pytest
from conftest import FakeStructurer, FakeTranscriber, audio_bytes, tweak_worker

from voicenotes import redo as redo_module
from voicenotes.redo import RedoFailed, apply_overrides, redo
from voicenotes.worker.ledger import Ledger
from voicenotes.worker.remote import build_remote
from voicenotes.worker.structure import Structure, StructuringFailed
from voicenotes.worker.transcribe import NoSpeechFound, TranscriptionFailed

IDENTIFIER = "2026-08-14T143211-a3f9c1"


def archive_recording(env, *, rejected: bool = False, identifier: str = IDENTIFIER) -> str:
    """Položí nahrávku do archivu na 'Pi', jako by ji tam nechal worker."""
    directory = env.archive_dir / "rejected" if rejected else env.archive_dir
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{identifier}.m4a").write_bytes(audio_bytes())
    return identifier


def remote_for(env):
    return build_remote(env.worker.remote)


# --- hledání nahrávky -----------------------------------------------------


def test_recording_is_found_in_the_archive(env):
    archive_recording(env)

    found = remote_for(env).find_archived(IDENTIFIER)

    assert found is not None and found.endswith(f"{IDENTIFIER}.m4a")


def test_rejected_recording_is_found_too(env):
    """VAD ji zahodil neprávem — musí jít vytáhnout zpátky."""
    archive_recording(env, rejected=True)

    found = remote_for(env).find_archived(IDENTIFIER)

    assert found is not None and "rejected" in found


def test_unknown_recording_is_reported(env):
    env.archive_dir.mkdir(parents=True, exist_ok=True)

    with pytest.raises(RedoFailed, match="není v archivu"):
        redo(env.worker, IDENTIFIER, remote=remote_for(env))


@pytest.mark.parametrize(
    "identifier",
    ["nesmysl", "../../etc/passwd", "2026-08-14T143211", "; rm -rf /", ""],
)
def test_invalid_identifier_is_refused_before_touching_the_pi(env, identifier):
    """ID jde do vzdáleného příkazu — musí projít regexem dřív."""
    with pytest.raises(RedoFailed, match="není platné ID"):
        redo(env.worker, identifier, remote=remote_for(env))


def test_ssh_lookup_searches_both_archive_dirs():
    from voicenotes.config import RemoteConfig

    remote = build_remote(
        RemoteConfig.from_dict({"kind": "ssh", "host": "pi", "root": "/srv/voicenotes"})
    )
    script = remote.find_archived_command(IDENTIFIER)[-1]

    # Hvězdička zůstává mimo uvozovky, ať ji rozbalí vzdálený shell.
    assert f"/srv/voicenotes/archive/{IDENTIFIER}.*" in script
    assert f"/srv/voicenotes/archive/rejected/{IDENTIFIER}.*" in script


def test_ssh_lookup_quotes_a_path_with_spaces():
    """Mezera v cestě nesmí rozpadnout vzdálený příkaz na dva argumenty."""
    from voicenotes.config import RemoteConfig

    remote = build_remote(
        RemoteConfig.from_dict({"kind": "ssh", "host": "pi", "root": "/srv/voice notes"})
    )
    script = remote.find_archived_command(IDENTIFIER)[-1]

    assert f"'/srv/voice notes/archive/{IDENTIFIER}'.*" in script


# --- nová poznámka --------------------------------------------------------


def test_redo_creates_a_new_note(env):
    archive_recording(env)
    tweak_worker(env, transcribe={"enabled": True}, structure={"enabled": True})

    written, _ = redo(
        env.worker,
        IDENTIFIER,
        remote=remote_for(env),
        transcriber=FakeTranscriber("Nový a lepší přepis."),
        structurer=FakeStructurer(),
    )

    assert written is not None
    text = written.read_text(encoding="utf-8")
    assert "# Přepracovat retry logiku v importu" in text
    assert "Nový a lepší přepis." in text
    assert "status: inbox" in text
    assert Ledger(env.worker.ledger_path).processed_ids() == {IDENTIFIER}


def test_redo_never_touches_the_original_note(env):
    """Pipeline poznámky needituje — původní zůstává vedle nové."""
    archive_recording(env)
    tweak_worker(env, transcribe={"enabled": True}, structure={"enabled": True})
    env.inbox.mkdir(parents=True, exist_ok=True)
    original = env.inbox / "2026-08-14T143211-prepracovat-retry-logiku-v-importu.md"
    original.write_text("původní poznámka\n", encoding="utf-8")

    written, _ = redo(
        env.worker,
        IDENTIFIER,
        remote=remote_for(env),
        transcriber=FakeTranscriber(),
        structurer=FakeStructurer(),
    )

    assert original.read_text(encoding="utf-8") == "původní poznámka\n"
    assert written != original
    assert written.name.endswith("-2.md")


def test_redo_keeps_the_audio_on_the_pi(env):
    archive_recording(env)
    tweak_worker(env, transcribe={"enabled": True})

    redo(env.worker, IDENTIFIER, remote=remote_for(env), transcriber=FakeTranscriber())

    assert (env.archive_dir / f"{IDENTIFIER}.m4a").exists()
    # Stažená kopie se po zpracování uklidí.
    assert not (env.work_dir / "redo" / f"{IDENTIFIER}.m4a").exists()


def test_dry_run_writes_nothing(env):
    archive_recording(env)
    tweak_worker(env, transcribe={"enabled": True}, structure={"enabled": True})

    written, content = redo(
        env.worker,
        IDENTIFIER,
        remote=remote_for(env),
        transcriber=FakeTranscriber("Zkušební přepis."),
        structurer=FakeStructurer(),
        dry_run=True,
    )

    assert written is None
    assert "Zkušební přepis." in content
    assert env.notes() == []
    assert Ledger(env.worker.ledger_path).processed_ids() == set()


# --- chybové stavy --------------------------------------------------------


def test_vad_rejection_suggests_no_vad(env):
    """Ruční doběhnutí nahrávku nezahazuje — uživatel o ni požádal výslovně."""
    archive_recording(env, rejected=True)
    tweak_worker(env, transcribe={"enabled": True})

    with pytest.raises(RedoFailed, match="--no-vad"):
        redo(
            env.worker,
            IDENTIFIER,
            remote=remote_for(env),
            transcriber=FakeTranscriber(NoSpeechFound("VAD nenašel řeč")),
        )

    assert env.notes() == []


def test_failed_transcription_still_produces_a_note(env):
    archive_recording(env)
    tweak_worker(env, transcribe={"enabled": True})

    written, _ = redo(
        env.worker,
        IDENTIFIER,
        remote=remote_for(env),
        transcriber=FakeTranscriber(TranscriptionFailed("pořád to padá")),
    )

    assert written is not None
    text = written.read_text(encoding="utf-8")
    assert "status: needs-review" in text
    assert "pořád to padá" in text


def test_failed_structuring_keeps_the_transcript(env):
    archive_recording(env)
    tweak_worker(env, transcribe={"enabled": True}, structure={"enabled": True})

    written, _ = redo(
        env.worker,
        IDENTIFIER,
        remote=remote_for(env),
        transcriber=FakeTranscriber("Surový přepis zůstává."),
        structurer=FakeStructurer(StructuringFailed("model se zbláznil")),
    )

    text = written.read_text(encoding="utf-8")
    assert "Surový přepis zůstává." in text
    assert "model se zbláznil" in text
    assert "status: needs-review" in text


# --- přepínače -------------------------------------------------------------


def test_overrides_apply_only_to_this_run(env):
    config = env.worker
    changed = apply_overrides(config, model="large-v3-turbo", backend="anthropic", vad=False)

    assert changed.transcribe.model == "large-v3-turbo"
    assert changed.transcribe.vad is False
    assert changed.structure.backend == "anthropic"
    assert changed.structure.enabled is True
    # Původní config zůstal netknutý.
    assert config.transcribe.vad is True
    assert config.structure.backend == "ollama"


def test_backend_none_skips_structuring(env):
    changed = apply_overrides(env.worker, model=None, backend="none", vad=None)

    assert changed.structure.enabled is False


def test_unknown_backend_is_refused(env):
    with pytest.raises(RedoFailed, match="--backend"):
        apply_overrides(env.worker, model=None, backend="gpt", vad=None)


def test_no_vad_lets_a_rejected_recording_through(env):
    """Přepínač musí opravdu dorazit do configu přepisu."""
    archive_recording(env, rejected=True)
    tweak_worker(env, transcribe={"enabled": True})
    config = apply_overrides(env.worker, model=None, backend="none", vad=False)

    assert config.transcribe.vad is False

    written, _ = redo(
        config,
        IDENTIFIER,
        remote=remote_for(env),
        transcriber=FakeTranscriber("Mluvil jsem potichu, ale mluvil."),
    )

    assert "Mluvil jsem potichu, ale mluvil." in written.read_text(encoding="utf-8")


# --- CLI -------------------------------------------------------------------


def test_cli_reports_the_new_note(env, monkeypatch, capsys):
    archive_recording(env)
    tweak_worker(env, transcribe={"enabled": True}, structure={"enabled": True})
    monkeypatch.setattr(redo_module, "build_transcriber", lambda c: FakeTranscriber())
    monkeypatch.setattr(redo_module, "build_structurer", lambda c: FakeStructurer())

    assert redo_module.main([IDENTIFIER, "--config", str(env.config_path)]) == 0
    assert "nová poznámka:" in capsys.readouterr().out
    assert len(env.notes()) == 1


def test_cli_dry_run_prints_the_note(env, monkeypatch, capsys):
    archive_recording(env)
    tweak_worker(env, transcribe={"enabled": True}, structure={"enabled": True})
    monkeypatch.setattr(
        redo_module, "build_transcriber", lambda c: FakeTranscriber("Náhled přepisu.")
    )
    monkeypatch.setattr(
        redo_module,
        "build_structurer",
        lambda c: FakeStructurer(
            Structure(title="Náhled", summary="s", tags=["napad"], model="qwen3:8b")
        ),
    )

    assert (
        redo_module.main([IDENTIFIER, "--config", str(env.config_path), "--dry-run"]) == 0
    )
    out = capsys.readouterr()
    assert "# Náhled" in out.out
    assert "Náhled přepisu." in out.out
    assert env.notes() == []


def test_cli_reports_a_missing_recording(env, capsys):
    env.archive_dir.mkdir(parents=True, exist_ok=True)

    assert redo_module.main([IDENTIFIER, "--config", str(env.config_path)]) == 1
    assert "není v archivu" in capsys.readouterr().err
