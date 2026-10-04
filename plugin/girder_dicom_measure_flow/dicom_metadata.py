"""Extraction des métadonnées DICOM à la réception des fichiers + tri des instances.

Réplique le comportement du plugin officiel `dicom_viewer`
(https://github.com/girder/girder/tree/v3.2.15/plugins/dicom_viewer) pour que NOTRE
plugin soit autosuffisant (pas de dépendance à `dicom_viewer`) :

  - à chaque fichier uploadé (event `data.process`), on parse les tags via pydicom
    (en-tête seul, `stop_before_pixels=True`) ;
  - on stocke sur l'item :
        item['dicom'] = {
            'meta':  <métadonnées communes à TOUS les fichiers DICOM de l'item>,
            'files': [ {'_id', 'name', 'dicom': {SeriesNumber, InstanceNumber, SliceLocation,
                                                 NumberOfFrames, PixelDataSHA256}}, ... ]
        }
    la liste `files` est TRIÉE par (SeriesNumber, InstanceNumber, SliceLocation, name)
    → le client lit cet ordre pour empiler les coupes (item 2 : tri des instances).

Le champ `dicom` est exposé en lecture via l'API REST (cf. __init__.py).

Écritures de `dicom` ATOMIQUES, jamais par `Item().save()` : les uploads parallèles d'une
série indexent le même item en même temps (cf. `handleUploadedDicom`, `REVISION_FIELD`).

`PixelDataSHA256` = empreinte de la donnée pixel (hors en-tête, cf. `transcode.pixelDataDigest`),
calculée à CHAQUE réception d'un fichier : elle permet de repérer une même image envoyée
plusieurs fois, y compris sous un autre patient ou dans un autre cas (`GET /dmf/pixelhash/:hash`).

"""

import logging

import pydicom
from girder.models.file import File
from girder.models.item import Item

from .dicom_tags import coerce_metadata, sort_key
from .transcode import pixelDataDigest

logger = logging.getLogger("girder.dicom_measure_flow")

# Révision de `item['dicom']`, incrémentée à CHAQUE écriture de ce champ : l'indexation d'un
# fichier n'écrit que si elle n'a pas bougé depuis sa lecture (cf. handleUploadedDicom).
# Champ de premier niveau, hors de `dicom` : non exposé par l'API (liste blanche de l'item).
REVISION_FIELD = "dmfDicomRev"
# Chaque tour perdu l'est au profit d'un autre écrivain qui, lui, a abouti : il en faudrait
# autant d'uploads simultanés dans le MÊME item pour épuiser la borne.
MAX_WRITE_ATTEMPTS = 100


def _parseFile(f):
    """Parse l'en-tête DICOM d'un fichier Girder. None si ce n'est pas du DICOM."""
    try:
        with File().open(f) as fp:
            dataset = pydicom.dcmread(fp, defer_size=1024, stop_before_pixels=True)
    except Exception:
        return None
    return coerce_metadata(dataset)


def _pixelHash(f):
    """Empreinte des pixels d'un fichier Girder ; None si pas de pixels ou échec de lecture
    (l'échec est journalisé mais ne doit jamais bloquer l'indexation)."""
    try:
        with File().open(f) as fp:
            return pixelDataDigest(fp)
    except Exception:
        logger.exception("[dmf] empreinte des pixels impossible pour %s", f.get("name"))
        return None


def _removeUniqueMetadata(dicomMeta, additionalMeta):
    """Intersection des deux dictionnaires (métadonnées communes à tous les fichiers)."""
    return dict(
        {(k, tuple(v) if isinstance(v, list) else v) for k, v in dicomMeta.items()}
        & {(k, tuple(v) if isinstance(v, list) else v) for k, v in additionalMeta.items()}
    )


def _extractFileData(file, dicomMeta, pixelHash=None):
    """Données par fichier conservées pour le tri et l'affichage côté client."""
    return {
        "_id": file["_id"],
        "name": file["name"],
        "dicom": {
            "SeriesNumber": dicomMeta.get("SeriesNumber"),
            "InstanceNumber": dicomMeta.get("InstanceNumber"),
            "SliceLocation": dicomMeta.get("SliceLocation"),
            # Sert au service des pixels : un fichier multi-frame est livré frame par frame
            # (affichage progressif). Le stocker ici évite de re-sonder l'en-tête de chaque
            # coupe à l'ouverture d'un examen.
            "NumberOfFrames": dicomMeta.get("NumberOfFrames"),
            "PixelDataSHA256": pixelHash,
        },
    }


def _mergeFile(dicom, file, fileMetadata, pixelHash):
    """Nouvel état de `item['dicom']` après réception d'un fichier (pur, sans écriture).

    Idempotent : une éventuelle entrée existante de ce fichier (re-upload, ou `dicom_viewer`
    officiel aussi installé) est remplacée, pas dupliquée.
    """
    if dicom is None:
        meta, files = fileMetadata, []
    else:
        meta = _removeUniqueMetadata(dicom.get("meta") or {}, fileMetadata)
        files = [x for x in dicom.get("files", []) if x.get("_id") != file["_id"]]
    files.append(_extractFileData(file, fileMetadata, pixelHash))
    # Tri Python et non `$push`/`$sort` Mongo : celui-ci range les valeurs manquantes EN
    # PREMIER, `sort_key` en dernier.
    files.sort(key=sort_key)
    return {"meta": meta, "files": files}


