"""Service des pixels : transcodage (cf. transcode.py) + cache + réponse en flux.

Glue entre les réglages Girder, le cache disque et le transcodeur. La route
`GET /api/v1/dmf/file/:id` passe par ici ; en cas d'inapplicabilité (mode `none`, fichier
déjà compressé, non-DICOM, trop volumineux, erreur d'encodage) on retombe silencieusement
sur le téléchargement Girder standard — le viewer reçoit alors l'original.
"""

import logging
import os
import threading

from girder.api.rest import setContentDisposition, setResponseHeader
from girder.models.file import File
from girder.models.setting import Setting

from .settings import PluginSettings
from .transcode import MODE_NONE, TranscodeCache, cacheKey, normalizeMode, transcode

logger = logging.getLogger(__name__)

_CHUNK = 1024 * 1024  # taille des morceaux relus depuis le cache

_cache = None
_cacheParams = None
_cacheLock = threading.Lock()

# Un verrou PAR clé : deux lecteurs ouvrant le même examen au même instant ne doivent pas
# encoder deux fois la même boucle (le transcodage est la partie coûteuse en CPU).
_keyLocks = {}
_keyLocksGuard = threading.Lock()


def compressionSettings():
    """(mode, ratio) courants, tolérants à un réglage absent ou aberrant."""
    setting = Setting()
    mode = normalizeMode(setting.get(PluginSettings.COMPRESSION))
    try:
        ratio = float(setting.get(PluginSettings.LOSSY_RATIO))
    except (TypeError, ValueError):
        ratio = 10.0
    return mode, ratio


def cache():
    """Cache disque courant (recréé si les réglages d'emplacement/taille ont changé)."""
    global _cache, _cacheParams
    setting = Setting()
    directory = (setting.get(PluginSettings.CACHE_DIR) or "").strip() or None
    try:
        maxBytes = int(setting.get(PluginSettings.CACHE_MAX_MB) or 0) * 1024 * 1024
    except (TypeError, ValueError):
        maxBytes = 0
    params = (directory, maxBytes)
    with _cacheLock:
        if _cacheParams != params or _cache is None:
            _cache = TranscodeCache(directory, maxBytes)
            _cacheParams = params
        return _cache


def _maxBytes():
    try:
        return int(Setting().get(PluginSettings.COMPRESSION_MAX_MB) or 0) * 1024 * 1024
    except (TypeError, ValueError):
        return 0


def _keyLock(key):
    with _keyLocksGuard:
        if len(_keyLocks) > 1024:
            # Purge de sécurité : les verrous encore tenus restent référencés par leur
            # détenteur, on risque au pire un double encodage, jamais une incohérence.
            _keyLocks.clear()
        lock = _keyLocks.get(key)
        if lock is None:
            lock = _keyLocks[key] = threading.Lock()
        return lock


def _revision(file):
    """Empreinte du CONTENU du fichier : deux versions d'un même id ne partagent pas d'entrée."""
    return file.get("sha512") or "%s-%s" % (file.get("size"), file.get("created"))


def _streamPath(path):
    def stream():
        with open(path, "rb") as fp:
            while True:
                chunk = fp.read(_CHUNK)
                if not chunk:
                    break
                yield chunk

    return stream


def _produce(file, mode, ratio):
    """Transcode le fichier ; renvoie les octets, ou b'' si non applicable."""
    maxBytes = _maxBytes()
    if maxBytes and (file.get("size") or 0) > maxBytes:
        return b""
    try:
        with File().open(file) as fp:
            data = fp.read()
        result = transcode(data, mode, ratio, maxBytes=maxBytes)
    except Exception as exc:
        logger.warning("[dmf] transcodage impossible pour %s : %r", file.get("_id"), exc)
        return b""
    if result is None:
        return b""
    logger.info(
        "[dmf] %s transcodé en %s (%.2f:1)", file.get("name"), result.label, result.ratio
    )
    return result.data


def serveFile(file):
    """Réponse en flux pour un fichier de pixels, transcodée si le réglage s'y prête.

    Renvoie une fonction génératrice (convention Girder pour une réponse en flux).
    """
    mode, ratio = compressionSettings()
    if mode == MODE_NONE:
        return File().download(file)

    store = cache()
    key = cacheKey(file["_id"], _revision(file), mode, ratio)

    known, path = store.lookup(key)
    if not known:
        # Verrou par clé : si plusieurs lecteurs ouvrent le même examen en même temps, un
        # seul encode (le transcodage est la partie coûteuse).
        with _keyLock(key):
            known, path = store.lookup(key)  # une requête concurrente a pu produire l'entrée
            if not known:
                store.put(key, _produce(file, mode, ratio))
                _known, path = store.lookup(key)
    if path is None:
        # Entrée négative (non transcodable) ou cache indisponible → fichier original.
        return File().download(file)

    setResponseHeader("Content-Type", "application/dicom")
    setResponseHeader("Content-Length", str(os.path.getsize(path)))
    setResponseHeader("X-Dmf-Compression", mode)
    setContentDisposition(file["name"])
    return _streamPath(path)
