"""Réglages configurables du plugin (via l'API Girder `system/setting` ou l'admin console).

`dmf.viewer_path` : chemin (même origine) sous lequel le plugin sert la SPA, p.ex. `/dmf`
(défaut). Permet de changer l'adresse du viewer sans rebuild (la SPA a une base relative et
déduit l'API de son URL). Recommandé : UN seul segment de chemin (cf. dérivation de l'API
côté client). La modification prend effet au redémarrage de Girder. L'intégration vue item
(`girder-link.js`) lit cette valeur sur `GET /api/v1/dmf/config` : rien d'autre à ajuster.
"""

from girder.exceptions import ValidationException
from girder.utility import setting_utilities


class PluginSettings:
    VIEWER_PATH = "dmf.viewer_path"


@setting_utilities.default(PluginSettings.VIEWER_PATH)
def _default_viewer_path():
    return "/dmf"


@setting_utilities.validator(PluginSettings.VIEWER_PATH)
def _validate_viewer_path(doc):
    value = (doc.get("value") or "").strip()
    if not value.startswith("/") or value.strip("/") == "":
        raise ValidationException(
            'Le chemin du viewer doit commencer par "/" et ne pas être la racine.', "value"
        )
    doc["value"] = "/" + value.strip("/")  # normalise (pas de slash final)
