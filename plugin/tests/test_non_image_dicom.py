"""Intégration (Girder + MongoDB réels) : objets DICOM sans pixels à côté des images.

Cas DEFINE-PFA (Rhéna, cas 8-7…8-16) : l'item contient `0001.dcm` = état de présentation
(PR, Grayscale Softcopy Presentation State, sans PixelData) et `0002.dcm` = la boucle XA.
Empilé comme une coupe, le PR donnait une « image » impossible à charger, sur laquelle le
viewer laissait poser une mesure (UID du PR → non calibrable) ; il retirait aussi
`ImagerPixelSpacing` des métadonnées communes de l'item.

Ignorés sans girder / MongoDB (cf. fixture `girder` de conftest.py).
"""

import importlib.util
import inspect
import io

import pytest

if importlib.util.find_spec("girder") is None:
    pytest.skip("girder non installé", allow_module_level=True)

from pydicom.dataset import Dataset, FileMetaDataset  # noqa: E402
from pydicom.uid import ExplicitVRLittleEndian, generate_uid  # noqa: E402

XA_FRAMES = 3
PR_SOP_CLASS = "1.2.840.10008.5.1.4.1.1.11.1"
XA_SOP_CLASS = "1.2.840.10008.5.1.4.1.1.12.1"


def _part10(ds, sopClass):
    ds.file_meta = FileMetaDataset()
    ds.file_meta.MediaStorageSOPClassUID = sopClass
    ds.file_meta.MediaStorageSOPInstanceUID = generate_uid()
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.SOPClassUID = sopClass
    ds.SOPInstanceUID = ds.file_meta.MediaStorageSOPInstanceUID
    ds.PatientID = "ANON"
    ds.StudyInstanceUID = "1.2.3.4"
    ds.SeriesNumber = 1
    buf = io.BytesIO()
    ds.save_as(buf, enforce_file_format=True)
    return buf.getvalue()


def _presentationState():
    ds = Dataset()
    ds.Modality = "PR"
    ds.InstanceNumber = 1
    ds.ContentLabel = "PS"
    return _part10(ds, PR_SOP_CLASS)


def _cineLoop():
    """Boucle XA non compressée multi-frame → livrée frame par frame (`frames` = 3)."""
    ds = Dataset()
    ds.Modality = "XA"
    ds.InstanceNumber = 2
    ds.ImagerPixelSpacing = [0.154, 0.154]
    ds.DistanceSourceToDetector = 1100
    ds.NumberOfFrames = XA_FRAMES
    ds.Rows = ds.Columns = 8
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated = ds.BitsStored = 8
    ds.HighBit = 7
    ds.PixelRepresentation = 0
    ds.PixelData = bytes(range(8 * 8)) * XA_FRAMES
    return _part10(ds, XA_SOP_CLASS)


def _newItem(girder, name):
    from girder.models.item import Item

    return Item().createItem(name, creator=girder["user"], folder=girder["folder"])


def _upload(girder, item, name, data):
    from girder.models.upload import Upload

    return Upload().uploadFromFile(io.BytesIO(data), len(data), name, "item", item, girder["user"])


def _reload(item):
    from girder.models.item import Item

    return Item().load(item["_id"], force=True)


def _getFiles(item):
    """`GET /dmf/item/:id/files`, sans le serveur HTTP (décorateurs d'accès/description ôtés)."""
    from girder_dicom_measure_flow.rest import DmfResource

    return inspect.unwrap(DmfResource.getFiles)(DmfResource(), item)


def _rhenaItem(girder, name, order=("0001.dcm", "0002.dcm")):
    item = _newItem(girder, name)
    payload = {"0001.dcm": _presentationState(), "0002.dcm": _cineLoop()}
    files = {n: _upload(girder, item, n, payload[n]) for n in order}
    return item, files["0001.dcm"], files["0002.dcm"]


