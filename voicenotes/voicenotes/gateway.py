"""Gateway na Pi 4 — přijme nahrávku, uloží ji do fronty, vrátí 200.

Žádný whisper, žádný model, žádné volání ven. Když tenhle proces spadne,
telefon nemá kam odesílat; proto je tu jen to nejnutnější.

Spuštění:  ``python -m voicenotes.gateway --config /etc/voicenotes/config.yaml``
"""

from __future__ import annotations

import argparse
import inspect
import logging
import os
import secrets
import subprocess
import sys
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterator

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException
from starlette.requests import Request as StarletteRequest

from .config import ConfigError, GatewayConfig, load_gateway_config
from .ids import hash_prefix, make_id, safe_suffix, sha256_file

log = logging.getLogger("voicenotes.gateway")

FIELD_NAME = "audio"
READ_CHUNK = 64 * 1024
#: Rezerva na hlavičky multipartu při kontrole Content-Length.
MULTIPART_SLACK = 8 * 1024
#: Nedokončené uploady starší než tohle se při startu uklidí.
STALE_TMP_AGE_S = 6 * 3600
PUBLIC_BINDS = {"0.0.0.0", "::", ""}

#: Starlette omezuje velikost části formuláře (default 1 MB) — musíme si
#: limit nastavit sami, jinak by neprošla ani běžná nahrávka.
_FORM_KWARGS = frozenset(inspect.signature(StarletteRequest.form).parameters)


