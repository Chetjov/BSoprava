"""Fáze 4 — týdenní přehled inboxu."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from voicenotes import review as review_module
from voicenotes.review import collect_inbox, generate_review, read_frontmatter

PRAGUE = timezone(timedelta(hours=2))
NOW = datetime(2026, 8, 15, 9, 0, 0, tzinfo=PRAGUE)


def write_note(env, stem: str, *, status: str, created: datetime | str, title: str = "Titulek"):
    env.inbox.mkdir(parents=True, exist_ok=True)
    stamp = created if isinstance(created, str) else created.isoformat(timespec="seconds")
    path = env.inbox / f"{stem}.md"
    path.write_text(
        f"---\ncreated: {stamp}\nsource: voice\nstatus: {status}\n---\n\n"
        f"# {title}\n\nShrnutí.\n",
        encoding="utf-8",
    )
    return path


def days_ago(days: int) -> datetime:
    return NOW - timedelta(days=days)


# --- výběr poznámek -------------------------------------------------------


def test_only_old_inbox_notes_are_collected(env):
    write_note(env, "stara", status="inbox", created=days_ago(30), title="Stará")
    write_note(env, "tesne", status="inbox", created=days_ago(8), title="Těsně za hranou")
    write_note(env, "cerstva", status="inbox", created=days_ago(2), title="Čerstvá")
    write_note(env, "hotova", status="done", created=days_ago(30), title="Hotová")
    write_note(env, "review", status="needs-review", created=days_ago(30), title="K revizi")

    found = collect_inbox(env.worker, NOW)

    assert [note.title for note in found] == ["Stará", "Těsně za hranou"]


def test_notes_are_sorted_oldest_first(env):
    write_note(env, "b", status="inbox", created=days_ago(10), title="Novější")
    write_note(env, "a", status="inbox", created=days_ago(40), title="Nejstarší")

    found = collect_inbox(env.worker, NOW)

    assert [note.title for note in found] == ["Nejstarší", "Novější"]


def test_review_notes_do_not_count_themselves(env):
    write_note(env, "_review-2026-08-01", status="inbox", created=days_ago(14))
    write_note(env, "poznamka", status="inbox", created=days_ago(14), title="Skutečná")

    found = collect_inbox(env.worker, NOW)

    assert [note.title for note in found] == ["Skutečná"]


def test_note_without_frontmatter_is_skipped(env):
    env.inbox.mkdir(parents=True, exist_ok=True)
    (env.inbox / "rucni.md").write_text("# Ruční poznámka\n\nBez frontmatteru.\n", encoding="utf-8")
    write_note(env, "poznamka", status="inbox", created=days_ago(14), title="Skutečná")

    found = collect_inbox(env.worker, NOW)

    assert [note.title for note in found] == ["Skutečná"]


def test_broken_frontmatter_does_not_stop_the_scan(env, caplog):
    env.inbox.mkdir(parents=True, exist_ok=True)
    (env.inbox / "rozbita.md").write_text(
        "---\ncreated: [nedopsaný\nstatus: inbox\n---\n\n# Rozbitá\n", encoding="utf-8"
    )
    write_note(env, "poznamka", status="inbox", created=days_ago(14), title="Skutečná")

    with caplog.at_level("WARNING"):
        found = collect_inbox(env.worker, NOW)

    assert [note.title for note in found] == ["Skutečná"]
    assert "poškozený frontmatter" in caplog.text


def test_note_without_created_is_still_listed(env):
    """Chybějící datum není důvod poznámku zamlčet."""
    env.inbox.mkdir(parents=True, exist_ok=True)
    (env.inbox / "bez-data.md").write_text(
        "---\nsource: voice\nstatus: inbox\n---\n\n# Bez data\n", encoding="utf-8"
    )

    found = collect_inbox(env.worker, NOW)

    assert [note.title for note in found] == ["Bez data"]
    assert found[0].created is None


def test_naive_timestamp_is_read_in_the_configured_timezone(env):
    write_note(env, "naivni", status="inbox", created="2026-07-01T10:00:00", title="Naivní")

    found = collect_inbox(env.worker, NOW)

    assert found[0].created is not None
    assert found[0].created.tzinfo is not None


# --- vygenerovaný přehled -------------------------------------------------


def test_review_lists_links_and_counts(env):
    write_note(env, "2026-07-01T101010-import", status="inbox", created=days_ago(45),
               title="Přepracovat import")
    write_note(env, "2026-08-01T101010-retry", status="inbox", created=days_ago(14),
               title="Retry logika")

    written, count = generate_review(env.worker, now=NOW)

    assert count == 2
    assert written.name == "_review-2026-08-15.md"
    text = written.read_text(encoding="utf-8")
    assert "starších než 7 dní: **2**" in text
    assert "[[2026-07-01T101010-import]] — Přepracovat import" in text
    assert "[[2026-08-01T101010-retry]] — Retry logika" in text
    assert "(45 dní)" in text
    # Přehled sám nemá status inbox, jinak by se příště započítal.
    assert "status: review" in text


def test_empty_inbox_still_produces_a_review(env):
    env.inbox.mkdir(parents=True, exist_ok=True)

    written, count = generate_review(env.worker, now=NOW)

    assert count == 0
    assert "Čistý stůl" in written.read_text(encoding="utf-8")


def test_second_run_the_same_day_never_overwrites(env):
    write_note(env, "poznamka", status="inbox", created=days_ago(14))

    first, _ = generate_review(env.worker, now=NOW)
    original = first.read_text(encoding="utf-8")
    second, _ = generate_review(env.worker, now=NOW)

    assert first != second
    assert second.name == "_review-2026-08-15-2.md"
    assert first.read_text(encoding="utf-8") == original


def test_review_does_not_edit_existing_notes(env):
    path = write_note(env, "poznamka", status="inbox", created=days_ago(14))
    before = path.read_text(encoding="utf-8")

    generate_review(env.worker, now=NOW)

    assert path.read_text(encoding="utf-8") == before


def test_cli_reports_the_count(env, capsys):
    write_note(env, "poznamka", status="inbox", created=days_ago(14))

    assert review_module.main(["--config", str(env.config_path)]) == 0
    assert "1 poznámek k projití" in capsys.readouterr().out


def test_cli_fails_loudly_without_an_inbox(env, capsys):
    assert review_module.main(["--config", str(env.config_path)]) == 2
    assert "inbox neexistuje" in capsys.readouterr().err


# --- parsování frontmatteru ------------------------------------------------


def test_read_frontmatter_splits_head_and_body(tmp_path):
    path = tmp_path / "n.md"
    path.write_text(
        "---\nstatus: inbox\ntags: [napad]\n---\n\n# Titulek\n\nTělo — a --- uvnitř textu.\n",
        encoding="utf-8",
    )

    data, body = read_frontmatter(path)

    assert data == {"status": "inbox", "tags": ["napad"]}
    assert "# Titulek" in body
    assert "--- uvnitř textu" in body


@pytest.mark.parametrize(
    "text", ["", "žádný frontmatter", "---\nnedokončený blok\n"]
)
def test_read_frontmatter_tolerates_junk(tmp_path, text):
    path = tmp_path / "n.md"
    path.write_text(text, encoding="utf-8")

    data, _ = read_frontmatter(path)

    assert data == {}
