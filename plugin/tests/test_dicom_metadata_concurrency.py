"""Intégration (Girder + MongoDB réels) : réceptions concurrentes dans un même item.

`data.process` est déclenché de façon SYNCHRONE dans le thread de la requête d'upload : deux
fichiers d'une même série envoyés en parallèle exécutent `handleUploadedDicom` en même temps
sur le même item. L'indexation doit être atomique — aucune entrée de `item.dicom.files`
perdue, aucune modification concurrente des autres champs de l'item écrasée.

Nécessite `girder` installé et un MongoDB joignable (`DMF_TEST_MONGO_URI`, défaut
`mongodb://localhost:27017`) ; sinon les tests sont ignorés. Chaque exécution travaille dans
une base jetable (`dmf_test_<aléa>`), supprimée à la fin.
"""

import importlib.util
import io
import os
import threading
import uuid

import pytest

if importlib.util.find_spec("girder") is None:
    pytest.skip("girder non installé", allow_module_level=True)

import pymongo  # noqa: E402  (dépendance de girder)
from pydicom.dataset import Dataset, FileMetaDataset  # noqa: E402
from pydicom.uid import ExplicitVRLittleEndian, generate_uid  # noqa: E402

MONGO_URI = os.environ.get("DMF_TEST_MONGO_URI", "mongodb://localhost:27017")
STUDY_UID = generate_uid()
SERIES_UID = generate_uid()


@pytest.fixture(scope="module")
def girder(tmp_path_factory):
    client = pymongo.MongoClient(MONGO_URI, serverSelectionTimeoutMS=1000)
    try:
        client.admin.command("ping")
    except pymongo.errors.PyMongoError:
        pytest.skip("MongoDB injoignable sur %s" % MONGO_URI)

    dbName = "dmf_test_%s" % uuid.uuid4().hex[:12]
    # Après l'import de girder (qui charge sa config) mais avant tout accès à un modèle :
    # la connexion est ouverte à la première instanciation, avec l'URI de la config cherrypy.
    import cherrypy
    from girder import events

    cherrypy.config["database"]["uri"] = "%s/%s" % (MONGO_URI.rstrip("/"), dbName)

    from girder.models.assetstore import Assetstore
    from girder.models.folder import Folder
    from girder.models.user import User

    from girder_dicom_measure_flow.dicom_metadata import handleUploadedDicom

    Assetstore().createFilesystemAssetstore("test", str(tmp_path_factory.mktemp("assetstore")))
    user = User().createUser("admin", "password", "Admin", "Test", "admin@example.com")
    folder = Folder().createFolder(user, "data", parentType="user", creator=user)
    events.bind("data.process", "dmf_test", handleUploadedDicom)
    try:
        yield {"user": user, "folder": folder}
    finally:
        events.unbind("data.process", "dmf_test")
        client.drop_database(dbName)


def _dicom(instance):
    """Une coupe d'une série commune : seuls les tags d'instance diffèrent."""
    ds = Dataset()
    ds.file_meta = FileMetaDataset()
    ds.file_meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.2"
    ds.file_meta.MediaStorageSOPInstanceUID = generate_uid()
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.SOPClassUID = ds.file_meta.MediaStorageSOPClassUID
    ds.SOPInstanceUID = ds.file_meta.MediaStorageSOPInstanceUID
    ds.StudyInstanceUID = STUDY_UID
    ds.SeriesInstanceUID = SERIES_UID
    ds.PatientID = "ANON"
    ds.Modality = "CT"
    ds.SeriesNumber = 1
    ds.InstanceNumber = instance
    ds.SliceLocation = float(instance) * 2.5
    ds.Rows = ds.Columns = 8
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated = ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 0
    ds.PixelData = bytes([instance % 256]) * (8 * 8 * 2)
    buf = io.BytesIO()
    ds.save_as(buf, enforce_file_format=True)
    return buf.getvalue()


def _newItem(girder, name):
    from girder.models.item import Item

    return Item().createItem(name, creator=girder["user"], folder=girder["folder"])


def _upload(girder, item, instance):
    from girder.models.upload import Upload

    data = _dicom(instance)
    return Upload().uploadFromFile(
        io.BytesIO(data), len(data), "IM%04d.dcm" % instance, "item", item, girder["user"]
    )


def _reload(item):
    from girder.models.item import Item

    return Item().load(item["_id"], force=True)


def test_concurrent_uploads_into_one_item_keep_every_file(girder):
    count = 24
    item = _newItem(girder, "series")
    # Ordre d'arrivée volontairement inverse de l'ordre des coupes : le tri doit tenir.
    instances = list(range(count, 0, -1))
    barrier = threading.Barrier(count)
    errors = []
    uploaded = {}

    def worker(instance):
        try:
            barrier.wait()
            uploaded[instance] = _upload(girder, item, instance)
        except Exception as exc:  # remonté dans le thread principal
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in instances]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors

    dicom = _reload(item)["dicom"]
    files = dicom["files"]
    assert sorted(str(f["_id"]) for f in files) == sorted(
        str(f["_id"]) for f in uploaded.values()
    )
    assert [f["dicom"]["InstanceNumber"] for f in files] == list(range(1, count + 1))
    assert all(f["dicom"]["PixelDataSHA256"] for f in files)
    # Métadonnées communes = intersection : les tags d'instance en sont exclus.
    assert dicom["meta"]["SeriesInstanceUID"] == SERIES_UID
    assert dicom["meta"]["PatientID"] == "ANON"
    assert "InstanceNumber" not in dicom["meta"]
    assert "SOPInstanceUID" not in dicom["meta"]


