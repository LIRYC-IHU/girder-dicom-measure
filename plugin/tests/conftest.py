"""Permet d'importer les modules PURS du plugin (dicom_tags) sans déclencher l'__init__
du package (qui importe Girder). On ajoute le dossier du package au sys.path.

Le dossier parent est ajouté aussi : les tests d'intégration importent le PACKAGE
(`girder_dicom_measure_flow.dicom_metadata`, imports relatifs) sans exiger qu'il soit installé.

Fixture `girder` des tests d'intégration (Girder + MongoDB réels) : base jetable, PARTAGÉE par
toute la session — les modèles Girder ouvrent leur connexion une seule fois, à la première
instanciation ; une base par module laisserait le second écrire dans celle du premier.
"""

import os
import sys
import uuid

import pytest

_here = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(_here, ".."))
sys.path.insert(0, os.path.join(_here, "..", "girder_dicom_measure_flow"))

MONGO_URI = os.environ.get("DMF_TEST_MONGO_URI", "mongodb://localhost:27017")


@pytest.fixture(scope="session")
def girder(tmp_path_factory):
    pymongo = pytest.importorskip("pymongo")  # dépendance de girder
    pytest.importorskip("girder")
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
