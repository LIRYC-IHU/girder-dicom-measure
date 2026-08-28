"""Plugin Girder pour DICOM Measure Flow.

Rôles (cf. CLAUDE.md) :
  1. Extraction automatique des métadonnées DICOM à l'upload (réplique `dicom_viewer`)
     + tri des instances → `dicom_metadata.handleUploadedDicom` (event `data.process`).
  2. Exposition du champ `dicom` de l'item via l'API REST (le client lit l'ordre trié).
  3. Routes dédiées `/api/v1/dmf/*` (auth cookie) + service de la SPA sous le chemin
     configurable `dmf.viewer_path` (défaut `/dmf`).
  4. Intégration dans la vue item du client web Girder (`girder-link.js`), déclarée via
     `registerPluginStaticContent` : c'est Girder qui injecte la balise dans sa page, le
     déploiement n'a plus à patcher son `index.html`.
"""

import os
from pathlib import Path

import cherrypy
from girder import events
from girder.constants import AccessType
from girder.models.item import Item
from girder.models.setting import Setting
from girder.plugin import GirderPlugin, registerPluginStaticContent

from .dicom_metadata import handleUploadedDicom
from .models import migrateFromItemMetadata
from .rest import DmfResource
from .settings import PluginSettings
from .spa import SpaServer

WEB_DIST = os.path.join(os.path.dirname(__file__), "web_dist")
# Script d'intégration vue item, déposé à la racine du bundle par vite (web/public/).
GIRDER_LINK = "girder-link.js"


class DicomMeasureFlowPlugin(GirderPlugin):
    DISPLAY_NAME = "DICOM Measure Flow"

    def load(self, info):
        # 1 + 2 : extraction des métadonnées DICOM + tri des instances à la réception.
        events.bind("data.process", "dicom_measure_flow", handleUploadedDicom)
        # 2 : rendre le champ `dicom` (méta communes + ordre des fichiers) lisible via REST.
        Item().exposeFields(level=AccessType.READ, fields="dicom")

        # Auth durcie (option 1) : routes dédiées /api/v1/dmf/* en auth cookie (aucun token
        # exposé en JS), mutation protégée par vérif d'Origin.
        info["apiRoot"].dmf = DmfResource()

        # Migration one-way des annotations historiques (item.meta) → collection dédiée.
        try:
            migrateFromItemMetadata()
        except Exception as exc:  # ne jamais bloquer le démarrage
            cherrypy.log("[dicom_measure_flow] migration annotations ignorée : %r" % exc)

        # 3 : servir la SPA sous le chemin configurable (défaut /dmf).
        # IMPORTANT : monter sur `info['serverRoot']` (l'arbre cherrypy réellement servi
        # par Girder), PAS sur le `cherrypy.tree` global — Girder crée son propre Tree.
        mountPath = Setting().get(PluginSettings.VIEWER_PATH) or "/dmf"
        if os.path.isdir(WEB_DIST):
            info["serverRoot"].mount(SpaServer(WEB_DIST), mountPath)
            cherrypy.log("[dicom_measure_flow] SPA montée sur %s/ depuis %s" % (mountPath, WEB_DIST))
        else:
            cherrypy.log(
                "[dicom_measure_flow] web_dist absent (%s) — lancer `make build`." % WEB_DIST
            )

        # 4 : intégration vue item. `registerPluginStaticContent` fait charger le script
        # par le client web Girder (avec cache-busting), ce qui remplace l'injection
        # manuelle d'une balise dans son index.html au moment de la construction de l'image.
        # Le script est servi depuis /plugin_static/, pas depuis le chemin du viewer : il
        # lit celui-ci sur `GET /api/v1/dmf/config`.
        if os.path.isfile(os.path.join(WEB_DIST, GIRDER_LINK)):
            registerPluginStaticContent(
                plugin="dicom_measure_flow",
                css=[],
                js=["/" + GIRDER_LINK],
                staticDir=Path(WEB_DIST),
                tree=info["serverRoot"],
            )
        else:
            cherrypy.log(
                "[dicom_measure_flow] %s absent de web_dist — pas d'intégration vue item."
                % GIRDER_LINK
            )
