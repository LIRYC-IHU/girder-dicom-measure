"""Serveur statique de la SPA (bundle prébuild dans web_dist), monté sous /dmf.

Auth durcie (option 1) : la SPA s'authentifie via le COOKIE de session sur les routes
dédiées `/api/v1/dmf/*` (cf. rest.py) → AUCUN token n'est injecté dans la page. On sert
donc l'index.html tel quel (pas de contenu par-utilisateur, cacheable).
"""

import mimetypes
import os

import cherrypy

# Fichiers SANS hash de contenu dans leur nom → à revalider (sinon le navigateur sert une
# version périmée après mise à jour). Les assets `assets/*-<hash>.js` restent cacheables.
_NO_CACHE = {"index.html", "girder-link.js"}


class SpaServer:
    def __init__(self, dist):
        self._dist = os.path.abspath(dist)

    @cherrypy.expose
    def index(self, **params):
        return self._serve_index()

    @cherrypy.expose
    def default(self, *segments, **params):
        rel = os.path.normpath(os.path.join(*segments)) if segments else ""
        target = os.path.abspath(os.path.join(self._dist, rel))
        # Anti path-traversal + fichier existant → sert l'asset statique.
        if target.startswith(self._dist) and os.path.isfile(target):
            ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
            content = cherrypy.lib.static.serve_file(target, content_type=ctype)
            if os.path.basename(target) in _NO_CACHE:
                cherrypy.response.headers["Cache-Control"] = "no-cache"
            return content
        # Sinon : route SPA → on retombe sur index.html.
        return self._serve_index()

    def _serve_index(self):
        content = cherrypy.lib.static.serve_file(
            os.path.join(self._dist, "index.html"), content_type="text/html"
        )
        cherrypy.response.headers["Cache-Control"] = "no-cache"
        return content
