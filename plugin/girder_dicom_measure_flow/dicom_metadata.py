"""Extraction des métadonnées DICOM à la réception des fichiers + tri des instances.

Réplique le comportement du plugin officiel `dicom_viewer`
(https://github.com/girder/girder/tree/v3.2.15/plugins/dicom_viewer) pour que NOTRE
plugin soit autosuffisant (pas de dépendance à `dicom_viewer`) :

  - à chaque fichier uploadé (event `data.process`), on parse les tags via pydicom
    (en-tête seul, `stop_before_pixels=True`) ;
  - on stocke sur l'item :
        item['dicom'] = {
            'meta':  <métadonnées communes à toutes les IMAGES de l'item>,
            'files': [ {'_id', 'name', 'dicom': {SeriesNumber, InstanceNumber, SliceLocation,
                                                 NumberOfFrames, PixelDataSHA256}}, ... ],
            'nonImageFiles': [ {'_id', 'name', 'dicom': {SOPClassUID, Modality,
                                                         SeriesNumber, InstanceNumber}}, ... ]
        }
    la liste `files` est TRIÉE par (SeriesNumber, InstanceNumber, SliceLocation, name)
    → le client lit cet ordre pour empiler les coupes (item 2 : tri des instances).

`files` ne contient QUE des images (donnée pixel de premier niveau, cf.
`dicom_tags.at_pixel_data`). Les objets DICOM sans pixels — état de présentation (PR), rapport
structuré (SR), sélection d'objets (KO), DICOMDIR… — vont dans `nonImageFiles` : empilés comme
une coupe, ils donnaient une « image » impossible à charger sur laquelle le viewer laissait
pourtant poser une mesure (rattachée à l'UID du PR, donc non calibrable). Ils ne participent
pas non plus à `meta` : l'intersection avec un PR en retirait `ImagerPixelSpacing`. Un item qui
ne contient AUCUNE image garde des `meta` communes à ses objets non-image (patient, étude).

Le champ `dicom` est exposé en lecture via l'API REST (cf. __init__.py).

Écritures de `dicom` ATOMIQUES, jamais par `Item().save()` : les uploads parallèles d'une
série indexent le même item en même temps (cf. `handleUploadedDicom`, `REVISION_FIELD`).

`PixelDataSHA256` = empreinte de la donnée pixel (hors en-tête, cf. `transcode.pixelDataDigest`),
calculée à CHAQUE réception d'un fichier : elle permet de repérer une même image envoyée
plusieurs fois, y compris sous un autre patient ou dans un autre cas (`GET /dmf/pixelhash/:hash`).

"""

import datetime
import logging

import pydicom
from girder.models.file import File
from girder.models.item import Item

from .dicom_tags import at_pixel_data, coerce_metadata, sort_key
from .models import Annotation
from .stack import remapIndex, removedPositions
from .streaming import frameCount
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
    """En-tête DICOM d'un fichier Girder : `(métadonnées, image?)`, ou None si ce n'est pas
    du DICOM. `image` = le fichier porte une donnée pixel de premier niveau."""
    try:
        with File().open(f) as fp:
            dataset = pydicom.dcmread(fp, defer_size=1024, stop_before_pixels=True)
            # Tout de suite : lire une valeur différée déplacerait le flux.
            image = at_pixel_data(fp, dataset)
    except Exception:
        return None
    return coerce_metadata(dataset), image


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


def _extractNonImageData(file, dicomMeta):
    """Entrée d'un objet DICOM SANS pixels : de quoi l'identifier, rien pour l'empiler."""
    return {
        "_id": file["_id"],
        "name": file["name"],
        "dicom": {
            "SOPClassUID": dicomMeta.get("SOPClassUID"),
            "Modality": dicomMeta.get("Modality"),
            "SeriesNumber": dicomMeta.get("SeriesNumber"),
            "InstanceNumber": dicomMeta.get("InstanceNumber"),
        },
    }


