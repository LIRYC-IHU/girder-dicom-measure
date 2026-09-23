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


def handleUploadedDicom(event):
    """Handler `data.process` : extrait les métadonnées et range/trie l'item."""
    file = event.info["file"]
    fileMetadata = _parseFile(file)
    if fileMetadata is None:
        return

    item = Item().load(file["itemId"], force=True)
    if item is None:
        return

    if "dicom" in item:
        item["dicom"]["meta"] = _removeUniqueMetadata(item["dicom"]["meta"], fileMetadata)
    else:
        item["dicom"] = {"meta": fileMetadata, "files": []}

    # Idempotent : on retire une éventuelle entrée existante de ce fichier (re-upload,
    # ou si `dicom_viewer` officiel est aussi installé) avant de ré-ajouter.
    item["dicom"]["files"] = [
        x for x in item["dicom"]["files"] if x.get("_id") != file["_id"]
    ]
    item["dicom"]["files"].append(_extractFileData(file, fileMetadata, _pixelHash(file)))
    item["dicom"]["files"].sort(key=sort_key)

    Item().save(item)


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
    Item().save(item)
    return True
