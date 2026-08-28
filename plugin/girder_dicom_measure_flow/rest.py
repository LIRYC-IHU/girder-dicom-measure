"""Routes REST dédiées au viewer (`/api/v1/dmf/...`).

Durcissement de l'autorisation (option 1) : la SPA n'a PLUS besoin d'un token en JS.
  - Lectures (user, files, dicom, annotations, pixels) : auth par COOKIE de session
    (`@access.user(cookie=True)`) — sûr car sans effet de bord. Le token explicite reste
    accepté (utile en dev cross-origin).
  - Écritures (POST/PUT/DELETE annotations) : auth par cookie autorisée MAIS protégée
    contre le CSRF par une vérification d'Origin (même origine que Girder requise).
  - Toutes les routes vérifient l'accès Girder (READ/WRITE) de l'utilisateur à l'item.

Annotations : stockées dans la collection dédiée `dmf_annotation` (cf. models.py) →
liste filtrée/paginée + CRUD unitaire par `key` (identifiant client).
"""

import cherrypy
from girder.api import access
from girder.api.describe import Description, autoDescribeRoute
from girder.api.rest import Resource, getCurrentUser
from girder.constants import AccessType
from girder.exceptions import AccessException, RestException
from girder.models.file import File
from girder.models.folder import Folder
from girder.models.item import Item
from girder.models.setting import Setting

from .dicom_metadata import processItem
from .models import Annotation
from .settings import PluginSettings


def _folderItemIds(folder, user):
    """IDs des items d'un dossier ET de ses sous-dossiers ACCESSIBLES (récursif)."""
    ids = [item["_id"] for item in Folder().childItems(folder)]
    for sub in Folder().childFolders(parentType="folder", parent=folder, user=user):
        ids.extend(_folderItemIds(sub, user))
    return ids


def _expectedOrigins():
    """Origines (scheme://host[:port]) considérées « même origine » que cette instance.

    Gère le déploiement derrière un reverse-proxy : l'hôte/scheme PUBLIC vu par le navigateur
    arrive dans `X-Forwarded-Host`/`X-Forwarded-Proto` (et/ou `Host`), alors que
    `cherrypy.request.base` est l'URL interne. On accepte donc ces variantes.

    Sûr vis-à-vis du CSRF : un site tiers dans le navigateur de la victime ne peut PAS forger
    `X-Forwarded-*` ni `Origin` (en-têtes interdits à JS) — ces requêtes passent par l'URL
    publique via le proxy. (Le plugin doit n'être joignable QUE via le proxy en prod.)
    """
    headers = cherrypy.request.headers
    cands = set()
    base = (cherrypy.request.base or "").rstrip("/")
    if base:
        cands.add(base.lower())
    xfh = headers.get("X-Forwarded-Host")
    xfp = (headers.get("X-Forwarded-Proto") or "").split(",")[0].strip()
    host = headers.get("Host")
    if xfh:
        proto = xfp or "https"
        cands.add(("%s://%s" % (proto, xfh.split(",")[0].strip())).lower())
    if host:
        proto = xfp or cherrypy.request.scheme or "http"
        cands.add(("%s://%s" % (proto, host)).lower())
    return cands


def _checkSameOrigin():
    """Garde anti-CSRF, UNIQUEMENT pour l'auth par cookie.

    Une requête authentifiée par token explicite (header/param) n'est pas exposée au CSRF
    (un site tiers ne peut pas forger ce header) → on ne vérifie l'Origin que pour le cookie.
    """
    if "Girder-Token" in cherrypy.request.headers or "token" in cherrypy.request.params:
        return  # auth par token → sûr
    origin = (cherrypy.request.headers.get("Origin") or "").rstrip("/").lower()
    if not origin or origin not in _expectedOrigins():
        raise AccessException("Requête refusée : origine non autorisée (CSRF).")


def _userInfo(user):
    name = " ".join(filter(None, [user.get("firstName"), user.get("lastName")])) or user["login"]
    return {"id": str(user["_id"]), "login": user["login"], "name": name}


