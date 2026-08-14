"""Přístup k frontě na Pi. rsync přes SSH, žádné API mezi stroji.

Dvě implementace za jedním rozhraním: `SshRemoteQueue` pro provoz,
`LocalRemoteQueue` pro testy a lokální vývoj (fronta je jen složka).
"""

from __future__ import annotations

import logging
import shlex
import shutil
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path

from ..config import RemoteConfig

log = logging.getLogger("voicenotes.worker.remote")


class RemoteUnavailable(RuntimeError):
    """Pi neodpovídá. Není to chyba — zkusí se to příští běh."""


class RemoteQueue(ABC):
    """Fronta na druhé straně: stáhnout, pak přesunout do archivu."""

    @abstractmethod
    def is_available(self) -> bool: ...

    @abstractmethod
    def fetch_into(self, destination: Path) -> list[Path]:
        """Stáhne frontu do `destination` a vrátí seznam lokálních souborů."""

    @abstractmethod
    def archive(self, name: str) -> bool:
        """Přesune `queue/<name>` do `archive/<name>`. Nikdy nemaže."""


class SshRemoteQueue(RemoteQueue):
    def __init__(
        self,
        config: RemoteConfig,
        *,
        ssh_path: str = "ssh",
        rsync_path: str = "rsync",
    ) -> None:
        self.config = config
        self.ssh_path = ssh_path
        self.rsync_path = rsync_path

    def ssh_command(self) -> list[str]:
        command = [
            self.ssh_path,
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={self.config.connect_timeout_s}",
        ]
        if self.config.port != 22:
            command += ["-p", str(self.config.port)]
        if self.config.identity_file:
            command += ["-i", str(self.config.identity_file)]
        return command

    def rsync_command(self, destination: Path) -> list[str]:
        remote = f"{self.config.target}:{self.config.queue_dir}/"
        return [
            self.rsync_path,
            "-rt",
            "--protect-args",
            "--exclude",
            ".*",
            f"--timeout={self.config.transfer_timeout_s}",
            "-e",
            shlex.join(self.ssh_command()),
            remote,
            f"{destination}/",
        ]

    def archive_command(self, name: str) -> list[str]:
        queue_item = shlex.quote(str(self.config.queue_dir / name))
        archive_dir = shlex.quote(str(self.config.archive_dir))
        archive_item = shlex.quote(str(self.config.archive_dir / name))
        # Idempotentně: když soubor ve frontě není (přesunutý předchozím
        # během), nic se neděje a návratový kód zůstává nulový.
        script = (
            f"mkdir -p {archive_dir} && "
            f"if [ -e {queue_item} ]; then mv -f {queue_item} {archive_item}; fi"
        )
        return self.ssh_command() + [self.config.target, script]

    def _run(self, command: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
        log.debug("spouštím: %s", shlex.join(command))
        try:
            return subprocess.run(
                command, capture_output=True, text=True, timeout=timeout, check=False
            )
        except FileNotFoundError as exc:
            raise RemoteUnavailable(f"chybí nástroj: {exc.filename}") from exc
        except subprocess.TimeoutExpired as exc:
            raise RemoteUnavailable(f"vypršel čas: {shlex.join(command)}") from exc

    def is_available(self) -> bool:
        try:
            result = self._run(
                self.ssh_command() + [self.config.target, "true"],
                timeout=self.config.connect_timeout_s + 5,
            )
        except RemoteUnavailable as exc:
            log.info("Pi nedostupné: %s", exc)
            return False
        if result.returncode != 0:
            log.info("Pi nedostupné: ssh skončilo s %d %s", result.returncode, result.stderr.strip())
            return False
        return True

    def fetch_into(self, destination: Path) -> list[Path]:
        destination.mkdir(parents=True, exist_ok=True)
        result = self._run(
            self.rsync_command(destination),
            timeout=self.config.transfer_timeout_s + 30,
        )
        if result.returncode != 0:
            raise RemoteUnavailable(
                f"rsync skončil s {result.returncode}: {result.stderr.strip()}"
            )
        return sorted(item for item in destination.iterdir() if item.is_file())

    def archive(self, name: str) -> bool:
        result = self._run(
            self.archive_command(name),
            timeout=self.config.connect_timeout_s + 30,
        )
        if result.returncode != 0:
            log.warning(
                "nepodařilo se archivovat %s na Pi: %s", name, result.stderr.strip()
            )
            return False
        return True


class LocalRemoteQueue(RemoteQueue):
    """Fronta na lokálním disku — pro testy a běh obojího na jednom stroji."""

    def __init__(self, config: RemoteConfig) -> None:
        self.config = config

    def is_available(self) -> bool:
        return self.config.queue_dir.is_dir()

    def fetch_into(self, destination: Path) -> list[Path]:
        destination.mkdir(parents=True, exist_ok=True)
        if not self.config.queue_dir.is_dir():
            raise RemoteUnavailable(f"fronta neexistuje: {self.config.queue_dir}")
        for item in sorted(self.config.queue_dir.iterdir()):
            if item.is_file() and not item.name.startswith("."):
                shutil.copy2(item, destination / item.name)
        return sorted(item for item in destination.iterdir() if item.is_file())

    def archive(self, name: str) -> bool:
        source = self.config.queue_dir / name
        self.config.archive_dir.mkdir(parents=True, exist_ok=True)
        if not source.exists():
            return True
        shutil.move(str(source), str(self.config.archive_dir / name))
        return True


def build_remote(
    config: RemoteConfig, *, ssh_path: str = "ssh", rsync_path: str = "rsync"
) -> RemoteQueue:
    if config.kind == "local":
        return LocalRemoteQueue(config)
    return SshRemoteQueue(config, ssh_path=ssh_path, rsync_path=rsync_path)