def resolve_bind_host(host: str) -> str:
    """``tailscale`` → adresa z ``tailscale ip -4``; cokoli jiného beze změny."""
    if host != "tailscale":
        return host
    try:
        result = subprocess.run(
            ["tailscale", "ip", "-4"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ConfigError(f"nepodařilo se zjistit tailscale adresu: {exc}") from exc
    address = result.stdout.strip().splitlines()
    if not address or not address[0].strip():
        raise ConfigError("`tailscale ip -4` nevrátil žádnou adresu")
    return address[0].strip()


def ensure_layout(config: GatewayConfig) -> None:
    """Vyrobí adresáře a ověří, že `.tmp/` a `queue/` jsou na stejném svazku.

    Bez toho by `os.rename` nebyl atomický, ale kopírování přes hranici
    souborového systému — a worker by si mohl stáhnout půlku souboru.
    """
    for directory in config.all_dirs:
        directory.mkdir(parents=True, exist_ok=True)
    devices = {directory: directory.stat().st_dev for directory in config.all_dirs}
    if len(set(devices.values())) != 1:
        raise ConfigError(
            "queue/, .tmp/ a archive/ musí ležet na stejném souborovém systému "
            f"(nalezeno: { {str(k): v for k, v in devices.items()} })"
        )


def cleanup_stale_tmp(config: GatewayConfig, *, max_age_s: int = STALE_TMP_AGE_S) -> int:
    """Uklidí rozpracované uploady po pádu procesu."""
    now = time.time()
    removed = 0
    for leftover in config.tmp_dir.glob("*.part"):
        try:
            if now - leftover.stat().st_mtime > max_age_s:
                leftover.unlink()
                removed += 1
        except OSError:  # pragma: no cover - závod s jiným během
            continue
    if removed:
        log.info("uklizeno %d nedokončených uploadů z %s", removed, config.tmp_dir)
    return removed


def queue_size(config: GatewayConfig) -> int:
    if not config.queue_dir.is_dir():
        return 0
    return sum(1 for item in config.queue_dir.iterdir() if item.is_file())


def _search_dirs(config: GatewayConfig) -> Iterator[Path]:
    yield config.queue_dir
    yield config.archive_dir
    yield config.rejected_dir


def find_duplicate(config: GatewayConfig, digest_hex: str, size: int) -> Path | None:
    """Najde už uloženou nahrávku se stejným obsahem.

    Kandidáty vybírá podle prefixu hashe v názvu, ale potvrzuje je až
    porovnáním velikosti a celého sha256 — šest hex znaků je na jistotu
    málo a dvě různé nahrávky pod jedním názvem by byly tichá ztráta dat.
    """
    pattern = f"*-{hash_prefix(digest_hex)}*"
    for directory in _search_dirs(config):
        if not directory.is_dir():
            continue
        for candidate in sorted(directory.glob(pattern)):
            try:
                if candidate.stat().st_size != size:
                    continue
                if sha256_file(candidate) == digest_hex:
                    return candidate
            except OSError:  # pragma: no cover - soubor mezitím zmizel
                continue
    return None


def _fsync_dir(directory: Path) -> None:
    """Rename se na SD kartě může ztratit při výpadku napájení; tohle to zafixuje."""
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _error(status_code: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": detail})


def _too_large(config: GatewayConfig, size: int | None) -> JSONResponse:
    """Odmítnutá nahrávka zůstává v Diktafonu a telefon ji zkusí poslat znovu.

    Bez záznamu v žurnálu by se taková nahrávka do vaultu nikdy nedostala
    a nebylo by jak zjistit proč.
    """
    limit = config.max_upload_bytes
    log.warning(
        "odmítnuta nahrávka nad limit (%s B > %d B) — telefon ji bude zkoušet "
        "znovu, dokud nezvýšíš gateway.max_upload_mb nebo ji nesmažeš",
        size if size is not None else "?",
        limit,
    )
    return _error(413, f"soubor je nad limit {limit} B")


async def _read_form(request: Request, max_upload_bytes: int) -> Any:
    kwargs: dict[str, Any] = {}
    if "max_part_size" in _FORM_KWARGS:
        kwargs["max_part_size"] = max_upload_bytes
    if "max_files" in _FORM_KWARGS:
        kwargs["max_files"] = 2
    if "max_fields" in _FORM_KWARGS:
        kwargs["max_fields"] = 8
    return await request.form(**kwargs)


def create_app(config: GatewayConfig) -> FastAPI:
    expected_token = config.auth_token()
    ensure_layout(config)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        cleanup_stale_tmp(config)
        log.info(
            "gateway připravena: fronta=%s limit=%.1f MB",
            config.queue_dir,
            config.max_upload_bytes / (1024 * 1024),
        )
        yield

    app = FastAPI(
        title="voicenotes gateway",
        version="1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "queued": queue_size(config)}

    @app.post("/ingest")
    async def ingest(
        request: Request,
        x_auth_token: str = Header(default=""),
    ) -> JSONResponse:
        if not secrets.compare_digest(x_auth_token, expected_token):
            log.warning("odmítnut upload se špatným tokenem z %s", request.client)
            return _error(401, "neplatný token")

        declared = request.headers.get("content-length")
        if declared and declared.isdigit():
            if int(declared) > config.max_upload_bytes + MULTIPART_SLACK:
                return _too_large(config, int(declared))

        try:
            form = await _read_form(request, config.max_upload_bytes)
        except MultiPartException:
            return _too_large(config, None)

        try:
            upload = form.get(FIELD_NAME)
            if not isinstance(upload, UploadFile):
                log.warning("upload bez pole '%s' — odesílatel posílá něco jiného", FIELD_NAME)
                return _error(400, f"chybí pole '{FIELD_NAME}' se souborem")
            return await _store(config, upload)
        finally:
            await form.close()

    return app


async def _store(config: GatewayConfig, upload: UploadFile) -> JSONResponse:
    """Zapíše nahrávku do `.tmp/` a teprve hotovou ji přesune do `queue/`."""
    suffix = safe_suffix(upload.filename)
    partial = config.tmp_dir / f"{uuid.uuid4().hex}.part"
    digest = sha256()
    size = 0

    try:
        with partial.open("wb") as sink:
            while chunk := await upload.read(READ_CHUNK):
                size += len(chunk)
                if size > config.max_upload_bytes:
                    return _too_large(config, size)
                digest.update(chunk)
                sink.write(chunk)
            sink.flush()
            os.fsync(sink.fileno())

        if size == 0:
            log.warning("odmítnut prázdný upload")
            return _error(400, "prázdný soubor")

        digest_hex = digest.hexdigest()

        existing = find_duplicate(config, digest_hex, size)
        if existing is not None:
            if existing.parent == config.queue_dir:
                # Druhý pokus o tutéž nahrávku přepíše ten samý soubor,
                # místo aby ve frontě vznikl duplikát.
                os.rename(partial, existing)
                _fsync_dir(config.queue_dir)
                log.info("duplicitní upload → přepsán %s", existing.name)
            else:
                log.info("duplicitní upload už zpracovaný → %s", existing.name)
            return JSONResponse({"status": "queued", "id": existing.name})

        target = config.queue_dir / f"{make_id(datetime.now(), digest_hex)}{suffix}"
        while target.exists():  # pragma: no cover - jiný obsah ve stejné sekundě
            target = target.with_name(f"{target.stem}-{uuid.uuid4().hex[:4]}{suffix}")

        os.rename(partial, target)
        _fsync_dir(config.queue_dir)
        log.info("zařazeno %s (%d B)", target.name, size)
        return JSONResponse({"status": "queued", "id": target.name})

    except OSError as exc:
        log.exception("zápis do fronty selhal")
        return _error(507, f"nelze uložit nahrávku: {exc}")
    finally:
        partial.unlink(missing_ok=True)
        await upload.close()


def app_from_env() -> FastAPI:
    """Vstupní bod pro `uvicorn voicenotes.gateway:app_from_env --factory`."""
    return create_app(load_gateway_config())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="voicenotes gateway")
    parser.add_argument("--config", default=None, help="cesta ke config.yaml")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    try:
        config = load_gateway_config(args.config)
        host = resolve_bind_host(config.host)
        if host in PUBLIC_BINDS and not config.allow_public_bind:
            raise ConfigError(
                f"gateway.host={config.host!r} by poslouchal na všech rozhraních. "
                "Nastav adresu tailscale rozhraní (nebo host: tailscale)."
            )
        app = create_app(config)
    except ConfigError as exc:
        print(f"chyba konfigurace: {exc}", file=sys.stderr)
        return 2

    import uvicorn

    uvicorn.run(app, host=host, port=config.port, log_config=None)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "create_app",
    "app_from_env",
    "cleanup_stale_tmp",
    "ensure_layout",
    "find_duplicate",
    "queue_size",
    "resolve_bind_host",
    "main",
]
