"""Réglages configurables du plugin.

Modifiables via l'API Girder (`system/setting`) ou, plus simplement, via la **page de
configuration du viewer** (`/dmf/?settings`, réservée aux administrateurs), qui les lit et
les écrit par `GET`/`PUT /api/v1/dmf/settings`.

`dmf.viewer_path` : chemin (même origine) sous lequel le plugin sert la SPA, p.ex. `/dmf`
(défaut). Permet de changer l'adresse du viewer sans rebuild (la SPA a une base relative et
déduit l'API de son URL). Recommandé : UN seul segment de chemin (cf. dérivation de l'API
côté client). La modification prend effet au redémarrage de Girder. L'intégration vue item
(`girder-link.js`) lit cette valeur sur `GET /api/v1/dmf/config` : rien d'autre à ajuster.

`dmf.compression` / `dmf.lossy_ratio` : recompression des pixels DICOM avant envoi au
navigateur (cf. transcode.py). Prise en compte IMMÉDIATE (pas de redémarrage) ; changer le
mode ou le ratio invalide de fait le cache (la clé en dépend).

`dmf.compression_max_mb`, `dmf.cache_dir`, `dmf.cache_max_mb` : garde-fous du transcodage
(taille max d'un fichier transcodé en mémoire, emplacement et taille du cache disque).
"""

import os

from girder.exceptions import ValidationException
from girder.utility import setting_utilities

from .transcode import MODES, MODE_LOSSLESS


class PluginSettings:
    VIEWER_PATH = "dmf.viewer_path"
    COMPRESSION = "dmf.compression"
    LOSSY_RATIO = "dmf.lossy_ratio"
    COMPRESSION_MAX_MB = "dmf.compression_max_mb"
    CACHE_DIR = "dmf.cache_dir"
    CACHE_MAX_MB = "dmf.cache_max_mb"


# Valeurs par défaut, également servies telles quelles à la page de configuration.
DEFAULTS = {
    PluginSettings.VIEWER_PATH: "/dmf",
    PluginSettings.COMPRESSION: MODE_LOSSLESS,
    PluginSettings.LOSSY_RATIO: 10.0,
    PluginSettings.COMPRESSION_MAX_MB: 512,
    PluginSettings.CACHE_DIR: "",  # vide → dossier temporaire du système
    PluginSettings.CACHE_MAX_MB: 4096,
}


@setting_utilities.default(PluginSettings.VIEWER_PATH)
def _default_viewer_path():
    return DEFAULTS[PluginSettings.VIEWER_PATH]


@setting_utilities.validator(PluginSettings.VIEWER_PATH)
def _validate_viewer_path(doc):
    value = (doc.get("value") or "").strip()
    if not value.startswith("/") or value.strip("/") == "":
        raise ValidationException(
            'Le chemin du viewer doit commencer par "/" et ne pas être la racine.', "value"
        )
    doc["value"] = "/" + value.strip("/")  # normalise (pas de slash final)


@setting_utilities.default(PluginSettings.COMPRESSION)
def _default_compression():
    return DEFAULTS[PluginSettings.COMPRESSION]


@setting_utilities.validator(PluginSettings.COMPRESSION)
def _validate_compression(doc):
    value = str(doc.get("value") or "").strip().lower()
    if value not in MODES:
        raise ValidationException(
            "Mode de compression invalide (attendu : %s)." % ", ".join(MODES), "value"
        )
    doc["value"] = value


@setting_utilities.default(PluginSettings.LOSSY_RATIO)
def _default_lossy_ratio():
    return DEFAULTS[PluginSettings.LOSSY_RATIO]


@setting_utilities.validator(PluginSettings.LOSSY_RATIO)
def _validate_lossy_ratio(doc):
    try:
        value = float(doc.get("value"))
    except (TypeError, ValueError):
        raise ValidationException("Le ratio de compression doit être un nombre.", "value")
    # Bornes volontairement conservatrices : sous 2:1 le mode avec perte n'a pas d'intérêt,
    # au-delà de 50:1 les artefacts deviennent visibles à l'échelle d'une mesure.
    if not 2.0 <= value <= 50.0:
        raise ValidationException("Le ratio doit être compris entre 2 et 50.", "value")
    doc["value"] = value


def _positive_int_validator(label):
    def validate(doc):
        try:
            value = int(doc.get("value"))
        except (TypeError, ValueError):
            raise ValidationException("%s doit être un entier (0 = illimité)." % label, "value")
        if value < 0:
            raise ValidationException("%s ne peut pas être négatif." % label, "value")
        doc["value"] = value

    return validate


@setting_utilities.default(PluginSettings.COMPRESSION_MAX_MB)
def _default_compression_max_mb():
    return DEFAULTS[PluginSettings.COMPRESSION_MAX_MB]


setting_utilities.validator(PluginSettings.COMPRESSION_MAX_MB)(
    _positive_int_validator("La taille maximale à transcoder")
)


@setting_utilities.default(PluginSettings.CACHE_MAX_MB)
def _default_cache_max_mb():
    return DEFAULTS[PluginSettings.CACHE_MAX_MB]


setting_utilities.validator(PluginSettings.CACHE_MAX_MB)(
    _positive_int_validator("La taille du cache")
)


@setting_utilities.default(PluginSettings.CACHE_DIR)
def _default_cache_dir():
    return DEFAULTS[PluginSettings.CACHE_DIR]


@setting_utilities.validator(PluginSettings.CACHE_DIR)
def _validate_cache_dir(doc):
    value = str(doc.get("value") or "").strip()
    if value and not os.path.isabs(value):
        raise ValidationException("Le dossier de cache doit être un chemin absolu.", "value")
    doc["value"] = value
