"""Recompression des pixels DICOM AVANT envoi au navigateur (économie de bande passante).

Contexte : les sources sont souvent en syntaxe de transfert NON compressée (Explicit/Implicit
VR Little Endian). Une simple boucle de scopie (512×512 × ~70 frames) pèse alors ~18 Mo, et
une série CT complète bien davantage. On transcode donc à la volée avant de streamer :

  - `lossless` (défaut) : **JPEG-LS sans perte** (1.2.840.10008.1.2.4.80) — rapide (~0,2 s
    pour 69 frames 8 bits) et ~1,9:1 en scopie, ~2,9:1 en CT. Pixels strictement identiques
    → les mesures restent valides au pixel près.
  - `lossy` : **JPEG 2000 avec perte** (1.2.840.10008.1.2.4.91) au ratio demandé (défaut
    10:1). Seul codec permettant de viser un ratio ; réservé aux images MONOCHROME
    (SamplesPerPixel == 1) — une image couleur retombe sur le mode sans perte, faute de
    gestion fiable de la transformation de composantes.
  - `none` : aucun transcodage (fichier servi tel quel).

Garde-fous :
  - un fichier DÉJÀ compressé (syntaxe encapsulée : JPEG, JPEG-LS, J2K, RLE…) est servi tel
    quel — recompresser ne gagnerait rien et, en lossy, dégraderait deux fois ;
  - un non-DICOM, un DICOM sans PixelData ou d'un format non encodable est servi tel quel ;
  - au-delà de `maxBytes` (réglage `dmf.compression_max_mb`) on ne charge pas les pixels en
    mémoire → fichier servi tel quel ;
  - si le résultat n'est pas plus petit que la source, on sert la source.

Le SOPInstanceUID est CONSERVÉ : les mesures déjà enregistrées le référencent. En lossy on
estampille en revanche `LossyImageCompression`/`Ratio`/`Method` (le viewer l'affiche).

Ce module n'importe NI Girder NI Mongo → testable avec pydicom seul (cf. tests/).
"""

import hashlib
import io
import logging
import os
import tempfile

import pydicom
from pydicom.uid import (
    JPEG2000,
    JPEG2000Lossless,
    JPEGLSLossless,
    RLELossless,
)

logger = logging.getLogger(__name__)

MODE_NONE = "none"
MODE_LOSSLESS = "lossless"
MODE_LOSSY = "lossy"
MODES = (MODE_NONE, MODE_LOSSLESS, MODE_LOSSY)

# Codecs sans perte, par ordre de préférence : JPEG-LS (le plus rapide et le meilleur sur
# de l'imagerie médicale), puis J2K, puis RLE (encodeur natif pydicom, toujours présent).
# Tous sont décodables par @cornerstonejs/dicom-image-loader.
_LOSSLESS_CHAIN = (JPEGLSLossless, JPEG2000Lossless, RLELossless)

# Version du format de sortie : incrémenter invalide le cache disque.
CACHE_VERSION = 1

# Libellés lisibles (exposés par l'API pour affichage dans le viewer).
_TS_LABELS = {
    JPEGLSLossless: "JPEG-LS sans perte",
    JPEG2000Lossless: "JPEG 2000 sans perte",
    RLELossless: "RLE sans perte",
    JPEG2000: "JPEG 2000 avec perte",
}


class Transcoded:
    """Résultat d'un transcodage réussi."""

    def __init__(self, data, transferSyntax, originalSize):
        self.data = data
        self.transferSyntax = str(transferSyntax)
        self.label = _TS_LABELS.get(transferSyntax, str(transferSyntax))
        self.originalSize = originalSize

    @property
    def ratio(self):
        return (self.originalSize / len(self.data)) if self.data else 1.0


def normalizeMode(value):
    """Mode valide, sinon `lossless` (jamais d'exception : la lecture d'un réglage douteux
    ne doit pas casser le service des pixels)."""
    value = str(value or "").strip().lower()
    return value if value in MODES else MODE_LOSSLESS


# Un encodeur manquant est une ERREUR DE DÉPLOIEMENT, pas un cas courant : on la signale
# (une seule fois par cause, sinon une série CT produirait une ligne par coupe) au lieu de
# dégrader en silence — c'est précisément ce qui masquerait un `lossy` devenu `lossless`.
_warned = set()