def _mergeFile(dicom, file, fileMetadata, pixelHash, image=True):
    """Nouvel état de `item['dicom']` après réception d'un fichier (pur, sans écriture).

    Idempotent : une éventuelle entrée existante de ce fichier (re-upload, ou `dicom_viewer`
    officiel aussi installé) est remplacée, pas dupliquée.

    `meta` = intersection des métadonnées des IMAGES ; tant que l'item n'en a aucune, celle
    des objets non-image. Indépendant de l'ordre d'arrivée : la première image remplace des
    `meta` issues d'objets non-image, et un objet non-image ne touche plus `meta` dès qu'une
    image est indexée.
    """
    dicom = dicom or {}
    files = [x for x in dicom.get("files", []) if x.get("_id") != file["_id"]]
    others = [x for x in dicom.get("nonImageFiles", []) if x.get("_id") != file["_id"]]
    meta = dicom.get("meta") or {}
    if image:
        meta = _removeUniqueMetadata(meta, fileMetadata) if files else fileMetadata
        files.append(_extractFileData(file, fileMetadata, pixelHash))
        # Tri Python et non `$push`/`$sort` Mongo : celui-ci range les valeurs manquantes EN
        # PREMIER, `sort_key` en dernier.
        files.sort(key=sort_key)
    else:
        if not files:
            meta = _removeUniqueMetadata(meta, fileMetadata) if others else fileMetadata
        others.append(_extractNonImageData(file, fileMetadata))
        others.sort(key=sort_key)
    return {"meta": meta, "files": files, "nonImageFiles": others}


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
    parsed = _parseFile(file)
    if parsed is None:
        return
    fileMetadata, image = parsed
    # Hors de la boucle : c'est la partie lente (lecture des pixels), et elle ne dépend pas
    # de l'état de l'item.
    pixelHash = _pixelHash(file) if image else None

    for _ in range(MAX_WRITE_ATTEMPTS):
        current = Item().collection.find_one(
            {"_id": file["itemId"]}, {"dicom": True, REVISION_FIELD: True}
        )
        if current is None:
            return
        dicom = _mergeFile(current.get("dicom"), file, fileMetadata, pixelHash, image)
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


def _buildDicom(item, known, hashMissing=True):
    """`item['dicom']` reconstruit à partir de zéro depuis les fichiers de l'item (sans
    écriture), ou None si l'item ne contient aucun DICOM. Même fusion que la réception
    (`_mergeFile`) : le résultat ne dépend pas de l'ordre de parcours des fichiers."""
    dicom = None
    for f in Item().childFiles(item):
        parsed = _parseFile(f)
        if parsed is None:
            continue
        meta, image = parsed
        pixelHash = None
        if image:
            pixelHash = known.get(str(f["_id"]))
            if pixelHash is None and hashMissing:
                pixelHash = _pixelHash(f)
        dicom = _mergeFile(dicom, f, meta, pixelHash, image)
    return dicom


def _stackSize(entry, entryCount):
    """Nombre de positions qu'occupait une entrée de `files` dans le stack du viewer : celui
    que `GET /dmf/item/:id/files` annonce (`frames`), avec le même raccourci (seul un
    `NumberOfFrames` déclaré > 1 justifie de relire l'en-tête). Un fichier SEUL est développé
    côté client quoi qu'il arrive : il n'a rien avant lui, sa taille n'importe pas."""
    if entryCount < 2:
        return 1
    declared = (entry.get("dicom") or {}).get("NumberOfFrames")
    if declared is not None and int(declared or 1) <= 1:
        return 1
    file = File().load(entry["_id"], force=True)
    return frameCount(file) if file is not None else 1


def _planStackChange(item, oldFiles, dicom):
    """Renumérotation des mesures de l'item quand des objets non-image sortent du stack.

    Ne concerne que les items indexés avant 0.6.0, dont `files` contenait encore ces objets :
    on retrouve leurs anciennes positions, puis le nouveau `frameIndex` de chaque mesure. Un
    item déjà retraité n'a plus rien à retirer → rien à faire (relancer est sans effet).
    Renvoie None si le stack ne change pas.
    """
    nonImage = {x["_id"]: x for x in dicom.get("nonImageFiles", [])}
    removed = [x for x in oldFiles if x.get("_id") in nonImage]
    if not removed:
        return None
    layout = [(x["_id"], _stackSize(x, len(oldFiles))) for x in oldFiles]
    positions, oldLength = removedPositions(layout, {x["_id"] for x in removed})
    removedIds = sorted(str(x["_id"]) for x in removed)
    changes = []
    for annot in Annotation().listForItem(item["_id"]):
        # Déjà renumérotée pour ces fichiers (passage précédent interrompu avant l'écriture
        # de `dicom`) : ne pas décaler deux fois.
        done = (annot.get("stackMigration") or {}).get("removedFileIds") or []
        if set(removedIds) <= set(done):
            continue
        new, orphan = remapIndex(annot.get("frameIndex"), positions, oldLength)
        if new == annot.get("frameIndex") and not orphan:
            continue
        changes.append({"annotation": annot, "from": annot.get("frameIndex"), "to": new,
                        "orphan": orphan})
    return {
        "removedFiles": [
            {"id": str(x["_id"]), "name": x.get("name"),
             "SOPClassUID": nonImage[x["_id"]]["dicom"].get("SOPClassUID"),
             "Modality": nonImage[x["_id"]]["dicom"].get("Modality"),
             "oldPosition": p}
            for x, p in zip(removed, _firstPositions(layout, removed))
        ],
        "removedFileIds": removedIds,
        "changes": changes,
    }


