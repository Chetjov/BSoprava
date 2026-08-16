"""Opakování přepisu po přechodné chybě.

Obsazená GPU nebo zaseknutý dekodér nesmí vyrobit trvalou `needs-review`
poznámku — pipeline soubory ve vaultu needituje, takže by tam ta chyba
zůstala navždy.
"""

from __future__ import annotations

import hashlib

from conftest import FakeTranscriber, audio_bytes, tweak_worker

from voicenotes.worker.attempts import Attempts
from voicenotes.worker.ledger import Ledger
from voicenotes.worker.run import run
from voicenotes.worker.transcribe import TranscriptionFailed


def queue_recording(env, data: bytes, *, when: str = "2026-08-14T143211") -> str:
    env.queue_dir.mkdir(parents=True, exist_ok=True)
    name = f"{when}-{hashlib.sha256(data).hexdigest()[:6]}.m4a"
    (env.queue_dir / name).write_bytes(data)
    return name


# --- počítadlo pokusů -----------------------------------------------------


def test_attempts_counter_persists(tmp_path):
    store = Attempts(tmp_path / "attempts.json")

    assert store.count("a") == 0
    assert store.bump("a") == 1
    assert store.bump("a") == 2
    assert Attempts(tmp_path / "attempts.json").count("a") == 2

    store.clear("a")
    assert store.count("a") == 0


def test_attempts_survive_a_damaged_file(tmp_path, caplog):
    path = tmp_path / "attempts.json"
    path.write_text("{nedopsaný", encoding="utf-8")
    store = Attempts(path)

    with caplog.at_level("WARNING"):
        assert store.count("a") == 0

    assert store.bump("a") == 1
    assert "nečitelný" in caplog.text


def test_attempts_leaves_no_temp_file(tmp_path):
    store = Attempts(tmp_path / "attempts.json")

    store.bump("a")

    assert sorted(p.name for p in tmp_path.iterdir()) == ["attempts.json"]


# --- chování během běhu ---------------------------------------------------


def test_first_failure_leaves_the_recording_in_the_queue(env, caplog):
    """Žádná poznámka, nic v archivu — příští běh to zkusí znovu."""
    name = queue_recording(env, audio_bytes())
    fake = FakeTranscriber(TranscriptionFailed("CUDA out of memory"))

    with caplog.at_level("WARNING"):
        stats = run(env.worker, transcriber=fake)

    assert (stats.created, stats.retried, stats.failed) == (0, 1, 0)
    assert env.notes() == []
    assert (env.queue_dir / name).exists()
    assert not (env.archive_dir / name).exists()
    assert Ledger(env.worker.ledger_path).processed_ids() == set()
    assert "pokus 1/3" in caplog.text
    # Lokální kopie se maže, ať se příště stáhne čerstvá.
    assert list(env.worker.incoming_dir.iterdir()) == []


def test_note_appears_after_the_attempts_are_used_up(env):
    name = queue_recording(env, audio_bytes())
    fake = FakeTranscriber(TranscriptionFailed("CUDA out of memory"))

    first = run(env.worker, transcriber=fake)
    second = run(env.worker, transcriber=fake)
    third = run(env.worker, transcriber=fake)

    assert (first.retried, second.retried, third.retried) == (1, 1, 0)
    assert (first.created, second.created, third.created) == (0, 0, 1)

    text = env.notes()[0].read_text(encoding="utf-8")
    assert "status: needs-review" in text
    assert "CUDA out of memory" in text
    assert (env.archive_dir / name).exists()
    assert list(env.queue_dir.iterdir()) == []


def test_a_recovered_recording_gets_a_normal_note(env):
    """Když GPU mezitím zvládne přepis, vznikne poznámka bez chyby."""
    queue_recording(env, audio_bytes())

    run(env.worker, transcriber=FakeTranscriber(TranscriptionFailed("GPU zaneprázdněná")))
    stats = run(env.worker, transcriber=FakeTranscriber("Podařilo se to napodruhé."))

    assert (stats.created, stats.failed) == (1, 0)
    text = env.notes()[0].read_text(encoding="utf-8")
    assert "status: inbox" in text
    assert "## Chyba" not in text
    assert "Podařilo se to napodruhé." in text
    # Počítadlo se po úspěchu vynuluje.
    assert Attempts(env.worker.attempts_path).pending() == {}


def test_counter_is_cleared_after_the_final_note(env):
    queue_recording(env, audio_bytes())
    tweak_worker(env, transcribe={"enabled": False, "max_attempts": 1})
    fake = FakeTranscriber(TranscriptionFailed("navždy rozbité"))

    run(env.worker, transcriber=fake)

    assert Attempts(env.worker.attempts_path).pending() == {}


def test_retries_can_be_turned_off(env):
    """max_attempts: 1 = původní chování, poznámka hned napoprvé."""
    queue_recording(env, audio_bytes())
    tweak_worker(env, transcribe={"enabled": False, "max_attempts": 1})

    stats = run(
        env.worker, transcriber=FakeTranscriber(TranscriptionFailed("jednorázová chyba"))
    )

    assert (stats.created, stats.retried) == (1, 0)


def test_a_retried_recording_does_not_block_the_others(env):
    good = queue_recording(env, audio_bytes(b"dobra"), when="2026-08-14T100000")
    bad = queue_recording(env, audio_bytes(b"spatna"), when="2026-08-14T110000")
    fake = FakeTranscriber(
        "Tahle je v pořádku.", {bad: TranscriptionFailed("dekódování selhalo")}
    )

    stats = run(env.worker, transcriber=fake)

    assert (stats.created, stats.retried) == (1, 1)
    assert len(env.notes()) == 1
    assert (env.archive_dir / good).exists()
    assert (env.queue_dir / bad).exists()


def test_each_recording_counts_its_own_attempts(env):
    first = queue_recording(env, audio_bytes(b"jedna"), when="2026-08-14T100000")
    second = queue_recording(env, audio_bytes(b"dva"), when="2026-08-14T110000")
    fake = FakeTranscriber(TranscriptionFailed("chyba"))

    run(env.worker, transcriber=fake)
    run(env.worker, transcriber=fake)

    store = Attempts(env.worker.attempts_path)
    assert store.count(first.removesuffix(".m4a")) == 2
    assert store.count(second.removesuffix(".m4a")) == 2


def test_no_speech_is_not_retried(env):
    """VAD nic nenašel — to není přechodná chyba, opakování by nepomohlo."""
    from voicenotes.worker.transcribe import NoSpeechFound

    name = queue_recording(env, audio_bytes())

    stats = run(env.worker, transcriber=FakeTranscriber(NoSpeechFound("ticho")))

    assert (stats.rejected, stats.retried) == (1, 0)
    assert (env.archive_dir / "rejected" / name).exists()
    assert Attempts(env.worker.attempts_path).pending() == {}


def test_structuring_failure_is_not_retried(env):
    """Poznámka s přepisem je použitelná — opakování by stálo nový přepis."""
    from conftest import FakeStructurer
    from voicenotes.worker.structure import StructuringFailed

    name = queue_recording(env, audio_bytes())

    stats = run(
        env.worker,
        transcriber=FakeTranscriber("Nějaký přepis."),
        structurer=FakeStructurer(StructuringFailed("model se zbláznil")),
    )

    assert (stats.created, stats.retried) == (1, 0)
    assert (env.archive_dir / name).exists()
