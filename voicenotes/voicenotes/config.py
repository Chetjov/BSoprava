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
class ReviewConfig:
    """Fáze 4 — týdenní přehled poznámek, které leží v inboxu."""

    older_than_days: int = 7
    #: Prefix přehledů; podle něj se poznámky přehledů vynechávají ze scanu.
    filename_prefix: str = "_review-"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReviewConfig:
        return cls(
            older_than_days=int(data.get("older_than_days", 7)),
            filename_prefix=str(data.get("filename_prefix", "_review-")),
        )


#: Uzavřený seznam tagů. Volné vymýšlení = dvě stě unikátních tagů za dva měsíce.
DEFAULT_TAGS = ("napad", "ukol", "poznamka", "otazka", "prace", "osobni")


@dataclass(frozen=True)
class OllamaConfig:
    model: str = "qwen3:8b"
    host: str = "http://127.0.0.1:11434"
    num_ctx: int = 8192
    temperature: float = 0.2
    #: Jak dlouho ollama drží model v paměti mezi dotazy během jednoho běhu.
    keep_alive: str = "5m"
    timeout_s: int = 180
    connect_timeout_s: int = 5

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OllamaConfig:
        return cls(
            model=str(data.get("model", "qwen3:8b")),
            host=str(data.get("host", "http://127.0.0.1:11434")).rstrip("/"),
            num_ctx=int(data.get("num_ctx", 8192)),
            temperature=float(data.get("temperature", 0.2)),
            keep_alive=str(data.get("keep_alive", "5m")),
            timeout_s=int(data.get("timeout_s", 180)),
            connect_timeout_s=int(data.get("connect_timeout_s", 5)),
        )


@dataclass(frozen=True)
class AnthropicConfig:
    model: str = "claude-opus-5"
    api_key_env: str = "ANTHROPIC_API_KEY"
    max_tokens: int = 2048
    #: Krátká poznámka není náročná úloha; vyšší effort by jen platil za tokeny.
    effort: str = "low"
    timeout_s: int = 120

    def api_key(self) -> str:
        return os.environ.get(self.api_key_env, "")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AnthropicConfig:
        return cls(
            model=str(data.get("model", "claude-opus-5")),
            api_key_env=str(data.get("api_key_env", "ANTHROPIC_API_KEY")),
            max_tokens=int(data.get("max_tokens", 2048)),
            effort=str(data.get("effort", "low")),
            timeout_s=int(data.get("timeout_s", 120)),
        )


@dataclass(frozen=True)
class StructureConfig:
    """Fáze 3 — titulek, shrnutí, tagy, úkoly."""

    enabled: bool = True
    backend: str = "ollama"
    tags: tuple[str, ...] = DEFAULT_TAGS
    max_tasks: int = 10
    ollama: OllamaConfig = field(default_factory=OllamaConfig)
    anthropic: AnthropicConfig = field(default_factory=AnthropicConfig)

    @property
    def model(self) -> str:
        return self.anthropic.model if self.backend == "anthropic" else self.ollama.model

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StructureConfig:
        backend = str(data.get("backend", "ollama"))
        if backend not in {"ollama", "anthropic"}:
            raise ConfigError(
                "config: worker.structure.backend musí být 'ollama' nebo 'anthropic'"
            )
        tags = data.get("tags")
        if tags is None:
            tags = list(DEFAULT_TAGS)
        if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
            raise ConfigError("config: worker.structure.tags musí být seznam řetězců")
        if not tags:
            raise ConfigError("config: worker.structure.tags nesmí být prázdný seznam")
        return cls(
            enabled=bool(data.get("enabled", True)),
            backend=backend,
            tags=tuple(tags),
            max_tasks=int(data.get("max_tasks", 10)),
            ollama=OllamaConfig.from_dict(data.get("ollama") or {}),
            anthropic=AnthropicConfig.from_dict(data.get("anthropic") or {}),
        )


@dataclass(frozen=True)
class WorkerConfig:
    remote: RemoteConfig
    vault: VaultConfig
    work_dir: Path
    transcribe: TranscribeConfig = field(default_factory=TranscribeConfig)
    structure: StructureConfig = field(default_factory=StructureConfig)
    review: ReviewConfig = field(default_factory=ReviewConfig)
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
            structure=StructureConfig.from_dict(data.get("structure") or {}),
            review=ReviewConfig.from_dict(data.get("review") or {}),
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
