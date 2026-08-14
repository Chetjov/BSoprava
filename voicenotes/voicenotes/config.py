"""Načtení `config.yaml`. Jeden soubor, dvě sekce: `gateway:` a `worker:`.

Tajemství (token, API klíče) se v souboru nikdy nedrží — v configu je jen
*jméno* proměnné prostředí, hodnota se čte až za běhu.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = Path("/etc/voicenotes/config.yaml")
CONFIG_PATH_ENV = "VOICENOTES_CONFIG"

MB = 1024 * 1024


class ConfigError(RuntimeError):
    """Config je špatně. Padáme hned při startu, ne až u první nahrávky."""


def _expand(value: str | os.PathLike[str]) -> Path:
    return Path(os.path.expandvars(str(value))).expanduser()


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    section = data.get(name)
    if section is None:
        raise ConfigError(f"config: chybí sekce '{name}:'")
    if not isinstance(section, dict):
        raise ConfigError(f"config: sekce '{name}:' musí být mapa")
    return section


def _require(section: dict[str, Any], key: str, where: str) -> Any:
    if key not in section or section[key] is None:
        raise ConfigError(f"config: chybí '{where}.{key}'")
    return section[key]


@dataclass(frozen=True)
class GatewayConfig:
    """Nastavení HTTP endpointu na Pi."""

    root: Path
    host: str
    port: int
    max_upload_bytes: int
    auth_token_env: str
    allow_public_bind: bool

    @property
    def queue_dir(self) -> Path:
        return self.root / "queue"

    @property
    def tmp_dir(self) -> Path:
        return self.root / ".tmp"

    @property
    def archive_dir(self) -> Path:
        return self.root / "archive"

    @property
    def rejected_dir(self) -> Path:
        return self.archive_dir / "rejected"

    @property
    def all_dirs(self) -> tuple[Path, ...]:
        return (self.queue_dir, self.tmp_dir, self.archive_dir, self.rejected_dir)

    def auth_token(self) -> str:
        token = os.environ.get(self.auth_token_env, "")
        if not token:
            raise ConfigError(
                f"proměnná prostředí {self.auth_token_env} není nastavená — "
                "gateway bez tokenu nestartuje"
            )
        return token

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GatewayConfig:
        return cls(
            root=_expand(_require(data, "root", "gateway")),
            host=str(data.get("host", "127.0.0.1")),
            port=int(data.get("port", 8080)),
            max_upload_bytes=int(data.get("max_upload_mb", 25) * MB),
            auth_token_env=str(data.get("auth_token_env", "VOICENOTES_TOKEN")),
            allow_public_bind=bool(data.get("allow_public_bind", False)),
        )


@dataclass(frozen=True)
class RemoteConfig:
    """Kde na Pi leží fronta a jak se tam dostat."""

    kind: str  # "ssh" | "local"
    root: Path
    host: str = ""
    user: str = ""
    port: int = 22
    identity_file: Path | None = None
    label: str = ""
    connect_timeout_s: int = 10
    transfer_timeout_s: int = 300

    @property
    def queue_dir(self) -> Path:
        return self.root / "queue"

    @property
    def archive_dir(self) -> Path:
        return self.root / "archive"

    @property
    def rejected_dir(self) -> Path:
        return self.archive_dir / "rejected"

    @property
    def target(self) -> str:
        """Cíl pro ssh/rsync, tj. ``user@host`` nebo jen ``host``."""
        return f"{self.user}@{self.host}" if self.user else self.host

    @property
    def display(self) -> str:
        """Prefix cesty k audiu ve frontmatteru, např. ``pi``."""
        return self.label or self.host or "local"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RemoteConfig:
        kind = str(data.get("kind", "ssh"))
        if kind not in {"ssh", "local"}:
            raise ConfigError("config: worker.remote.kind musí být 'ssh' nebo 'local'")
        if kind == "ssh" and not data.get("host"):
            raise ConfigError("config: worker.remote.host je povinný pro kind: ssh")
        identity = data.get("identity_file")
        return cls(
            kind=kind,
            root=_expand(_require(data, "root", "worker.remote")),
            host=str(data.get("host", "")),
            user=str(data.get("user", "")),
            port=int(data.get("port", 22)),
            identity_file=_expand(identity) if identity else None,
            label=str(data.get("label", "")),
            connect_timeout_s=int(data.get("connect_timeout_s", 10)),
            transfer_timeout_s=int(data.get("transfer_timeout_s", 300)),
        )


@dataclass(frozen=True)
class VaultConfig:
    root: Path
    inbox: str = "Inbox"
    #: Pracovní adresář uvnitř vaultu — musí být na stejném svazku jako inbox,
    #: aby šel zápis dokončit atomicky. Do Syncthingu patří na ignore list.
    staging_dirname: str = ".voicenotes-tmp"

    @property
    def inbox_dir(self) -> Path:
        return self.root / self.inbox

    @property
    def staging_dir(self) -> Path:
        return self.root / self.staging_dirname

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> VaultConfig:
        return cls(
            root=_expand(_require(data, "root", "worker.vault")),
            inbox=str(data.get("inbox", "Inbox")),
            staging_dirname=str(data.get("staging_dirname", ".voicenotes-tmp")),
        )


@dataclass(frozen=True)
class TranscribeConfig:
    """Fáze 2 — přepis. Výchozí hodnoty jsou laděné na češtinu z kapsy."""

    enabled: bool = True
    model: str = "large-v3"
    device: str = "auto"
    compute_type: str = "auto"
    #: Explicitně, nikdy autodetekce — ta u krátkých nahrávek přepne na slovenštinu.
    language: str = "cs"
    beam_size: int = 5
    vad: bool = True
    vad_threshold: float = 0.5
    vad_min_silence_ms: int = 500
    #: Vypnuté kvůli smyčkám, ve kterých se model zacyklí na jedné frázi.
    condition_on_previous_text: bool = False
    initial_prompt_terms: tuple[str, ...] = ()
    initial_prompt_override: str | None = None
    download_root: Path | None = None

    def build_initial_prompt(self) -> str | None:
        """Slovníček jmen a termínů, které model bez nápovědy komolí."""
        if self.initial_prompt_override:
            return self.initial_prompt_override
        if not self.initial_prompt_terms:
            return None
        return ", ".join(self.initial_prompt_terms) + "."

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TranscribeConfig:
        terms = data.get("initial_prompt_terms") or []
        if not isinstance(terms, list):
            raise ConfigError("config: worker.transcribe.initial_prompt_terms musí být seznam")
        download_root = data.get("download_root")
        return cls(
            enabled=bool(data.get("enabled", True)),
            model=str(data.get("model", "large-v3")),
            device=str(data.get("device", "auto")),
            compute_type=str(data.get("compute_type", "auto")),
            language=str(data.get("language", "cs")),
            beam_size=int(data.get("beam_size", 5)),
            vad=bool(data.get("vad", True)),
            vad_threshold=float(data.get("vad_threshold", 0.5)),
            vad_min_silence_ms=int(data.get("vad_min_silence_ms", 500)),
            condition_on_previous_text=bool(data.get("condition_on_previous_text", False)),
            initial_prompt_terms=tuple(str(term) for term in terms),
            initial_prompt_override=(
                str(data["initial_prompt"]) if data.get("initial_prompt") else None
            ),
            download_root=_expand(download_root) if download_root else None,
        )


@dataclass(frozen=True)
class WorkerConfig:
    remote: RemoteConfig
    vault: VaultConfig
    work_dir: Path
    transcribe: TranscribeConfig = field(default_factory=TranscribeConfig)
    timezone: str = "Europe/Prague"
    ffprobe_path: str = "ffprobe"
    rsync_path: str = "rsync"
    ssh_path: str = "ssh"
    #: Nezpracované sekce pro pozdější fáze (přepis, strukturování).
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def incoming_dir(self) -> Path:
        return self.work_dir / "incoming"

    @property
    def state_dir(self) -> Path:
        return self.work_dir / "state"

    @property
    def ledger_path(self) -> Path:
        return self.state_dir / "processed.jsonl"

    @property
    def lock_path(self) -> Path:
        return self.work_dir / "worker.lock"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkerConfig:
        return cls(
            remote=RemoteConfig.from_dict(_section(data, "remote")),
            vault=VaultConfig.from_dict(_section(data, "vault")),
            work_dir=_expand(_require(data, "work_dir", "worker")),
            transcribe=TranscribeConfig.from_dict(data.get("transcribe") or {}),
            timezone=str(data.get("timezone", "Europe/Prague")),
            ffprobe_path=str(data.get("ffprobe_path", "ffprobe")),
            rsync_path=str(data.get("rsync_path", "rsync")),
            ssh_path=str(data.get("ssh_path", "ssh")),
            raw=data,
        )


def load_yaml(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError(f"config nenalezen: {path}") from exc
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"config {path}: kořen musí být mapa")
    return data


def config_path(explicit: str | os.PathLike[str] | None = None) -> Path:
    if explicit:
        return Path(explicit)
    return Path(os.environ.get(CONFIG_PATH_ENV) or DEFAULT_CONFIG_PATH)


def load_gateway_config(path: str | os.PathLike[str] | None = None) -> GatewayConfig:
    resolved = config_path(path)
    return GatewayConfig.from_dict(_section(load_yaml(resolved), "gateway"))


def load_worker_config(path: str | os.PathLike[str] | None = None) -> WorkerConfig:
    resolved = config_path(path)
    return WorkerConfig.from_dict(_section(load_yaml(resolved), "worker"))
