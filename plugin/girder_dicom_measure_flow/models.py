"""Modèle Girder des annotations : collection dédiée `dmf_annotation`.

Sortir les mesures des métadonnées de l'item (`item.meta.annotations`) vers une collection
propre les rend interrogeables par l'API (liste filtrée/paginée, index Mongo, CRUD unitaire,
requêtes transverses). Chaque annotation est un document indépendant qui RÉFÉRENCE un item ;
le contrôle d'accès se fait via l'item (cf. rest.py).

Le champ `key` = identifiant CLIENT de la mesure (uuid / annotationUID Cornerstone) : il sert
de clé stable pour le CRUD côté client (pas de remapping avec l'_id Mongo).
"""

import datetime
import logging

from girder.models.item import Item
from girder.models.model_base import Model

logger = logging.getLogger(__name__)

# Champs « mesure » conservés tels quels (le reste est serveur : creator*, created, updated).
_MEASUREMENT_FIELDS = (
    "type",
    "geometry",
    "values",
    "frameIndex",
    "sopInstanceUID",
    "seriesInstanceUID",
    "label",
    "appVersion",
)


def _user_name(user):
    return (
        " ".join(filter(None, [user.get("firstName"), user.get("lastName")])) or user["login"]
    )


class Annotation(Model):
    def initialize(self):
        self.name = "dmf_annotation"
        self.ensureIndices(["itemId", "creatorId", "type", "key"])

    def validate(self, doc):
        return doc

    def listForItem(self, itemId, **kwargs):
        return self.find({"itemId": itemId}, sort=[("created", 1)], **kwargs)

    def fromMeasurement(self, measurement, item, user):
        """Mesure client → document de la collection (creator/date serveur, source de vérité)."""
        now = datetime.datetime.utcnow()
        doc = {field: measurement.get(field) for field in _MEASUREMENT_FIELDS}
        doc.update(
            {
                "key": measurement.get("id"),
                "itemId": item["_id"],
                "creatorId": user["_id"],
                "creatorLogin": user["login"],
                "creatorName": _user_name(user),
                "created": now,
                "updated": now,
            }
        )
        doc.setdefault("frameIndex", 0)
        return doc

    def toMeasurement(self, doc):
        """Document → mesure au format attendu par le client (clé `id`, `user`, `createdAt`)."""
        out = {field: doc.get(field) for field in _MEASUREMENT_FIELDS}
        out.update(
            {
                "id": doc.get("key"),
                "user": {
                    "id": str(doc["creatorId"]) if doc.get("creatorId") else None,
                    "login": doc.get("creatorLogin"),
                    "name": doc.get("creatorName"),
                },
                "createdAt": doc.get("created"),  # Girder sérialise les datetime en ISO
            }
        )
        return out


def migrateFromItemMetadata():
    """Déplace les annotations historiques (item.meta.annotations) vers la collection.

    Idempotent et non destructif des mesures : on dédoublonne par `key`, puis on RETIRE
    `meta.annotations` de l'item (déplacement à sens unique → pas de résurrection d'une
    mesure supprimée, et les redémarrages suivants ne re-scannent rien d'utile).
    """
    model = Annotation()
    migrated = 0
    for item in Item().find({"meta.annotations": {"$exists": True}}):
        anns = (item.get("meta") or {}).get("annotations") or []
        for a in anns:
            key = a.get("id")
            if not key or model.findOne({"key": key}):
                continue
            created = a.get("createdAt")
            user = a.get("user") or {}
            doc = {field: a.get(field) for field in _MEASUREMENT_FIELDS}
            doc.update(
                {
                    "key": key,
                    "itemId": item["_id"],
                    "creatorId": None,  # ancien format : id non garanti ObjectId
                    "creatorLogin": user.get("login"),
                    "creatorName": user.get("name"),
                    "created": created,
                    "updated": created,
                }
            )
            doc.setdefault("frameIndex", 0)
            model.save(doc)
            migrated += 1
        # Source historique retirée (la collection fait foi désormais).
        del item["meta"]["annotations"]
        Item().save(item)
    if migrated:
        logger.info("[dicom_measure_flow] %d annotation(s) migrées depuis item.meta", migrated)
