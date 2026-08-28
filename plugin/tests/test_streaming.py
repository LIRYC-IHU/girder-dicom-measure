"""Tests de la couche de SERVICE des pixels (`streaming.serveFile`).

Cette couche n'était couverte par rien, et c'est elle qui a laissé passer le bug : le
transcodeur fonctionnait, mais le fichier partait quand même non compressé parce que la
lecture de la source échouait. On simule donc le strict nécessaire de Girder (modèles
`File`/`Setting` + en-têtes de réponse) pour tester les décisions de bout en bout :
que sert-on, avec quels en-têtes, et que mémorise-t-on dans le cache.

Le faux `File.open()` reproduit le comportement qui a mordu : `FileHandle.read()` REFUSE
toute lecture unique plus grande que `core.filehandle_max_size` (16 Mo par défaut).
"""

import importlib
import io
import os
import sys
import types

import pytest

from test_transcode import _bytes, _dataset  # jeux d'essai DICOM synthétiques

PACKAGE = "dmf_under_test"
SOURCES = os.path.join(os.path.dirname(__file__), "..", "girder_dicom_measure_flow")

# Plafond du faux FileHandle, volontairement minuscule : les datasets de test font quelques
# dizaines de Ko, il faut donc plusieurs blocs pour lire l'un d'eux.
FAKE_MAX_READ = 4096


class _FakeHandle:
    def __init__(self, data):
        self._buf = io.BytesIO(data)

    def read(self, size=None):
        if size is None or size < 0 or size > FAKE_MAX_READ:
            raise RuntimeError("Read exceeds maximum allowed size.")
        return self._buf.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeFileModel:
    """Modèle `File` de Girder, réduit à ce que `streaming` utilise."""

    contents = {}
    downloaded = []

    def open(self, doc):
        return _FakeHandle(self.contents[doc["_id"]])

    def download(self, doc):
        _FakeFileModel.downloaded.append(doc["_id"])
        data = self.contents[doc["_id"]]

        def stream():
            yield data

        return stream


@pytest.fixture
def streaming(tmp_path, monkeypatch):
    """Importe `streaming` avec un faux paquet `girder`, et un cache dans `tmp_path`.

    Le module utilise des imports RELATIFS : on le charge donc sous un paquet synthétique
    dont le `__path__` pointe sur les sources, plutôt que via le vrai `__init__` du plugin
    (qui, lui, tire toute la machinerie de plugin Girder).
    """
    headers = {}
    settings = {
        "dmf.compression": "lossless",
        "dmf.lossy_ratio": 10.0,
        "dmf.compression_max_mb": 0,
        "dmf.cache_dir": str(tmp_path),
        "dmf.cache_max_mb": 0,
    }

    def _module(name, **attrs):
        mod = types.ModuleType(name)
        mod.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, mod)
        return mod

    class _FakeSetting:
        def get(self, key):
            return settings[key]

    _noopDecorator = lambda *a, **k: (lambda fn: fn)  # noqa: E731

    _module("girder")
    _module("girder.api")
    _module(
        "girder.api.rest",
        setResponseHeader=lambda name, value: headers.__setitem__(name, value),
        setContentDisposition=lambda *a, **k: None,
    )
    _module("girder.models")
    _module("girder.models.file", File=_FakeFileModel)
    _module("girder.models.setting", Setting=_FakeSetting)
    _module("girder.exceptions", ValidationException=type("ValidationException", (Exception,), {}))
    _module("girder.utility")
    _module(
        "girder.utility.setting_utilities",
        default=_noopDecorator,
        validator=_noopDecorator,
    )

    # Paquet synthétique : `from .settings import …` doit résoudre dans les sources du plugin.
    pkg = types.ModuleType(PACKAGE)
    pkg.__path__ = [SOURCES]
    monkeypatch.setitem(sys.modules, PACKAGE, pkg)
    for name in [n for n in sys.modules if n.startswith(PACKAGE + ".")]:
        monkeypatch.delitem(sys.modules, name)

    mod = importlib.import_module(PACKAGE + ".streaming")
    monkeypatch.setattr(mod, "_READ_CHUNK", FAKE_MAX_READ)
    _FakeFileModel.contents = {}
    _FakeFileModel.downloaded = []
    mod._headers = headers
    mod._settings = settings
    return mod


def _register(source, fileId="f1"):
    _FakeFileModel.contents[fileId] = source
    return {"_id": fileId, "name": "%s.dcm" % fileId, "size": len(source), "sha512": "abc"}


def _served(stream):
    return b"".join(stream())


def test_serves_transcoded_bytes_not_the_original(streaming):
    """Le cœur du sujet : ce qui part sur le réseau doit être PLUS PETIT que la source."""
    source = _bytes(_dataset(frames=8))
    doc = _register(source)

    data = _served(streaming.serveFile(doc))

    assert len(data) < len(source)
    assert streaming._headers["X-Dmf-Transfer"].startswith("transcoded")
    assert streaming._headers["Content-Length"] == str(len(data))
    assert _FakeFileModel.downloaded == []  # jamais servi tel quel


def test_source_larger_than_one_read_is_still_transcoded(streaming):
    """Régression : un `read()` unique plus grand que le plafond Girder échouait, et le
    fichier repartait non compressé — le cas de TOUTES les boucles de scopie."""
    source = _bytes(_dataset(frames=8))
    assert len(source) > FAKE_MAX_READ  # sinon le test ne prouve rien
    assert len(_served(streaming.serveFile(_register(source)))) < len(source)


def test_already_compressed_file_passes_through_with_a_reason(streaming):
    import pydicom
    from pydicom.uid import RLELossless

    ds = pydicom.dcmread(io.BytesIO(_bytes(_dataset(frames=2))))
    ds.compress(RLELossless, generate_instance_uid=False)
    source = _bytes(ds)

    assert _served(streaming.serveFile(_register(source))) == source
    assert streaming._headers["X-Dmf-Transfer"] == "passthrough; reason=already-compressed"


def test_disabled_mode_passes_through(streaming):
    streaming._settings["dmf.compression"] = "none"
    source = _bytes(_dataset(frames=2))

    assert _served(streaming.serveFile(_register(source))) == source
    assert streaming._headers["X-Dmf-Transfer"] == "passthrough; reason=disabled"


def test_a_transient_failure_is_not_cached_as_untranscodable(streaming):
    """Une panne passagère ne doit pas condamner le fichier à repartir non compressé.

    C'est ce qui s'était produit : l'échec de lecture avait écrit une entrée négative dans
    le cache, et même une fois la lecture réparée le fichier restait servi tel quel.
    """
    source = _bytes(_dataset(frames=8))
    doc = _register(source)
    healthy = streaming._readFile

    def boom(_file):
        raise OSError("assetstore momentanément indisponible")

    streaming._readFile = boom
    try:
        assert _served(streaming.serveFile(doc)) == source
        assert streaming._headers["X-Dmf-Transfer"] == "passthrough; reason=error"
    finally:
        streaming._readFile = healthy  # la panne cesse

    # Rien n'a été mémorisé : la requête suivante retente, et cette fois elle transcode.
    assert len(_served(streaming.serveFile(doc))) < len(source)
