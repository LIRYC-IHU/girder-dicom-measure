"""Service des pixels : transcodage (cf. transcode.py) + cache + réponse en flux.

Glue entre les réglages Girder, le cache disque et le transcodeur. La route
`GET /api/v1/dmf/file/:id` passe par ici ; en cas d'inapplicabilité (mode `none`, fichier
déjà compressé, non-DICOM, trop volumineux, erreur d'encodage) on retombe sur le
téléchargement Girder standard — le viewer reçoit alors l'original.

Ce repli n'est PAS silencieux : toute réponse porte un en-tête `X-Dmf-Transfer` disant ce
qui a été fait (`transcoded; mode=…`) ou pourquoi ça ne l'a pas été
(`passthrough; reason=already-compressed|not-dicom|unsupported-format|too-large|no-gain|error|disabled`).

LIVRAISON FRAME PAR FRAME : une boucle de scopie non compressée est un seul fichier de
plusieurs dizaines (voire centaines) de Mo, dont l'encodage complet prend des secondes — et
le loader du client n'affiche rien avant le dernier octet. `frameCount` + `serveFrame`
permettent donc de servir chaque frame comme un DICOM mono-frame indépendant : la première
image s'affiche en ~0,15 s, le reste arrive en tâche de fond. Seuls les octets de la frame
demandée sont lus dans la source (cf. `transcode.frameLayout`).
"""

import io
import logging
import os
import threading

from girder.api.rest import setContentDisposition, setResponseHeader
from girder.exceptions import RestException
from girder.models.file import File
from girder.models.setting import Setting

from .settings import PluginSettings
from .transcode import (
    MODE_NONE,
    buildFrame,
    declaredFrames,
    frameLayout,
    SKIP_DISABLED,
    SKIP_ERROR,
    SKIP_NO_GAIN,
    SKIP_TOO_LARGE,
    SKIP_UNSUPPORTED,
    TranscodeCache,
    cacheKey,
    inspect,
    normalizeMode,
    transcode,
)

# Enfant du logger « girder » : c'est lui qui porte les handlers configurés par Girder.
logger = logging.getLogger("girder.dicom_measure_flow")

_CHUNK = 1024 * 1024  # taille des morceaux relus depuis le cache
# Lecture de la source par blocs : `FileHandle.read()` REFUSE toute lecture unique plus
# grande que le réglage Girder `core.filehandle_max_size` (16 Mo par défaut) — soit à peu
# près toutes les boucles de scopie. Un `read()` sans argument levait donc une exception,
# et le fichier repartait non compressé.
_READ_CHUNK = 8 * 1024 * 1024

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


def _readExactly(fp, size):
    """`size` octets à la position courante, en respectant le plafond de lecture Girder."""
    buf = io.BytesIO()
    while buf.tell() < size:
        chunk = fp.read(min(_READ_CHUNK, size - buf.tell()))
        if not chunk:
            break
        buf.write(chunk)
    return buf.getvalue()


def frameCount(file):
    """Nombre de frames LIVRABLES SÉPARÉMENT pour ce fichier (1 = servir le fichier entier).

    C'est la valeur annoncée au client par `GET /dmf/item/:id/files` : il construit une URL
    par frame quand elle dépasse 1. On ne renvoie > 1 que si la découpe est réellement
    possible (source non compressée, format géré) ET que le transcodage est actif — sinon
    découper coûterait plus qu'il ne rapporte.
    """
    mode, _ratio = compressionSettings()
    if mode == MODE_NONE:
        return 1
    try:
        with File().open(file) as fp:
            layout = frameLayout(fp)
    except Exception:
        logger.exception("[dmf] lecture de l'en-tête impossible pour %s", file.get("name"))
        return 1
    return layout.frames if layout else 1


def probeDeclaredFrames(file):
    """`NumberOfFrames` de l'en-tête d'un fichier Girder (1 par défaut, jamais d'exception)."""
    try:
        with File().open(file) as fp:
            return declaredFrames(fp) or 1
    except Exception:
        logger.exception("[dmf] lecture de l'en-tête impossible pour %s", file.get("name"))
        return 1


def _readFile(file):
    """Contenu complet d'un fichier Girder, lu PAR BLOCS BORNÉS (cf. `_READ_CHUNK`)."""
    buf = io.BytesIO()
    with File().open(file) as fp:
        while True:
            chunk = fp.read(_READ_CHUNK)
            if not chunk:
                break
            buf.write(chunk)
    return buf.getvalue()