def _firstPositions(layout, entries):
    """Ancienne position (première) de chacune des `entries` dans le stack décrit par `layout`."""
    starts = {}
    offset = 0
    for fileId, size in layout:
        starts[fileId] = offset
        offset += max(int(size or 1), 1)
    return [starts[x["_id"]] for x in entries]


def _applyStackChange(plan):
    """Écrit le nouveau `frameIndex` des mesures, avec sa provenance (`stackMigration`).

    `Annotation().save` : recalcule aussi le résumé `item.dmf`. Le `sopInstanceUID` n'est PAS
    modifié — y compris pour une mesure orpheline, qui garde celui de l'objet non-image : la
    corriger relève de la relecture, pas d'une migration.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    for change in plan["changes"]:
        annot = change["annotation"]
        annot["frameIndex"] = change["to"]
        annot["stackMigration"] = {
            "reason": "non-image-files-removed",
            "at": now,
            "fromFrameIndex": change["from"],
            "orphan": change["orphan"],
            "removedFileIds": plan["removedFileIds"],
        }
        Annotation().save(annot)


def _report(item, dicom, plan):
    report = {
        "itemId": str(item["_id"]),
        "itemName": item.get("name"),
        "nonImageFiles": len(dicom.get("nonImageFiles", [])),
    }
    if plan is not None:
        report["removedFromStack"] = plan["removedFiles"]
        report["annotations"] = [
            {"key": c["annotation"].get("key"), "type": c["annotation"].get("type"),
             "creatorLogin": c["annotation"].get("creatorLogin"),
             "sopInstanceUID": c["annotation"].get("sopInstanceUID"),
             "fromFrameIndex": c["from"], "toFrameIndex": c["to"], "orphan": c["orphan"]}
            for c in plan["changes"]
        ]
    return report


def processItem(item, rehash=False, dryRun=False):
    """Retraite TOUS les fichiers d'un item (backfill des items uploadés avant le plugin, ou
    indexés par une version antérieure).

    Reconstruit `item['dicom']` (métadonnées communes + fichiers triés + objets non-image) à
    partir de zéro. Les empreintes des pixels déjà connues sont conservées (sauf `rehash`) :
    seul un fichier sans empreinte est relu en entier.

    Si des objets non-image sortent du stack (item indexé avant 0.6.0), les `frameIndex` des
    mesures de l'item sont renumérotés AVANT l'écriture de `dicom` (cf. `_planStackChange`) :
    une interruption entre les deux est rattrapée au passage suivant sans double décalage.

    `dryRun` : rien n'est écrit et aucune empreinte manquante n'est calculée ; le rapport dit
    ce qui changerait. Renvoie None si l'item ne contient aucun DICOM, sinon le rapport.
    """
    known = {} if rehash else _knownHashes(item)
    dicom = _buildDicom(item, known, hashMissing=not dryRun)
    if dicom is None:
        return None
    oldFiles = (item.get("dicom") or {}).get("files") or []
    plan = _planStackChange(item, oldFiles, dicom)
    report = _report(item, dicom, plan)
    if dryRun:
        return report
    if plan is not None:
        _applyStackChange(plan)
    item["dicom"] = dicom
    # Écriture inconditionnelle (reconstruction complète), mais ciblée : pas d'`Item().save()`
    # d'un document lu plus tôt, qui écraserait les autres champs. La révision avance, ce qui
    # fait rejouer une indexation `data.process` concurrente sur ce nouvel état.
    Item().update(
        {"_id": item["_id"]},
        {"$set": {"dicom": dicom}, "$inc": {REVISION_FIELD: 1}},
        multi=False,
    )
    return report