@pytest.mark.parametrize("order", [("0001.dcm", "0002.dcm"), ("0002.dcm", "0001.dcm")])
def test_presentation_state_stays_out_of_the_stack(girder, order):
    item, pr, xa = _rhenaItem(girder, "rhena-%s" % order[0], order)

    dicom = _reload(item)["dicom"]
    assert [f["_id"] for f in dicom["files"]] == [xa["_id"]]
    assert [f["_id"] for f in dicom["nonImageFiles"]] == [pr["_id"]]
    assert dicom["nonImageFiles"][0]["dicom"]["SOPClassUID"] == PR_SOP_CLASS
    assert dicom["nonImageFiles"][0]["dicom"]["Modality"] == "PR"
    # Métadonnées communes calculées sur les IMAGES : le PR n'en retire plus l'échelle.
    assert dicom["meta"]["ImagerPixelSpacing"] == [0.154, 0.154]
    assert dicom["meta"]["Modality"] == "XA"
    assert dicom["files"][0]["dicom"]["PixelDataSHA256"]

    assert _getFiles(_reload(item)) == [
        {"id": str(xa["_id"]), "name": "0002.dcm", "frames": XA_FRAMES}
    ]


def test_item_without_any_image_exposes_no_slice(girder):
    item = _newItem(girder, "pr-only")
    pr = _upload(girder, item, "0001.dcm", _presentationState())

    dicom = _reload(item)["dicom"]
    assert dicom["files"] == []
    assert [f["_id"] for f in dicom["nonImageFiles"]] == [pr["_id"]]
    assert dicom["meta"]["PatientID"] == "ANON"  # repli : méta des objets non-image
    # Indexé sans image ≠ jamais indexé : pas de repli sur tous les fichiers de l'item.
    assert _getFiles(_reload(item)) == []


def test_unindexed_item_still_falls_back_to_its_files(girder):
    from girder.models.item import Item

    item, pr, xa = _rhenaItem(girder, "unindexed")
    Item().update({"_id": item["_id"]}, {"$unset": {"dicom": ""}})
    assert {f["name"] for f in _getFiles(_reload(item))} == {"0001.dcm", "0002.dcm"}


# --- Items indexés avant 0.6.0 : retraitement + renumérotation des mesures --------------------


def _makeLegacy(item, pr, xa):
    """Remet `item.dicom` dans l'état d'une indexation < 0.6.0 : PR empilé en tête, pas de
    `nonImageFiles`, et métadonnées communes privées de l'échelle."""
    from girder.models.item import Item

    dicom = _reload(item)["dicom"]
    legacyPr = {"_id": pr["_id"], "name": pr["name"],
                "dicom": {"SeriesNumber": 1, "InstanceNumber": 1, "SliceLocation": None,
                          "NumberOfFrames": None, "PixelDataSHA256": None}}
    meta = {k: v for k, v in dicom["meta"].items()
            if k in ("PatientID", "StudyInstanceUID", "SeriesNumber")}
    Item().update({"_id": item["_id"]},
                  {"$set": {"dicom": {"meta": meta, "files": [legacyPr] + dicom["files"]}}})
    return _reload(item)


def _annotate(girder, item, key, frameIndex, sop):
    from girder_dicom_measure_flow.models import Annotation

    measurement = {"id": key, "type": "distance", "frameIndex": frameIndex, "sopInstanceUID": sop,
                   "geometry": {"start": {"x": 1, "y": 1}, "end": {"x": 1, "y": 5}},
                   "values": {"lengthPx": 4, "lengthMm": None, "spacingSource": "none"}}
    return Annotation().save(Annotation().fromMeasurement(measurement, item, girder["user"]))


def _frameIndices(item):
    from girder_dicom_measure_flow.models import Annotation

    return {a["key"]: a["frameIndex"] for a in Annotation().listForItem(item["_id"])}


def _legacyRhenaWithAnnotations(girder, name):
    item, pr, xa = _rhenaItem(girder, name)
    legacy = _makeLegacy(item, pr, xa)
    # Ancien stack : [PR, xa#0, xa#1, xa#2] → positions 0..3.
    _annotate(girder, item, name + "-on-pr", 0, "uid-of-the-pr")
    _annotate(girder, item, name + "-first", 1, "uid-of-the-loop")
    _annotate(girder, item, name + "-last", 3, "uid-of-the-loop")
    return legacy, pr, xa


