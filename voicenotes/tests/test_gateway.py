"""Gateway: hlavně chybové stavy. Happy path stačí jeden."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
from pathlib import Path

import pytest
from conftest import MAX_UPLOAD_BYTES, TOKEN, audio_bytes, upload

from voicenotes import gateway
from voicenotes.config import ConfigError
from voicenotes.ids import ID_RE, sha256_file

NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{6}-[0-9a-f]{6}\.m4a$")


def test_upload_lands_in_queue(client, env):
    data = audio_bytes()
    response = upload(client, data)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "queued"
    assert NAME_RE.match(body["id"]), body["id"]

    queued = list(env.queue_dir.iterdir())
    assert [p.name for p in queued] == [body["id"]]
    assert queued[0].read_bytes() == data
    # Název nese prefix sha256 obsahu.
    assert sha256_file(queued[0]).startswith(ID_RE.match(queued[0].stem)["hash"])
    # Po dokončení uploadu nesmí v .tmp/ nic zůstat.
    assert list(env.gateway.tmp_dir.iterdir()) == []


def test_health_counts_queue(client, env):
    assert client.get("/health").json() == {"status": "ok", "queued": 0}
    upload(client, audio_bytes(b"a"))
    upload(client, audio_bytes(b"b"))
    assert client.get("/health").json() == {"status": "ok", "queued": 2}


# --- token ---------------------------------------------------------------


@pytest.mark.parametrize("token", ["", "spatny", TOKEN + "x", TOKEN[:-1]])
def test_wrong_token_is_401_and_writes_nothing(client, env, token):
    response = upload(client, audio_bytes(), token=token)

    assert response.status_code == 401
    assert list(env.queue_dir.iterdir()) == []
    assert list(env.gateway.tmp_dir.iterdir()) == []


def test_missing_token_header_is_401(client, env):
    response = client.post("/ingest", files={"audio": ("rec.m4a", audio_bytes())})

    assert response.status_code == 401
    assert list(env.queue_dir.iterdir()) == []


def test_gateway_refuses_to_start_without_token(env, monkeypatch):
    monkeypatch.delenv("VOICENOTES_TOKEN")
    with pytest.raises(ConfigError, match="VOICENOTES_TOKEN"):
        gateway.create_app(env.gateway)


# --- limit velikosti -----------------------------------------------------


def test_oversize_declared_length_is_413(client, env):
    """Content-Length nad limit → 413 dřív, než se sáhne na tělo."""
    response = upload(client, audio_bytes(size=MAX_UPLOAD_BYTES * 2))

    assert response.status_code == 413
    assert list(env.queue_dir.iterdir()) == []
    assert list(env.gateway.tmp_dir.iterdir()) == []


def test_oversize_without_content_length_is_413(client, env):
    """Chunked upload projde parserem — limit musí platit i tam."""
    boundary = "----voicenotestest"
    payload = audio_bytes(size=MAX_UPLOAD_BYTES + 5000)
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="audio"; filename="rec.m4a"\r\n'
        "Content-Type: audio/m4a\r\n\r\n"
    ).encode() + payload + f"\r\n--{boundary}--\r\n".encode()

    def chunks():
        for start in range(0, len(body), 4096):
            yield body[start : start + 4096]

    response = client.post(
        "/ingest",
        headers={
            "X-Auth-Token": TOKEN,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        content=chunks(),
    )

    assert response.status_code == 413
    assert list(env.queue_dir.iterdir()) == []
    assert list(env.gateway.tmp_dir.iterdir()) == []


def test_upload_exactly_at_limit_passes(client, env):
    response = upload(client, audio_bytes(size=MAX_UPLOAD_BYTES))

    assert response.status_code == 200
    assert len(list(env.queue_dir.iterdir())) == 1


# --- vadný vstup ---------------------------------------------------------


def test_empty_file_is_400(client, env):
    response = upload(client, b"")

    assert response.status_code == 400
    assert list(env.queue_dir.iterdir()) == []
    assert list(env.gateway.tmp_dir.iterdir()) == []


def test_missing_audio_field_is_400(client, env):
    response = client.post(
        "/ingest", headers={"X-Auth-Token": TOKEN}, data={"neco": "jineho"}
    )

    assert response.status_code == 400
    assert list(env.queue_dir.iterdir()) == []


def test_client_filename_cannot_choose_the_path(client, env):
    """Z názvu od klienta se bere jen přípona, a jen ze známé sady."""
    response = upload(client, audio_bytes(), filename="../../../etc/passwd.evil")

    assert response.status_code == 200
    assert response.json()["id"].endswith(".m4a")
    assert [p.name for p in env.queue_dir.iterdir()] == [response.json()["id"]]


# --- atomický zápis ------------------------------------------------------


def test_file_enters_queue_only_complete(client, env, monkeypatch):
    """Do queue/ se soubor dostane přesunem, až je celý na disku."""
    data = audio_bytes(size=4096)
    real_rename = os.rename
    observed: list[tuple[int, str, str]] = []

    def watching_rename(src, dst):
        observed.append((os.path.getsize(src), str(src), str(dst)))
        return real_rename(src, dst)

    monkeypatch.setattr(os, "rename", watching_rename)
    response = upload(client, data)

    assert response.status_code == 200
    assert len(observed) == 1
    size_at_rename, src, dst = observed[0]
    # Zdroj byl kompletní a ležel v .tmp/, cíl je až queue/.
    assert size_at_rename == len(data)
    assert str(env.gateway.tmp_dir) in src
    assert str(env.queue_dir) in dst


def test_failed_upload_leaves_no_trace(client, env, monkeypatch):
    """Když přesun selže, nezůstane půlka souboru ani v .tmp/, ani ve frontě."""

    def boom(src, dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "rename", boom)
    response = upload(client, audio_bytes())

    assert response.status_code == 507
    assert list(env.queue_dir.iterdir()) == []
    assert list(env.gateway.tmp_dir.iterdir()) == []


def test_stale_partial_uploads_are_cleaned(env):
    config = env.gateway
    gateway.ensure_layout(config)
    fresh = config.tmp_dir / "fresh.part"
    stale = config.tmp_dir / "stale.part"
    fresh.write_bytes(b"x")
    stale.write_bytes(b"x")
    os.utime(stale, (0, 0))

    assert gateway.cleanup_stale_tmp(config) == 1
    assert fresh.exists()
    assert not stale.exists()


def test_layout_requires_one_filesystem(env, monkeypatch):
    """`.tmp/` jinde než `queue/` znamená, že přesun není atomický."""
    config = env.gateway
    gateway.ensure_layout(config)
    real_stat = Path.stat

    class DevOverride:
        def __init__(self, real, dev):
            self._real = real
            self.st_dev = dev

        def __getattr__(self, name):
            return getattr(self._real, name)

    def fake_stat(self, **kwargs):
        result = real_stat(self, **kwargs)
        return DevOverride(result, 99 if self.name == ".tmp" else result.st_dev)

    monkeypatch.setattr(Path, "stat", fake_stat)
    with pytest.raises(ConfigError, match="stejném souborovém systému"):
        gateway.ensure_layout(config)


# --- idempotence ---------------------------------------------------------


def test_same_recording_twice_does_not_duplicate(client, env):
    """Shortcut odeslal dvakrát: druhý pokus přepíše ten samý soubor."""
    data = audio_bytes(b"stejna-nahravka")

    first = upload(client, data)
    second = upload(client, data)

    assert first.status_code == second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert [p.name for p in env.queue_dir.iterdir()] == [first.json()["id"]]
    assert (env.queue_dir / first.json()["id"]).read_bytes() == data
    assert list(env.gateway.tmp_dir.iterdir()) == []


def test_already_archived_recording_is_not_requeued(client, env):
    """Nahrávku, kterou worker odbavil, nesmí opakovaný upload vrátit do fronty."""
    data = audio_bytes(b"uz-zpracovana")
    first = upload(client, data)
    name = first.json()["id"]
    env.archive_dir.mkdir(parents=True, exist_ok=True)
    shutil.move(str(env.queue_dir / name), str(env.archive_dir / name))

    second = upload(client, data)

    assert second.status_code == 200
    assert second.json()["id"] == name
    assert list(env.queue_dir.iterdir()) == []
    assert list(env.gateway.tmp_dir.iterdir()) == []


def test_different_recordings_stay_separate(client, env):
    first = upload(client, audio_bytes(b"jedna"))
    second = upload(client, audio_bytes(b"dva"))

    assert first.json()["id"] != second.json()["id"]
    assert len(list(env.queue_dir.iterdir())) == 2


def test_matching_hash_prefix_is_not_enough(env):
    """Shoda šesti znaků názvu nestačí — rozhoduje celý obsah."""
    config = env.gateway
    gateway.ensure_layout(config)
    data = audio_bytes(b"original")
    digest = hashlib.sha256(data).hexdigest()
    impostor = config.queue_dir / f"2026-01-01T000000-{digest[:6]}.m4a"
    impostor.write_bytes(audio_bytes(b"neco-jineho", size=len(data)))

    assert gateway.find_duplicate(config, digest, len(data)) is None

    impostor.write_bytes(data)
    assert gateway.find_duplicate(config, digest, len(data)) == impostor


# --- bind ----------------------------------------------------------------


def test_resolve_bind_host_passes_addresses_through():
    assert gateway.resolve_bind_host("100.101.102.103") == "100.101.102.103"
    assert gateway.resolve_bind_host("pi-voicenotes") == "pi-voicenotes"


def test_public_bind_is_refused(env, monkeypatch, capsys):
    config = env.config_path.read_text(encoding="utf-8")
    env.config_path.write_text(
        config.replace("host: 127.0.0.1", "host: 0.0.0.0"), encoding="utf-8"
    )

    assert gateway.main(["--config", str(env.config_path)]) == 2
    assert "poslouchal na všech rozhraních" in capsys.readouterr().err