def _warnOnce(key, message, *args):
    if key not in _warned:
        _warned.add(key)
        logger.warning(message, *args)


def _isEncodable(ds):
    """Le dataset décrit-il une image ré-encodable par les codecs utilisés ici ?

    Ne teste QUE des tags de l'en-tête (groupe 0028) : la fonction sert aussi au pré-contrôle
    sur un dataset lu avec `stop_before_pixels=True`, où `PixelData` est absent par
    construction. La présence effective des pixels est vérifiée à la lecture complète.
    """
    if "FloatPixelData" in ds or "DoubleFloatPixelData" in ds:
        return False
    if not ds.get("Rows") or not ds.get("Columns"):
        return False
    if int(ds.get("SamplesPerPixel") or 0) not in (1, 3):
        return False
    if int(ds.get("BitsAllocated") or 0) not in (8, 16):
        return False
    # Palette : la table de couleurs sort du domaine des encodeurs utilisés ici.
    if str(ds.get("PhotometricInterpretation") or "").startswith("PALETTE"):
        return False
    return True


def inspect(fp):
    """Pré-contrôle SUR L'EN-TÊTE SEUL (pas de lecture des pixels).

    Renvoie True si le fichier est un DICOM non compressé a priori transcodable. Permet
    d'écarter à moindre coût les non-DICOM et les fichiers déjà compressés, qui sont le cas
    fréquent, avant de charger les pixels en mémoire.
    """
    try:
        ds = pydicom.dcmread(fp, stop_before_pixels=True, defer_size=1024)
    except Exception:
        return False  # pas du DICOM
    ts = getattr(ds.file_meta, "TransferSyntaxUID", None)
    if ts is None or ts.is_encapsulated:
        return False  # déjà compressé → on sert tel quel
    return _isEncodable(ds)


def _compress(ds, transferSyntax, **kwargs):
    # `generate_instance_uid=False` : pydicom renouvelle le SOPInstanceUID par défaut (règle
    # de conformance pour une image DÉRIVÉE que l'on archiverait). Ici la sortie n'est qu'une
    # représentation de transport de l'original — et les mesures déjà enregistrées le
    # référencent par ce même UID. On le CONSERVE donc.
    ds.compress(transferSyntax, generate_instance_uid=False, **kwargs)
    return ds


def _toLossless(ds):
    """Essaie les codecs sans perte dans l'ordre ; renvoie l'UID retenu ou None."""
    for uid in _LOSSLESS_CHAIN:
        try:
            _compress(ds, uid)
            return uid
        except Exception as exc:
            logger.debug("[dmf] encodage %s indisponible/échoué : %r", uid, exc)
    _warnOnce(
        "lossless",
        "[dmf] aucun encodeur sans perte disponible (JPEG-LS/J2K/RLE) : les pixels sont "
        "envoyés NON COMPRESSÉS. Installer `pyjpegls`.",
    )
    return None


def _toLossy(ds, ratio):
    """JPEG 2000 au ratio demandé (monochrome uniquement) ; renvoie l'UID ou None."""
    if int(ds.get("SamplesPerPixel") or 1) != 1:
        return None  # couleur → l'appelant retombera sur le sans perte
    try:
        _compress(ds, JPEG2000, j2k_cr=[float(ratio)])
    except Exception as exc:
        _warnOnce(
            "lossy",
            "[dmf] mode « avec perte » demandé mais l'encodeur JPEG 2000 est indisponible "
            "(%r) — repli sur le sans perte. Installer `pylibjpeg` ET `pylibjpeg-openjpeg` "
            "(le second seul ne suffit pas).",
            exc,
        )
        return None
    # Estampille DICOM de la perte (l'image N'EST PLUS l'original).
    ds.LossyImageCompression = "01"
    ds.LossyImageCompressionRatio = float(ratio)
    ds.LossyImageCompressionMethod = "ISO_15444_1"
    return JPEG2000


