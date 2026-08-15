"""Týdenní přehled poznámek, které leží v inboxu.

Bez téhle smyčky se z vaultu stane hřbitov: poznámky přibývají, nikdo je
nečte. Skript spočítá poznámky se `status: inbox` starší než týden a
vygeneruje `Inbox/_review-<datum>.md` se seznamem odkazů.

Přehled je **nový soubor**, žádná existující poznámka se needituje — stejné
pravidlo jako u zbytku pipeline kvůli Syncthingu.

    voicenotes-review --config ~/.config/voicenotes/config.yaml
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from .config import ConfigError, WorkerConfig, load_worker_config
from .worker.notes import STATUS_INBOX, dump_frontmatter
from .worker.run import resolve_timezone
from .worker.vault import VaultWriteError, write_note

log = logging.getLogger("voicenotes.review")

FRONTMATTER_SEPARATOR = "---"


@dataclass(frozen=True)
class InboxNote:
    path: Path
    title: str
    created: datetime | None

    def age_days(self, now: datetime) -> int | None:
        if self.created is None:
            return None
        return (now - self.created).days


def read_frontmatter(path: Path) -> tuple[dict[str, Any], str]:
    """Frontmatter a tělo poznámky. Soubor bez frontmatteru vrací prázdnou mapu."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        log.warning("nelze přečíst %s, přeskakuji", path)
        return {}, ""
    if not text.startswith(FRONTMATTER_SEPARATOR):
        return {}, text
    parts = text.split("\n" + FRONTMATTER_SEPARATOR, 1)
    if len(parts) != 2:
        return {}, text
    block = parts[0][len(FRONTMATTER_SEPARATOR) :]
    try:
        data = yaml.safe_load(block) or {}
    except yaml.YAMLError:
        log.warning("poškozený frontmatter v %s, přeskakuji", path)
        return {}, parts[1]
    return (data if isinstance(data, dict) else {}), parts[1]


def first_heading(body: str, fallback: str) -> str:
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return fallback


def as_datetime(value: Any, tzinfo: Any) -> datetime | None:
    """`created` z frontmatteru — YAML ho vrátí jako datum, text nebo nic."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime.min.time())
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
    else:
        return None
    return parsed.replace(tzinfo=tzinfo) if parsed.tzinfo is None else parsed


def collect_inbox(config: WorkerConfig, now: datetime) -> list[InboxNote]:
    """Poznámky se `status: inbox` starší než `review.older_than_days`."""
    inbox = config.vault.inbox_dir
    if not inbox.is_dir():
        raise ConfigError(f"inbox neexistuje: {inbox}")

    cutoff = now - timedelta(days=config.review.older_than_days)
    tzinfo = resolve_timezone(config.timezone)
    found: list[InboxNote] = []

    for path in sorted(inbox.glob("*.md")):
        if path.name.startswith(config.review.filename_prefix):
            continue  # přehledy samy sebe nezapočítávají
        frontmatter, body = read_frontmatter(path)
        if frontmatter.get("status") != STATUS_INBOX:
            continue
        created = as_datetime(frontmatter.get("created"), tzinfo)
        if created is not None and created > cutoff:
            continue
        found.append(
            InboxNote(path=path, title=first_heading(body, path.stem), created=created)
        )

    found.sort(key=lambda note: (note.created is None, note.created or now))
    return found


def render_review(config: WorkerConfig, notes: list[InboxNote], now: datetime) -> str:
    days = config.review.older_than_days
    frontmatter = dump_frontmatter(
        {
            "created": now.isoformat(timespec="seconds"),
            "source": "review",
            "status": "review",
        }
    )
    lines = [
        frontmatter,
        "",
        f"# Review {now.date().isoformat()}",
        "",
    ]
    if not notes:
        lines += [f"V inboxu není žádná poznámka starší než {days} dní. Čistý stůl.", ""]
        return "\n".join(lines).rstrip() + "\n"

    lines += [
        f"Poznámek se `status: inbox` starších než {days} dní: **{len(notes)}**.",
        "",
        "Projdi je a buď zpracuj, nebo změň `status`.",
        "",
    ]
    for note in notes:
        age = note.age_days(now)
        stamp = note.created.date().isoformat() if note.created else "bez data"
        suffix = f" ({age} dní)" if age is not None else ""
        lines.append(f"- [[{note.path.stem}]] — {note.title} · {stamp}{suffix}")
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def generate_review(config: WorkerConfig, *, now: datetime | None = None) -> tuple[Path, int]:
    tzinfo = resolve_timezone(config.timezone)
    moment = now or datetime.now(tzinfo)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=tzinfo)
    notes = collect_inbox(config, moment)
    stem = f"{config.review.filename_prefix}{moment.date().isoformat()}"
    written = write_note(config.vault, stem, render_review(config, notes, moment))
    return written, len(notes)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="týdenní přehled inboxu")
    parser.add_argument("--config", default=None, help="cesta ke config.yaml")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    try:
        config = load_worker_config(args.config)
        written, count = generate_review(config)
    except ConfigError as exc:
        print(f"chyba konfigurace: {exc}", file=sys.stderr)
        return 2
    except VaultWriteError as exc:
        print(f"vault není použitelný: {exc}", file=sys.stderr)
        return 1

    print(f"{count} poznámek k projití → {written}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