def _produce(file, mode, ratio):
    """Transcode le fichier.

    Renvoie `(données, raison, mémorisable)` :
      - `(bytes, "transcoded", True)` en cas de succès ;
      - `(b"", <raison>, True)` si le fichier n'est PAS transcodable — propriété stable du
        fichier, donc mémorisée pour ne pas le ré-analyser à chaque requête ;
      - `(b"", "error", False)` sur échec technique (I/O, mémoire…). NE PAS mémoriser : une
        panne passagère ne doit pas condamner ce fichier à repartir non compressé pour
        toujours — c'est précisément ce qui s'est produit avec la lecture non bornée.
    """
    maxBytes = _maxBytes()
    if maxBytes and (file.get("size") or 0) > maxBytes:
        return b"", SKIP_TOO_LARGE, True
    try:
        data = _readFile(file)
        result = transcode(data, mode, ratio, maxBytes=maxBytes)
    except Exception:
        logger.exception("[dmf] transcodage impossible pour %s — fichier servi tel quel",
                         file.get("name"))
        return b"", SKIP_ERROR, False
    if result is None:
        return b"", inspect(io.BytesIO(data)) or SKIP_NO_GAIN, True
    logger.info(
        "[dmf] %s transcodé en %s (%.2f:1)", file.get("name"), result.label, result.ratio
    )
    return result.data, "transcoded", True


def _produceFrame(file, index, mode, ratio):
    """Encode UNE frame. Seuls les octets de cette frame sont lus dans la source — c'est ce
    qui rend l'affichage progressif possible : ~0,15 s au lieu des secondes qu'exige la
    boucle entière."""
    try:
        with File().open(file) as fp:
            layout = frameLayout(fp)
            if layout is None or not 0 <= index < layout.frames:
                return b"", SKIP_UNSUPPORTED, True
            fp.seek(layout.offsetOf(index))
            frameBytes = _readExactly(fp, layout.frameSize)
        if len(frameBytes) != layout.frameSize:
            return b"", SKIP_ERROR, False  # source tronquée : ne pas mémoriser
        result = buildFrame(layout, frameBytes, index, mode, ratio)
    except Exception:
        logger.exception("[dmf] frame %d de %s : encodage impossible", index, file.get("name"))
        return b"", SKIP_ERROR, False
    if result is None:
        return b"", SKIP_NO_GAIN, True
    return result.data, "transcoded", True


def serveFrame(file, index):
    """Réponse en flux pour UNE frame d'un fichier multi-frame (cf. `frameCount`)."""
    mode, ratio = compressionSettings()
    store = cache()
    key = cacheKey(file["_id"], _revision(file), mode, ratio, frame=index)

    known, path = store.lookup(key)
    reason = "cached"
    if not known:
        with _keyLock(key):
            known, path = store.lookup(key)
            if not known:
                data, reason, cacheable = _produceFrame(file, index, mode, ratio)
                if cacheable:
                    store.put(key, data)
                    _known, path = store.lookup(key)
    if path is None:
        raise RestException(
            "Frame %d indisponible pour ce fichier (%s)."
            % (index, reason if reason != "cached" else SKIP_UNSUPPORTED),
            code=404,
        )

    setResponseHeader("Content-Type", "application/dicom")
    setResponseHeader("Content-Length", str(os.path.getsize(path)))
    setResponseHeader("X-Dmf-Transfer", "transcoded; mode=%s; frame=%d" % (mode, index))
    setContentDisposition("%s-%d.dcm" % (file["name"], index))
    return _streamPath(path)


def _passthrough(file, reason):
    """Fichier servi tel quel — la RAISON part dans la réponse (`X-Dmf-Transfer`), pour que
    « pourquoi ce fichier n'est-il pas compressé ? » se lise dans l'onglet réseau."""
    setResponseHeader("X-Dmf-Transfer", "passthrough; reason=%s" % reason)
    return File().download(file)


def serveFile(file):
    """Réponse en flux pour un fichier de pixels, transcodée si le réglage s'y prête.

    Renvoie une fonction génératrice (convention Girder pour une réponse en flux).
    """
    mode, ratio = compressionSettings()
    if mode == MODE_NONE:
        return _passthrough(file, SKIP_DISABLED)

    store = cache()
    key = cacheKey(file["_id"], _revision(file), mode, ratio)

    known, path = store.lookup(key)
    reason = "cached"
    if not known:
        # Verrou par clé : si plusieurs lecteurs ouvrent le même examen en même temps, un
        # seul encode (le transcodage est la partie coûteuse).
        with _keyLock(key):
            known, path = store.lookup(key)  # une requête concurrente a pu produire l'entrée
            if not known:
                data, reason, cacheable = _produce(file, mode, ratio)
                if cacheable:
                    store.put(key, data)
                    _known, path = store.lookup(key)
    if path is None:
        return _passthrough(file, reason if reason != "cached" else "not-transcodable")

    setResponseHeader("Content-Type", "application/dicom")
    setResponseHeader("Content-Length", str(os.path.getsize(path)))
    setResponseHeader("X-Dmf-Transfer", "transcoded; mode=%s" % mode)
    setContentDisposition(file["name"])
    return _streamPath(path)