def test_reupload_of_the_same_file_replaces_its_entry(girder):
    from girder.models.file import File

    from girder_dicom_measure_flow.dicom_metadata import handleUploadedDicom

    item = _newItem(girder, "reupload")
    first = _upload(girder, item, 2)
    _upload(girder, item, 1)
    # Nouvelle réception du même fichier (ou `dicom_viewer` officiel aussi installé).
    handleUploadedDicom(type("Event", (), {"info": {"file": File().load(first["_id"], force=True)}}))

    files = _reload(item)["dicom"]["files"]
    assert [f["dicom"]["InstanceNumber"] for f in files] == [1, 2]


def test_indexing_does_not_clobber_a_concurrent_item_update(girder, monkeypatch):
    """Une écriture d'un autre champ de l'item PENDANT l'indexation (ici, au moment où
    l'empreinte des pixels est calculée) doit survivre."""
    from girder.models.item import Item

    from girder_dicom_measure_flow import dicom_metadata

    item = _newItem(girder, "concurrent-meta")
    original = dicom_metadata._pixelHash

    def hashThenConcurrentWrite(f):
        Item().update({"_id": item["_id"]}, {"$set": {"meta.reviewed": True}})
        return original(f)

    monkeypatch.setattr(dicom_metadata, "_pixelHash", hashThenConcurrentWrite)
    _upload(girder, item, 1)

    reloaded = _reload(item)
    assert reloaded["meta"].get("reviewed") is True
    assert [f["dicom"]["InstanceNumber"] for f in reloaded["dicom"]["files"]] == [1]
    # La taille de l'item (incrémentée atomiquement par Girder à la réception) est intacte.
    assert reloaded["size"] == sum(f["size"] for f in Item().childFiles(reloaded))


def test_a_write_lost_to_a_concurrent_indexing_is_replayed(girder, monkeypatch):
    """Interleaving forcé : un autre fichier est indexé ENTRE la lecture de l'item et
    l'écriture. L'écriture conditionnelle échoue, est rejouée, et les deux entrées restent."""
    from girder_dicom_measure_flow import dicom_metadata

    item = _newItem(girder, "interleaved")
    original = dicom_metadata._mergeFile
    interleaved = []

    def mergeAfterAConcurrentUpload(*args):
        if not interleaved:
            interleaved.append(True)
            _upload(girder, item, 2)  # indexation complète, révision avancée
        return original(*args)

    monkeypatch.setattr(dicom_metadata, "_mergeFile", mergeAfterAConcurrentUpload)
    _upload(girder, item, 1)

    reloaded = _reload(item)
    assert [f["dicom"]["InstanceNumber"] for f in reloaded["dicom"]["files"]] == [1, 2]
    # Écritures réussies seulement : 2 indexations (la tentative perdue ne compte pas).
    assert reloaded[dicom_metadata.REVISION_FIELD] == 2


def test_process_item_rebuilds_dicom_without_touching_other_fields(girder):
    from girder.models.item import Item

    from girder_dicom_measure_flow.dicom_metadata import processItem

    item = _newItem(girder, "backfill")
    for instance in (3, 1, 2):
        _upload(girder, item, instance)
    stale = _reload(item)
    # Après la lecture par le backfill : index abîmé + écriture d'un autre champ.
    Item().update({"_id": item["_id"]}, {"$set": {"dicom.files": [], "meta.reviewed": True}})

    assert processItem(stale) is True

    reloaded = _reload(item)
    assert [f["dicom"]["InstanceNumber"] for f in reloaded["dicom"]["files"]] == [1, 2, 3]
    assert reloaded["meta"].get("reviewed") is True
    assert reloaded["dicom"] == stale["dicom"]


def test_remembered_frame_counts_do_not_drop_entries(girder):
    from girder.models.item import Item

    from girder_dicom_measure_flow.dicom_metadata import rememberDeclaredFrames

    item = _newItem(girder, "frames")
    first = _upload(girder, item, 1)
    # Entrée indexée par une version antérieure : pas de NumberOfFrames.
    Item().update({"_id": item["_id"]}, {"$unset": {"dicom.files.0.dicom.NumberOfFrames": ""}})
    stale = _reload(item)
    _upload(girder, item, 2)  # indexé APRÈS la lecture de l'item par la route

    rememberDeclaredFrames(stale["_id"], {first["_id"]: 7})

    files = _reload(item)["dicom"]["files"]
    assert [f["dicom"]["InstanceNumber"] for f in files] == [1, 2]
    assert files[0]["dicom"]["NumberOfFrames"] == 7
