"""Résumé des mesures d'un item, dénormalisé dans `item.dmf` (module PUR, sans Girder).

La collection `dmf_annotation` reste la SOURCE DE VÉRITÉ (CRUD unitaire, index, cf.
models.py). Ce résumé en est une PROJECTION en lecture seule, recalculée à chaque écriture
d'annotation : il rend les mesures visibles à tout client générique de Girder — filtres
`GET /item`, outils d'analyse, assistant girder-mcp (racine `dmf`) — sans route dédiée.
Le contrôle d'accès est celui de l'item (champ exposé au niveau READ), exactement comme
`GET /dmf/item/:id/annotations`.

Forme (version SUMMARY_VERSION) :

    item.dmf = {
      "v": 1,
      "count": 3,                          # nombre total d'annotations de l'item
      "types": ["distance", "point"],      # types présents (triés)
      "labels": ["VD", "VG"],              # libellés non vides présents (triés)
      "truncated": false,                  # true si `measurements` a été borné
      "updated": <datetime>,               # date du dernier recalcul
      "measurements": [                    # une entrée PLATE par mesure, sans géométrie
        {"key", "type", "label", "lengthMm", "lengthPx", "positionPx", "spacingSource",
         "frameIndex", "seriesInstanceUID", "sopInstanceUID", "creatorLogin", "created"}
      ]
    }

Entrées plates (valeurs remontées hors de `values`) : un filtre ou une agrégation y accède
par un chemin simple (`dmf.measurements.lengthMm`), sans connaître le format client. La
géométrie (coordonnées pixel) est omise : volumineuse et sans intérêt pour l'analyse.
"""

import numbers

# À incrémenter à chaque changement de forme : le backfill du démarrage recalcule alors
# tous les items annotés dont le résumé porte une autre version.
SUMMARY_VERSION = 1

# Borne la taille du document item (limite Mongo : 16 Mo). Au-delà, `truncated` = true ;
# `count`, `types` et `labels` restent calculés sur TOUTES les annotations.
MAX_MEASUREMENTS = 2000
MAX_LABEL = 500
MAX_TEXT = 128

_VALUE_FIELDS = ("lengthMm", "lengthPx", "positionPx")


def _number(value):
    """Nombre fini, ou None (les booléens et NaN/inf ne sont pas des mesures)."""
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        return None
    value = float(value) if not isinstance(value, int) else value
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return value


def _text(value, limit=MAX_TEXT):
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value[:limit] or None


def _int(value):
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def measurementEntry(doc):
    """Document `dmf_annotation` → entrée plate du résumé."""
    values = doc.get("values") if isinstance(doc.get("values"), dict) else {}
    entry = {
        "key": _text(doc.get("key")),
        "type": _text(doc.get("type")),
        "label": _text(doc.get("label"), MAX_LABEL),
        "spacingSource": _text(values.get("spacingSource")),
        "frameIndex": _int(doc.get("frameIndex")),
        "seriesInstanceUID": _text(doc.get("seriesInstanceUID")),
        "sopInstanceUID": _text(doc.get("sopInstanceUID")),
        "creatorLogin": _text(doc.get("creatorLogin")),
        "created": doc.get("created"),
    }
    for field in _VALUE_FIELDS:
        entry[field] = _number(values.get(field))
    return entry


def buildSummary(docs, now):
    """Annotations d'un item (ordre chronologique) → résumé `item.dmf`, ou None si aucune."""
    docs = list(docs)
    if not docs:
        return None
    entries = [measurementEntry(d) for d in docs]
    return {
        "v": SUMMARY_VERSION,
        "count": len(entries),
        "types": sorted({e["type"] for e in entries if e["type"]}),
        "labels": sorted({e["label"] for e in entries if e["label"]}),
        "truncated": len(entries) > MAX_MEASUREMENTS,
        "updated": now,
        "measurements": entries[:MAX_MEASUREMENTS],
    }
