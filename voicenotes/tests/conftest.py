from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from voicenotes.config import load_gateway_config, load_worker_config  # noqa: E402
from voicenotes.worker.structure import Structure, Structurer  # noqa: E402
from voicenotes.worker.transcribe import Transcriber, Transcript  # noqa: E402

TOKEN = "test-token-123"
#: Malý limit, ať se velké soubory netestují velkými soubory.
MAX_UPLOAD_MB = 0.01
MAX_UPLOAD_BYTES = int(MAX_UPLOAD_MB * 1024 * 1024)


@dataclass
class Env:
    """Celá pipeline na jednom disku: 'Pi' i vault jsou jen adresáře."""

    config_path: Path
    pi_root: Path
    vault_root: Path
    work_dir: Path

    @property
    def gateway(self):
        return load_gateway_config(self.config_path)

    @property
    def worker(self):
        return load_worker_config(self.config_path)

    @property
    def queue_dir(self) -> Path:
        return self.pi_root / "queue"

    @property
    def archive_dir(self) -> Path:
        return self.pi_root / "archive"

    @property
    def inbox(self) -> Path:
        return self.vault_root / "Inbox"

    def notes(self) -> list[Path]:
        return sorted(p for p in self.inbox.glob("*.md")) if self.inbox.is_dir() else []


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    pi_root = tmp_path / "pi" / "voicenotes"
    vault_root = tmp_path / "vault"
    work_dir = tmp_path / "work"
    vault_root.mkdir(parents=True)  # vault existuje vždycky, pipeline ho nezakládá

    config = {
        "gateway": {
            "root": str(pi_root),
            "host": "127.0.0.1",
            "port": 8080,
            "max_upload_mb": MAX_UPLOAD_MB,
            "auth_token_env": "VOICENOTES_TOKEN",
        },
        "worker": {
            "remote": {"kind": "local", "root": str(pi_root), "label": "pi"},
            "work_dir": str(work_dir),
            "vault": {"root": str(vault_root), "inbox": "Inbox"},
            "timezone": "Europe/Prague",
            # ffprobe v CI není; ať se netestuje na jeho přítomnosti.
            "ffprobe_path": "ffprobe-neexistuje",
            # Testy transportu běží bez modelu; fáze 2 si přepisovač podstrčí.
            "transcribe": {"enabled": False},
            "structure": {"enabled": False},
        },
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")

    monkeypatch.setenv("VOICENOTES_TOKEN", TOKEN)
    monkeypatch.setenv("VOICENOTES_CONFIG", str(config_path))
    return Env(config_path, pi_root, vault_root, work_dir)


@pytest.fixture
def client(env: Env):
    from fastapi.testclient import TestClient

    from voicenotes.gateway import create_app

    with TestClient(create_app(env.gateway)) as test_client:
        yield test_client


def audio_bytes(seed: bytes = b"nahravka", size: int = 2048) -> bytes:
    """Deterministický 'zvuk' — jde o bajty, ne o obsah."""
    out = bytearray()
    block = hashlib.sha256(seed).digest()
    while len(out) < size:
        out.extend(block)
        block = hashlib.sha256(block).digest()
    return bytes(out[:size])


class FakeTranscriber(Transcriber):
    """Přepisovač bez modelu — vrátí, co mu test nastaví.

    `per_file` mapuje název souboru na text nebo na výjimku, kterou má
    přepis vyhodit.
    """

    def __init__(
        self,
        default: str | Exception = "Přepracovat retry logiku v importu.",
        per_file: dict[str, str | Exception] | None = None,
        *,
        model: str = "large-v3",
        duration_s: int | None = 52,
        speech_s: int | None = 47,
    ) -> None:
        self.default = default
        self.per_file = per_file or {}
        self.model = model
        self.duration_s = duration_s
        self.speech_s = speech_s
        self.calls: list[str] = []
        self.unloaded = 0

    def transcribe(self, path: Path) -> Transcript:
        self.calls.append(path.name)
        outcome = self.per_file.get(path.name, self.default)
        if isinstance(outcome, Exception):
            raise outcome
        return Transcript(
            text=outcome,
            model=self.model,
            language="cs",
            duration_s=self.duration_s,
            speech_s=self.speech_s,
            segments=outcome.count(".") or 1,
        )

    def unload(self) -> None:
        self.unloaded += 1


def upload(client, data: bytes, *, token: str = TOKEN, filename: str = "rec.m4a"):
    return client.post(
        "/ingest",
        headers={"X-Auth-Token": token},
        files={"audio": (filename, data, "audio/m4a")},
    )


class FakeStructurer(Structurer):
    """Strukturování bez modelu — vrátí, co mu test nastaví."""

    def __init__(
        self,
        outcome: Structure | Exception | None = None,
        *,
        model: str = "qwen3:8b",
        available: bool = True,
    ) -> None:
        self.outcome = outcome or Structure(
            title="Přepracovat retry logiku v importu",
            summary="Import padá na timeoutu u velkých souborů.",
            tags=["napad", "prace"],
            tasks=["zvýšit timeout v importu"],
            model=model,
        )
        self._model = model
        self._available = available
        self.calls: list[str] = []
        self.unloaded = 0

    @property
    def model(self) -> str:
        return self._model

    def available(self) -> bool:
        return self._available

    def structure(self, transcript: str) -> Structure:
        self.calls.append(transcript)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome

    def unload(self) -> None:
        self.unloaded += 1