def test_dry_run_reports_without_writing(girder):
    from girder_dicom_measure_flow.dicom_metadata import processItem

    legacy, pr, xa = _legacyRhenaWithAnnotations(girder, "dry")
    report = processItem(legacy, dryRun=True)

    assert report["removedFromStack"] == [{
        "id": str(pr["_id"]), "name": "0001.dcm", "SOPClassUID": PR_SOP_CLASS,
        "Modality": "PR", "oldPosition": 0,
    }]
    moves = {a["key"]: (a["fromFrameIndex"], a["toFrameIndex"], a["orphan"])
             for a in report["annotations"]}
    assert moves == {"dry-on-pr": (0, 0, True), "dry-first": (1, 0, False),
                     "dry-last": (3, 2, False)}
    # Rien n'a bougé.
    assert _reload(legacy)["dicom"] == legacy["dicom"]
    assert _frameIndices(legacy) == {"dry-on-pr": 0, "dry-first": 1, "dry-last": 3}


def test_reprocess_moves_the_presentation_state_out_and_renumbers_annotations(girder):
    from girder_dicom_measure_flow.dicom_metadata import processItem
    from girder_dicom_measure_flow.models import Annotation

    legacy, pr, xa = _legacyRhenaWithAnnotations(girder, "mig")
    processItem(legacy)

    reloaded = _reload(legacy)
    assert [f["_id"] for f in reloaded["dicom"]["files"]] == [xa["_id"]]
    assert [f["_id"] for f in reloaded["dicom"]["nonImageFiles"]] == [pr["_id"]]
    assert reloaded["dicom"]["meta"]["ImagerPixelSpacing"] == [0.154, 0.154]
    assert _frameIndices(legacy) == {"mig-on-pr": 0, "mig-first": 0, "mig-last": 2}

    onPr = Annotation().findOne({"key": "mig-on-pr"})
    assert onPr["stackMigration"]["orphan"] is True
    assert onPr["stackMigration"]["fromFrameIndex"] == 0
    assert onPr["stackMigration"]["removedFileIds"] == [str(pr["_id"])]
    assert onPr["sopInstanceUID"] == "uid-of-the-pr"  # non corrigé : relève de la relecture
    assert Annotation().toMeasurement(onPr)["stackMigration"]["orphan"] is True
    # Le résumé `item.dmf` suit la collection.
    assert sorted(m["frameIndex"] for m in reloaded["dmf"]["measurements"]) == [0, 0, 2]

    # Relancer ne décale rien de plus.
    second = processItem(reloaded)
    assert "removedFromStack" not in second
    assert _frameIndices(legacy) == {"mig-on-pr": 0, "mig-first": 0, "mig-last": 2}


def test_interrupted_reprocess_does_not_shift_twice(girder):
    """Mesures renumérotées, puis arrêt AVANT l'écriture de `dicom` : le passage suivant
    retrouve l'ancien stack mais ne redécale pas les mesures déjà traitées."""
    from girder_dicom_measure_flow.dicom_metadata import processItem

    legacy, pr, xa = _legacyRhenaWithAnnotations(girder, "int")
    processItem(legacy)
    stale = _makeLegacy(legacy, pr, xa)  # l'écriture de `dicom` n'aurait pas eu lieu

    report = processItem(stale)
    assert report["annotations"] == []
    assert _frameIndices(legacy) == {"int-on-pr": 0, "int-first": 0, "int-last": 2}
    assert [f["_id"] for f in _reload(legacy)["dicom"]["files"]] == [xa["_id"]]


def test_new_upload_into_a_legacy_item_does_not_renumber(girder):
    """Seul `reprocess` renumérote : une réception ordinaire classe le NOUVEAU fichier et
    laisse l'ancien stack tel quel (sinon les mesures se décaleraient sans trace)."""
    legacy, pr, xa = _legacyRhenaWithAnnotations(girder, "upl")
    _upload(girder, legacy, "0003.dcm", _presentationState())

    dicom = _reload(legacy)["dicom"]
    assert [f["_id"] for f in dicom["files"]] == [pr["_id"], xa["_id"]]
    assert [f["name"] for f in dicom["nonImageFiles"]] == ["0003.dcm"]
    assert _frameIndices(legacy) == {"upl-on-pr": 0, "upl-first": 1, "upl-last": 3}