def transcode(data, mode=MODE_LOSSLESS, ratio=10.0, maxBytes=0):
    """Transcode un fichier DICOM complet (bytes) ; None si non applicable.

    `maxBytes` (0 = illimité) borne la taille de la source acceptée : l'encodage charge les
    pixels en mémoire, on refuse donc les fichiers démesurés plutôt que de saturer le serveur.
    """
    mode = normalizeMode(mode)
    if mode == MODE_NONE:
        return None
    if maxBytes and len(data) > maxBytes:
        return None
    if not inspect(io.BytesIO(data)):
        return None

    try:
        ds = pydicom.dcmread(io.BytesIO(data))
    except Exception:
        return None
    if "PixelData" not in ds:
        return None

    uid = _toLossy(ds, ratio) if mode == MODE_LOSSY else None
    if uid is None:
        uid = _toLossless(ds)
    if uid is None:
        return None

    try:
        buf = io.BytesIO()
        ds.save_as(buf, enforce_file_format=True)
    except Exception as exc:
        logger.warning("[dmf] sérialisation du DICOM transcodé échouée : %r", exc)
        return None

    out = buf.getvalue()
    if len(out) >= len(data):
        return None  # aucun gain → on sert la source
    return Transcoded(out, uid, len(data))


# --- Cache disque -----------------------------------------------------------
#
# Le transcodage d'une boucle de scopie coûte de 0,2 s (JPEG-LS) à ~1 s (J2K) : à refaire
# pour chaque relecture de l'examen, ce serait une régression de latence. On mémorise donc
# la sortie sur disque, indexée par (fichier, mode, ratio), avec une éviction LRU par date
# d'accès. Une entrée VIDE (0 octet) mémorise un « non transcodable » : elle évite de
# re-parser l'en-tête d'un fichier déjà compressé à chaque requête.


def cacheKey(fileId, revision, mode, ratio):
    """Clé stable d'une sortie transcodée. `revision` distingue deux contenus d'un même id
    (re-upload) — en pratique le sha512 ou la taille du fichier Girder."""
    raw = "|".join(
        [str(CACHE_VERSION), str(fileId), str(revision), normalizeMode(mode), "%.3f" % float(ratio)]
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def defaultCacheDir():
    return os.path.join(tempfile.gettempdir(), "dmf-transcode-cache")


class TranscodeCache:
    """Cache disque simple, borné en taille, éviction LRU (par mtime)."""

    def __init__(self, directory=None, maxBytes=0):
        self.directory = directory or defaultCacheDir()
        self.maxBytes = maxBytes

    def _path(self, key):
        # Un niveau de sous-dossier : évite un répertoire à dizaines de milliers d'entrées.
        return os.path.join(self.directory, key[:2], key)

    def lookup(self, key):
        """`(connu, chemin)` :
        - `(False, None)` : jamais transcodé ;
        - `(True, None)`  : entrée négative — déjà examiné, non transcodable ;
        - `(True, path)`  : sortie disponible (à streamer depuis le disque, sans la charger).
        """
        path = self._path(key)
        try:
            size = os.path.getsize(path)
        except OSError:
            return False, None
        try:  # marque l'accès (LRU)
            os.utime(path, None)
        except OSError:
            pass
        return True, (path if size > 0 else None)

    def put(self, key, data):
        """Écrit une entrée (`data` vide = mémorise « non transcodable »). Best-effort : un
        cache indisponible ne doit jamais empêcher de servir les pixels."""
        path = self._path(key)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            # Écriture atomique : deux requêtes concurrentes ne peuvent pas se servir un
            # fichier tronqué.
            tmp = "%s.%d.tmp" % (path, os.getpid())
            with open(tmp, "wb") as fp:
                fp.write(data)
            os.replace(tmp, path)
        except OSError as exc:
            logger.warning("[dmf] cache de transcodage non écrit (%s) : %r", path, exc)
            return
        self.sweep()

    def sweep(self):
        """Éviction LRU jusqu'à repasser sous `maxBytes` (0 = pas de limite)."""
        if not self.maxBytes:
            return
        entries = []
        total = 0
        for root, _dirs, files in os.walk(self.directory):
            for name in files:
                if name.endswith(".tmp"):
                    continue
                full = os.path.join(root, name)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                entries.append((st.st_mtime, st.st_size, full))
                total += st.st_size
        if total <= self.maxBytes:
            return
        for _mtime, size, full in sorted(entries):  # plus anciens accès d'abord
            try:
                os.remove(full)
            except OSError:
                continue
            total -= size
            if total <= self.maxBytes:
                return