class DmfResource(Resource):
    def __init__(self):
        super().__init__()
        self.resourceName = "dmf"
        self.route("GET", ("config",), self.getConfig)
        self.route("GET", ("user",), self.getUser)
        self.route("GET", ("item", ":id", "files"), self.getFiles)
        self.route("GET", ("item", ":id", "dicom"), self.getDicom)
        self.route("GET", ("item", ":id", "annotations"), self.getItemAnnotations)
        # Collection d'annotations : liste filtrée + CRUD unitaire.
        self.route("GET", ("annotation",), self.listAnnotations)
        self.route("POST", ("annotation",), self.createAnnotation)
        self.route("PUT", ("annotation", ":key"), self.updateAnnotation)
        self.route("DELETE", ("annotation", ":key"), self.deleteAnnotation)
        self.route("GET", ("file", ":id"), self.downloadFile)
        self.route("POST", ("reprocess",), self.reprocess)

    # --- Configuration publique --------------------------------------------

    @access.public
    @autoDescribeRoute(Description("Configuration publique du plugin (chemin du viewer)."))
    def getConfig(self):
        # Lu par `girder-link.js`, qui est servi depuis /plugin_static/... et ne peut donc
        # plus déduire l'adresse du viewer de sa propre balise <script>.
        return {"viewerPath": Setting().get(PluginSettings.VIEWER_PATH) or "/dmf"}

    # --- Lectures item -----------------------------------------------------

    @access.user(cookie=True)
    @autoDescribeRoute(Description("Identité de l'utilisateur courant (id/login/name)."))
    def getUser(self):
        return _userInfo(getCurrentUser())

    @access.user(cookie=True)
    @autoDescribeRoute(
        Description("Fichiers de l'item, DANS L'ORDRE DES COUPES.").modelParam(
            "id", model=Item, level=AccessType.READ
        )
    )
    def getFiles(self, item):
        ordered = (item.get("dicom") or {}).get("files")
        if ordered:
            return [{"id": str(f["_id"]), "name": f.get("name")} for f in ordered]
        return [{"id": str(f["_id"]), "name": f["name"]} for f in Item().childFiles(item)]

    @access.user(cookie=True)
    @autoDescribeRoute(
        Description("Métadonnées DICOM communes de l'item (vide si non-DICOM).").modelParam(
            "id", model=Item, level=AccessType.READ
        )
    )
    def getDicom(self, item):
        return (item.get("dicom") or {}).get("meta", {})

    @access.user(cookie=True)
    @autoDescribeRoute(
        Description("Mesures de l'item (raccourci = liste filtrée par item).").modelParam(
            "id", model=Item, level=AccessType.READ
        )
    )
    def getItemAnnotations(self, item):
        return [Annotation().toMeasurement(a) for a in Annotation().listForItem(item["_id"])]

    # --- Collection d'annotations -----------------------------------------

    @access.user(cookie=True)
    @autoDescribeRoute(
        Description("Liste les annotations (interrogeable par item / dossier / type / créateur).")
        .param("itemId", "Filtrer par item (accès vérifié).", required=False)
        .param("folderId", "Filtrer par dossier — items du dossier et sous-dossiers "
               "accessibles (accès vérifié).", required=False)
        .param("type", "Filtrer par type de mesure.", required=False)
        .param("creatorId", "Filtrer par créateur.", required=False)
        .pagingParams(defaultSort="created")
    )
    def listAnnotations(self, itemId, folderId, type, creatorId, limit, offset, sort):
        user = getCurrentUser()
        query = {}
        if itemId:
            item = Item().load(itemId, level=AccessType.READ, user=user, exc=True)  # 403 si pas d'accès
            query["itemId"] = item["_id"]
        elif folderId:
            folder = Folder().load(folderId, level=AccessType.READ, user=user, exc=True)
            query["itemId"] = {"$in": _folderItemIds(folder, user)}
        elif not user.get("admin"):
            # Requête transverse sans portée : on restreint au créateur courant (sûr).
            query["creatorId"] = user["_id"]
        if type:
            query["type"] = type
        if creatorId:
            query["creatorId"] = creatorId
        cursor = Annotation().find(query, offset=offset, limit=limit, sort=sort)
        return [Annotation().toMeasurement(a) for a in cursor]

    @access.user(cookie=True)
    @autoDescribeRoute(
        Description("Crée une annotation sur un item.")
        .param("itemId", "Item cible (accès WRITE requis).")
        .jsonParam("annotation", "La mesure (format client).", requireObject=True, paramType="body")
    )
    def createAnnotation(self, itemId, annotation):
        _checkSameOrigin()
        user = getCurrentUser()
        item = Item().load(itemId, level=AccessType.WRITE, user=user, exc=True)
        doc = Annotation().fromMeasurement(annotation, item, user)
        return Annotation().toMeasurement(Annotation().save(doc))

    @access.user(cookie=True)
    @autoDescribeRoute(
        Description("Met à jour une annotation (par sa clé client).")
        .param("key", "Identifiant client de l'annotation.", paramType="path")
        .jsonParam("patch", "Champs à modifier.", requireObject=True, paramType="body")
    )
    def updateAnnotation(self, key, patch):
        _checkSameOrigin()
        user = getCurrentUser()
        annot = Annotation().findOne({"key": key})
        if not annot:
            raise RestException("Annotation introuvable.", code=404)
        Item().load(annot["itemId"], level=AccessType.WRITE, user=user, exc=True)
        for field in ("geometry", "values", "label", "frameIndex"):
            if field in patch:
                annot[field] = patch[field]
        return Annotation().toMeasurement(Annotation().save(annot))

    @access.user(cookie=True)
    @autoDescribeRoute(
        Description("Supprime une annotation (par sa clé client).").param(
            "key", "Identifiant client de l'annotation.", paramType="path"
        )
    )
    def deleteAnnotation(self, key):
        _checkSameOrigin()
        user = getCurrentUser()
        annot = Annotation().findOne({"key": key})
        if not annot:
            return  # idempotent
        Item().load(annot["itemId"], level=AccessType.WRITE, user=user, exc=True)
        Annotation().remove(annot)

    # --- Maintenance (backfill) -------------------------------------------

    @access.admin
    @autoDescribeRoute(
        Description(
            "Retraite les items DICOM existants (extraction métadonnées + tri). "
            "Backfill des items uploadés avant l'installation du plugin. Admin uniquement."
        ).param(
            "folderId",
            "Limiter à un dossier (récursif). Sinon : tous les items.",
            required=False,
        )
    )
    def reprocess(self, folderId):
        _checkSameOrigin()
        user = getCurrentUser()
        if folderId:
            folder = Folder().load(folderId, level=AccessType.WRITE, user=user, exc=True)
            items = (Item().load(iid, force=True) for iid in _folderItemIds(folder, user))
        else:
            items = Item().find({})
        scanned = processed = 0
        for item in items:
            if item is None:
                continue
            scanned += 1
            if processItem(item):
                processed += 1
        return {"scanned": scanned, "dicomItems": processed}

    # --- Pixels ------------------------------------------------------------

    @access.user(cookie=True)
    @autoDescribeRoute(
        Description("Télécharge le fichier (pixels DICOM), avec contrôle d'accès.").modelParam(
            "id", model=File, level=AccessType.READ
        )
    )
    def downloadFile(self, file):
        return File().download(file)