def _writeDicom(itemId, dicom, expectedRevision):
    """Écrit `item['dicom']` SEULEMENT si sa révision n'a pas bougé depuis la lecture.

    `$set` ciblé : les autres champs de l'item (meta, taille, résumé `dmf`…) ne sont jamais
    réécrits. Renvoie False si un autre écrivain est passé entre-temps (ou item supprimé).
    """
    result = Item().update(
        # `{champ: None}` matche aussi un champ absent : item jamais indexé par cette version.
        {"_id": itemId, REVISION_FIELD: expectedRevision},
        {"$set": {"dicom": dicom}, "$inc": {REVISION_FIELD: 1}},
        multi=False,
    )
    return result.matched_count == 1


def handleUploadedDicom(event):
    """Handler `data.process` : extrait les métadonnées et range/trie l'item.

    Girder déclenche `data.process` dans le thread de la requête d'upload : les fichiers d'une
    même série envoyés EN PARALLÈLE indexent le même item en même temps. Un lire-modifier-
    `Item().save()` perdrait alors des entrées de `files` (et écraserait les autres champs de
    l'item). D'où une écriture conditionnelle sur la révision de `dicom`, rejouée tant qu'un
    autre écrivain passe entre la lecture et l'écriture.
    """
    file = event.info["file"]
    if not file.get("itemId"):
        return
    fileMetadata = _parseFile(file)
    if fileMetadata is None:
        return
    # Hors de la boucle : c'est la partie lente (lecture des pixels), et elle ne dépend pas
    # de l'état de l'item.
    pixelHash = _pixelHash(file)

    for _ in range(MAX_WRITE_ATTEMPTS):
        current = Item().collection.find_one(
            {"_id": file["itemId"]}, {"dicom": True, REVISION_FIELD: True}
        )
        if current is None:
            return
        dicom = _mergeFile(current.get("dicom"), file, fileMetadata, pixelHash)
        if _writeDicom(file["itemId"], dicom, current.get(REVISION_FIELD)):
            return
    # Ne pas faire échouer l'upload (le fichier est bien reçu) ; `POST /dmf/reprocess` réindexe.
    logger.error(
        "[dmf] indexation DICOM abandonnée pour %s (item %s) : %d écritures concurrentes",
        file.get("name"),
        file["itemId"],
        MAX_WRITE_ATTEMPTS,
    )


def rememberDeclaredFrames(itemId, declared):
    """Mémorise `NumberOfFrames` sondé a posteriori ({fileId: n}) dans les entrées EXISTANTES
    de `item.dicom.files`. Mise à jour ciblée de l'entrée (jamais `Item().save()`, qui
    écraserait une indexation concurrente) ; une entrée disparue entre-temps est ignorée."""
    for fileId, frames in declared.items():
        Item().update(
            {"_id": itemId, "dicom.files._id": fileId},
            {
                "$set": {"dicom.files.$.dicom.NumberOfFrames": frames},
                "$inc": {REVISION_FIELD: 1},
            },
            multi=False,
        )


def _knownHashes(item):
    """Empreintes déjà calculées, par fichier. Le contenu d'un fichier ne change que par une
    nouvelle réception (`data.process` la recalcule), elles restent donc valables."""
    return {
        str(x.get("_id")): (x.get("dicom") or {}).get("PixelDataSHA256")
        for x in (item.get("dicom") or {}).get("files", [])
    }


def processItem(item, rehash=False):
    """Retraite TOUS les fichiers d'un item (backfill des items uploadés avant le plugin).

    Reconstruit `item['dicom']` (métadonnées communes + fichiers triés) à partir de zéro.
    Les empreintes des pixels déjà connues sont conservées (sauf `rehash`) : seul un fichier
    sans empreinte est relu en entier. Renvoie True si l'item contient au moins un fichier
    DICOM exploitable.
    """
    known = {} if rehash else _knownHashes(item)
    dicom = None
    for f in Item().childFiles(item):
        meta = _parseFile(f)
        if meta is None:
            continue
        if dicom is None:
            dicom = {"meta": meta, "files": []}
        else:
            dicom["meta"] = _removeUniqueMetadata(dicom["meta"], meta)
        pixelHash = known.get(str(f["_id"])) or _pixelHash(f)
        dicom["files"].append(_extractFileData(f, meta, pixelHash))
    if dicom is None:
        return False
    dicom["files"].sort(key=sort_key)
    item["dicom"] = dicom
    # Écriture inconditionnelle (reconstruction complète), mais ciblée : pas d'`Item().save()`
    # d'un document lu plus tôt, qui écraserait les autres champs. La révision avance, ce qui
    # fait rejouer une indexation `data.process` concurrente sur ce nouvel état.
    Item().update(
        {"_id": item["_id"]},
        {"$set": {"dicom": dicom}, "$inc": {REVISION_FIELD: 1}},
        multi=False,
    )
    return True
