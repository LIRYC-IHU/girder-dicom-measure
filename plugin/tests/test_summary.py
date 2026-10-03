"""Résumé dénormalisé des mesures (`item.dmf`) : fonctions pures de summary.py."""

import datetime

import summary  # chargé via conftest (dossier du package sur sys.path)

NOW = datetime.datetime(2026, 10, 3, 12, 0, tzinfo=datetime.timezone.utc)
CREATED = datetime.datetime(2026, 10, 1, 9, 30)


def _doc(**overrides):
    doc = {
        "key": "uuid-1",
        "type": "distance",
        "geometry": {"start": {"x": 1, "y": 2}, "end": {"x": 30, "y": 40}},
        "values": {"lengthPx": 50.0, "lengthMm": 32.5, "spacingSource": "PixelSpacing"},
        "frameIndex": 3,
        "sopInstanceUID": "1.2.3.4",
        "seriesInstanceUID": "1.2.3",
        "label": "VD",
        "creatorLogin": "alice",
        "creatorName": "Alice Martin",
        "created": CREATED,
    }
    doc.update(overrides)
    return doc


def test_no_annotation_gives_no_summary():
    assert summary.buildSummary([], NOW) is None
    assert summary.buildSummary(iter(()), NOW) is None


def test_entry_is_flat_and_drops_geometry():
    entry = summary.measurementEntry(_doc())
    assert entry == {
        "key": "uuid-1",
        "type": "distance",
        "label": "VD",
        "lengthMm": 32.5,
        "lengthPx": 50.0,
        "positionPx": None,
        "spacingSource": "PixelSpacing",
        "frameIndex": 3,
        "seriesInstanceUID": "1.2.3",
        "sopInstanceUID": "1.2.3.4",
        "creatorLogin": "alice",
        "created": CREATED,
    }
    # Ni géométrie, ni nom complet du créateur (le login suffit à l'analyse).
    assert "geometry" not in entry and "creatorName" not in entry


def test_summary_aggregates_types_and_labels_over_all_annotations():
    docs = [
        _doc(key="a", type="distance", label="VD"),
        _doc(key="b", type="point", label=""),
        _doc(key="c", type="distance", label="VG"),
        _doc(key="d", type="level-h", label="VD"),
    ]
    out = summary.buildSummary(docs, NOW)
    assert out["v"] == summary.SUMMARY_VERSION
    assert out["count"] == 4
    assert out["types"] == ["distance", "level-h", "point"]
    assert out["labels"] == ["VD", "VG"]
    assert out["truncated"] is False
    assert out["updated"] == NOW
    # L'ordre (chronologique, fourni par l'appelant) est conservé.
    assert [m["key"] for m in out["measurements"]] == ["a", "b", "c", "d"]


def test_non_numeric_values_are_dropped():
    for bad in ("12", True, None, float("nan"), float("inf"), {"x": 1}, [1]):
        entry = summary.measurementEntry(_doc(values={"lengthMm": bad, "lengthPx": bad}))
        assert entry["lengthMm"] is None and entry["lengthPx"] is None, bad
    entry = summary.measurementEntry(_doc(values={"lengthPx": 7, "positionPx": 12.25}))
    assert entry["lengthPx"] == 7 and isinstance(entry["lengthPx"], int)
    assert entry["positionPx"] == 12.25


def test_malformed_fields_do_not_break_the_summary():
    """Le format client n'est pas validé à l'écriture : le résumé doit tout encaisser."""
    doc = {
        "key": None,
        "type": 42,
        "values": "pas un objet",
        "frameIndex": "3",
        "label": ["liste"],
        "sopInstanceUID": {"x": 1},
    }
    entry = summary.measurementEntry(doc)
    assert entry["type"] is None and entry["label"] is None and entry["key"] is None
    assert entry["frameIndex"] is None and entry["sopInstanceUID"] is None
    assert entry["lengthMm"] is None and entry["spacingSource"] is None
    out = summary.buildSummary([doc], NOW)
    assert out["count"] == 1 and out["types"] == [] and out["labels"] == []


def test_frame_index_rejects_booleans():
    assert summary.measurementEntry(_doc(frameIndex=True))["frameIndex"] is None
    assert summary.measurementEntry(_doc(frameIndex=0))["frameIndex"] == 0


def test_text_is_trimmed_and_bounded():
    entry = summary.measurementEntry(_doc(label="  " + "x" * 2000 + "  ", type="  point "))
    assert entry["type"] == "point"
    assert len(entry["label"]) == summary.MAX_LABEL
    assert summary.measurementEntry(_doc(label="   "))["label"] is None
    assert len(summary.measurementEntry(_doc(key="k" * 1000))["key"]) == summary.MAX_TEXT


def test_measurements_are_capped_but_counts_are_not(monkeypatch):
    monkeypatch.setattr(summary, "MAX_MEASUREMENTS", 3)
    docs = [_doc(key=str(i), label="L%d" % i) for i in range(5)]
    out = summary.buildSummary(docs, NOW)
    assert out["count"] == 5 and out["truncated"] is True
    assert len(out["measurements"]) == 3
    # Libellés calculés sur TOUTES les annotations, pas seulement les entrées conservées.
    assert out["labels"] == ["L0", "L1", "L2", "L3", "L4"]
