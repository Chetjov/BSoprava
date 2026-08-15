"""Tvar poznámky a pojmenování — části, na kterých staví fáze 2 a 3."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import yaml

from voicenotes.ids import ALLOWED_SUFFIXES, make_id, parse_id, safe_suffix, slugify
from voicenotes.worker.notes import (
    FALLBACK_TITLE,
    NoteData,
    dump_frontmatter,
    render_note,
    title_from_transcript,
    yaml_scalar,
)

PRAGUE = timezone(timedelta(hours=2))


def test_render_matches_the_agreed_shape():
    note = NoteData(
        title="Přepracovat retry logiku v importu",
        created=datetime(2026, 8, 14, 14, 32, 11, tzinfo=PRAGUE),
        audio="pi:/srv/voicenotes/archive/2026-08-14T143211-a3f9c1.m4a",
        status="inbox",
        duration_s=52,
        speech_s=47,
        transcript_model="large-v3",
        structure_model="qwen3:8b",
        tags=["napad"],
        summary="Shrnutí ve dvou větách.",
        tasks=["konkrétní úkol"],
        transcript="Celý surový text, beze změn.",
    )

    assert render_note(note) == (
        "---\n"
        "created: 2026-08-14T14:32:11+02:00\n"
        "source: voice\n"
        "duration_s: 52\n"
        "speech_s: 47\n"
        "audio: pi:/srv/voicenotes/archive/2026-08-14T143211-a3f9c1.m4a\n"
        "transcript_model: large-v3\n"
        "structure_model: qwen3:8b\n"
        "tags: [napad]\n"
        "status: inbox\n"
        "---\n"
        "\n"
        "# Přepracovat retry logiku v importu\n"
        "\n"
        "Shrnutí ve dvou větách.\n"
        "\n"
        "## Úkoly\n"
        "\n"
        "- [ ] konkrétní úkol\n"
        "\n"
        "## Přepis\n"
        "\n"
        "Celý surový text, beze změn.\n"
    )


def test_unknown_fields_are_omitted_not_nulled():
    note = NoteData(
        title="Bez modelů",
        created=datetime(2026, 8, 14, 14, 32, 11, tzinfo=PRAGUE),
        audio="pi:/srv/x.m4a",
    )
    text = render_note(note)

    assert "transcript_model" not in text
    assert "duration_s" not in text
    assert "null" not in text
    assert "## Přepis" not in text


def test_failure_puts_the_error_in_the_body():
    note = NoteData(
        title="Nahrávka 2026-08-14",
        created=datetime(2026, 8, 14, 14, 32, 11, tzinfo=PRAGUE),
        audio="pi:/srv/x.m4a",
        status="needs-review",
        error="CUDA out of memory",
    )
    text = render_note(note)

    assert "status: needs-review" in text
    assert "## Chyba" in text
    assert "CUDA out of memory" in text


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # Plain scalar unese i dvojtečku bez mezery a uvozovky uvnitř —
        # frontmatter tak vypadá jako v zadání, ne jako uniklý JSON.
        ("inbox", "inbox"),
        ("large-v3", "large-v3"),
        ("pi:/srv/voicenotes/archive/x.m4a", "pi:/srv/voicenotes/archive/x.m4a"),
        ("qwen3:8b", "qwen3:8b"),
        ('uvozovky "uvnitř"', 'uvozovky "uvnitř"'),
        # Tohle už by YAML rozbilo nebo přečetlo jinak.
        ("Titulek: dvojtečka", '"Titulek: dvojtečka"'),
        ("- pomlčka na začátku", '"- pomlčka na začátku"'),
        ("  mezery  ", '"  mezery  "'),
        ("křížek # uvnitř", '"křížek # uvnitř"'),
        ("", '""'),
        (47, "47"),
    ],
)
def test_scalars_are_quoted_only_when_needed(value, expected):
    assert yaml_scalar(value) == expected


@pytest.mark.parametrize(
    "title",
    [
        "Přepracovat retry logiku",
        "Titulek: s dvojtečkou",
        "qwen3:8b",
        'uvozovky "uvnitř" a zpětné \\ lomítko',
        "- začíná pomlčkou",
        "# začíná křížkem",
        "[hranaté] {složené}",
        "*hvězdička & ampersand",
        "  mezery na krajích  ",
    ],
)
def test_frontmatter_survives_a_real_yaml_parser(title):
    """Ať se titulek zvrtne jakkoli, frontmatter musí zůstat čitelný."""
    note = NoteData(
        title="nepodstatné",
        created=datetime(2026, 8, 14, 14, 32, 11, tzinfo=PRAGUE),
        audio="pi:/srv/voicenotes/archive/2026-08-14T143211-a3f9c1.m4a",
        tags=["napad", "prace"],
    )
    fields = note.frontmatter() | {"title": title}

    parsed = yaml.safe_load(dump_frontmatter(fields).strip("-\n"))

    assert parsed["title"] == title
    assert parsed["audio"] == note.audio
    assert parsed["tags"] == ["napad", "prace"]
    assert parsed["source"] == "voice"


def test_frontmatter_skips_missing_values():
    assert dump_frontmatter({"a": 1, "b": None, "tags": ["x", "y"]}) == (
        "---\na: 1\ntags: [x, y]\n---"
    )


# --- názvy ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Přepracovat retry logiku", "prepracovat-retry-logiku"),
        ("Žluťoučký kůň úpěl ďábelské ódy", "zlutoucky-kun-upel-dabelske-ody"),
        ("  Více   mezer  ", "vice-mezer"),
        ("!!!", "bez-nazvu"),
        ("", "bez-nazvu"),
    ],
)
def test_slugify(text, expected):
    assert slugify(text) == expected


def test_slug_is_bounded_and_does_not_cut_mid_word():
    slug = slugify("velmi " * 40, max_length=30)

    assert len(slug) <= 30
    assert not slug.endswith("-")


@pytest.mark.parametrize(
    ("transcript", "expected"),
    [
        # Titulek končí na hranici věty, ne uprostřed myšlenky.
        (
            "Přepracovat retry logiku v importu. Padá to na timeoutu.",
            "Přepracovat retry logiku v importu",
        ),
        # Bez interpunkce se bere prvních pár slov.
        (
            "přepracovat retry logiku v importu, padá to na timeoutu",
            "Přepracovat retry logiku v importu, padá to na",
        ),
        # Příliš krátká první věta sama o sobě titulek nedá.
        ("Hele. Tohle je nápad na příště.", "Hele. Tohle je nápad na příště"),
        ("Krátká myšlenka.", "Krátká myšlenka"),
        ("...", FALLBACK_TITLE),
        ("", FALLBACK_TITLE),
        ("   ", FALLBACK_TITLE),
    ],
)
def test_title_from_transcript(transcript, expected):
    assert title_from_transcript(transcript) == expected


def test_title_from_transcript_does_not_cut_mid_word():
    title = title_from_transcript("nej" + "dlouhe " * 20)

    assert len(title) <= 70
    assert not title.endswith("-")
    assert title.split()[-1] in {"dlouhe", "nejdlouhe"}


def test_id_roundtrip():
    when = datetime(2026, 8, 14, 14, 32, 11)
    identifier = make_id(when, "a3f9c1deadbeef")

    assert identifier == "2026-08-14T143211-a3f9c1"
    assert parse_id(identifier) == (when, "a3f9c1")
    assert parse_id(identifier + ".m4a") == (when, "a3f9c1")


def test_id_with_disambiguation_suffix_still_parses():
    """Rozlišovací ocásek nesmí soubor odstavit z fronty."""
    assert parse_id("2026-08-14T143211-a3f9c1-1a2b.m4a") == (
        datetime(2026, 8, 14, 14, 32, 11),
        "a3f9c1",
    )


@pytest.mark.parametrize(
    "name",
    [
        "nesmysl",
        "2026-08-14T143211",
        "2026-08-14T143211-XYZ123",
        "2026-08-14T143211-a3f9c1-nazev",
        "",
    ],
)
def test_unparseable_ids_are_rejected(name):
    assert parse_id(name) is None


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("rec.m4a", ".m4a"),
        ("Audio Recording.M4A", ".m4a"),
        ("hlas.wav", ".wav"),
        ("../../etc/passwd", ".m4a"),
        ("skript.sh", ".m4a"),
        (None, ".m4a"),
        ("", ".m4a"),
    ],
)
def test_safe_suffix(filename, expected):
    assert safe_suffix(filename) == expected
    assert expected in ALLOWED_SUFFIXES
