# dicom-measure-flow

Outil web d'annotation/mesure d'imagerie DICOM, adossé à un serveur **Girder** qui
sert de back-end (stockage, authentification, métadonnées).

## Objectif

Permettre à des *reviewers* d'ouvrir des images DICOM stockées sur Girder et d'y poser
des mesures simples, persistées comme métadonnées Girder de l'examen, et ré-affichées
en overlay aux visites suivantes.

Cible de **première intention : imagerie 2D** (fluoroscopie, coupe de scanner).
Le support volumique/3D complet n'est pas un objectif initial mais l'architecture ne
doit pas le rendre impossible.

## Types de mesures (MVP)

1. **Distance** — clic début / clic fin. Affichée en **pixels** et en **mm** lorsque
   l'échelle est disponible dans les métadonnées (voir « Échelle » plus bas).
2. **Position absolue** — un point unique `(x, y)`.
3. **Niveau** — ligne horizontale (donne une position `y`) ou verticale (position `x`),
   interface en crosshair / ligne guide.

## Fonctionnalités

- Overlay des mesures existantes au ré-ouverture du fichier.
- Affichage plein écran.
- DICOM multi-slice : une mesure posée sur une slice adjacente s'affiche à **30 %
  d'opacité** sur la slice courante (slice ±1 ; au-delà, masquée).

## Architecture (décisions)

- **Girder = back-end uniquement** : stockage des fichiers, auth, API REST, métadonnées.
  On ne dépend PAS de son front-end legacy (Backbone/Pug).
- **Front-end = SPA Vite + React + Cornerstone3D**
  (`@cornerstonejs/core` + `@cornerstonejs/tools` + `@cornerstonejs/dicom-image-loader`),
  **packagée et déployée comme plugin Girder** (montage hybride, cf. ci-dessous). Pas de
  méta-framework ni de state lib lourde : React + Cornerstone + un client REST Girder
  maison. Cornerstone fournit déjà Length/Probe/crosshair et la conversion px→mm.
- **Montage hybride (décision clé) :**
  - **Dev** : SPA autonome, `vite dev` + proxy vers un Girder de dev.
  - **Prod** : un **plugin Girder sert le `dist/` en statique** (même origine garantie) et
    ajoute un **lien « Annoter » dans la vue item**. C'est le SEUL contact avec le client
    legacy Backbone — quelques lignes, pas l'app.
  - On NE construit PAS de vues Backbone/Pug : le plugin sert un bundle React prébuild, il
    ne s'intègre pas au pipeline de build web-client de Girder.
- **Bundle PORTABLE (pas de chemin codé en dur) :** Vite `base: './'` (assets relatifs), la
  SPA déduit la racine de l'API de son URL (`deriveApiRoot`, retire le segment de montage →
  gère racine ET préfixe de reverse-proxy), et `girder-link.js` déduit l'URL du viewer de sa
  propre balise `<script>`. → le chemin de montage est libre (réglage `dmf.viewer_path`, défaut
  `/dmf`) sans rebuild. Contrainte : montage sur **un seul segment** de chemin.
- **Injection du script vue item — deux voies :** au **build Docker** (balise `<script>` dans
  l'index Girder) pour l'image fournie ; via **`sub_filter` du reverse-proxy** (nginx) pour
  une install pip sur un Girder existant. La SPA reste accessible même sans injection
  (`/dmf/?itemId=…`).
- **Transport des pixels = API REST Girder, PAS DICOMweb.** Girder n'est pas un serveur
  DICOMweb ; les DICOM sont des *fichiers* dans des *items*. On télécharge le fichier
  (`GET /api/v1/file/:id/download`) et Cornerstone le parse côté client via le loader
  `wadouri`. DICOMweb (Orthanc/dcm4chee) n'est envisagé qu'en cas de besoin volumique
  lourd ultérieur — il ajouterait un service + une 2e auth.
- **Plugin Girder `dicom_viewer` = OPTIONNEL, pas une dépendance.** La SPA parse le DICOM
  **côté client** (loader `wadouri` → tous les tags : PixelSpacing, UID, etc.) et n'utilise
  que des routes du **cœur** de Girder (`user/me`, `item/:id/files`, `file/:id/download`,
  `item/:id/metadata`). Elle fonctionne donc `dicom_viewer` absent. Son seul apport perdu
  est le **tri serveur des fichiers par series/instance**. Direction retenue pour le
  compenser : un **hook d'upload dans NOTRE plugin** (lit `InstanceNumber` via pydicom, le
  stocke en métadonnée → le client trie dessus, sans parsing lourd côté client) ; repli =
  tri 100% client. Voir la section fork.

### Authentification — héritée de la session Girder (pas de couche login)

Comme l'app est servie **par Girder, en même origine** (montage hybride), l'utilisateur
arrive **déjà authentifié** : la session Girder existe avant d'entrer dans la SPA. On ne
construit **aucun écran de login**, ni les portes OIDC/table-based — Girder s'en charge en
amont. Population mixte (plugin OIDC custom activé pour certains, login classique pour les
autres) : transparent pour la SPA, qui ne voit qu'une session.

- **Auth durcie via routes dédiées (option 1) — AUCUN token en JS.** La SPA n'appelle pas
  l'API Girder générique mais des routes plugin **`/api/v1/dmf/*`** (`user`, `item/:id/files`,
  `item/:id/annotations`, `file/:id`) déclarées en **auth cookie** (`@access.user(cookie=True)`).
  Les lectures (sans effet de bord) sont sûres en cookie ; l'écriture (`PUT .../annotations`)
  autorise le cookie MAIS est protégée du CSRF par une **vérification d'Origin** côté serveur
  (l'Origin doit matcher l'hôte). Les routes vérifient l'accès Girder READ/WRITE de l'item.
- **Cornerstone** : `beforeSend: xhr => { xhr.withCredentials = true }` → le cookie part avec
  le fetch des pixels (`/api/v1/dmf/file/:id`). `credentials: 'include'` côté client REST.
- **Mode dev** (`vite dev`, cross-origin) : pas de cookie → `VITE_GIRDER_TOKEN` envoyé en
  header `Girder-Token` (accepté par les routes /dmf ; la vérif d'Origin est sautée quand
  l'auth est par token, qui est intrinsèquement non-CSRF).
- **Lien « Annoter »** dans la vue item Girder : un script (`/dmf/girder-link.js`) injecté
  dans l'index du client web Girder **au build Docker** ajoute un bouton flottant vers
  `/dmf/?itemId=<id>` (pur DOM, pas de rebuild du client Girder).

### Déploiement

- **`docker-compose`** : MongoDB + Girder (image incluant le plugin + la SPA embarquée).
  Girder sert lui-même le bundle → mono-origine par construction, pas de nginx pour le front.
  `scripts/girder-init.sh` provisionne le compte admin + un assetstore au 1er démarrage.
- Build : `Dockerfile` multi-étapes — `vite build` puis `dist/` copié dans `web_dist/` du
  plugin, `pip install` du plugin, et **injection** d'une balise `<script>` (girder-link.js)
  dans l'index du client Girder.

**Notes Girder 5 (vérifiées en local) :**
- L'image `girder/girder:latest` est **Girder 5**, **amd64 uniquement** → `platform:
  linux/amd64` (émulée sur Apple Silicon).
- Serveur **ASGI** : monter la SPA sur **`info['serverRoot']`** (l'arbre cherrypy réellement
  servi), PAS sur le `cherrypy.tree` global (sinon 404).
- Bind par défaut `127.0.0.1` → `command: ["--host", "0.0.0.0"]` indispensable.
- Plugins installés (entry point `girder.plugin`) chargés automatiquement ; `GIRDER_MONGO_URI`
  pour la connexion Mongo.

### Reverse-proxy & backfill (traités)

- **Reverse-proxy / CSRF** : la garde d'Origin (`_checkSameOrigin`) accepte l'hôte public via
  `X-Forwarded-Host`/`X-Forwarded-Proto` en plus de `cherrypy.request.base` (cf.
  `_expectedOrigins`). Le proxy doit donc émettre ces en-têtes, et le plugin ne doit être
  joignable QUE via le proxy (sinon usurpation possible de `X-Forwarded-*`).
- **Backfill** : `POST /api/v1/dmf/reprocess[?folderId=]` (admin) retraite les items DICOM
  existants (extraction + tri) → couvre les uploads antérieurs à l'install. `processItem`
  reconstruit `item['dicom']` from scratch.

### Limitations connues / TODO

- **Scope dossier** (`?folderId=`) : implémenté via `$in` sur les itemIds — suffisant pour des
  études normales ; pour des dossiers de milliers d'items, envisager un `folderId` dénormalisé
  sur l'annotation.
- **Rendu CPU** (`?cpu`) : échappatoire pour environnements headless (le readback WebGL ressort
  noir) ; n'y dessine pas les overlays d'annotations. GPU par défaut en usage réel.

## Échelle (px → mm) — IMPORTANT

- Coupes transverses (scanner) : `PixelSpacing (0028,0030)` → conversion mm fiable.
- Fluoroscopie / projection : souvent seulement `ImagerPixelSpacing (0018,1164)`, qui est
  la taille de pixel **au détecteur**, pas au niveau du patient. Une distance « en mm »
  calculée ainsi est entachée du facteur de magnification → **afficher la mesure mais
  signaler que l'échelle est au plan détecteur** (ne pas la présenter comme une vraie
  dimension anatomique). Cornerstone renvoie `NaN`/px quand `PixelSpacing` est absent.
- Toujours **dégrader proprement vers les pixels** quand aucune info d'échelle n'est
  exploitable, et stocker quelle source de spacing a été utilisée.

## Modèle de données des annotations

**Stockage : collection Girder dédiée `dmf_annotation`** (cf. `plugin/.../models.py`), un
document par mesure référençant l'item (`itemId`). Interrogeable par l'API via
`/api/v1/dmf/annotation` (liste filtrée `itemId`/`type`/`creatorId`, paginée) + CRUD unitaire
par `key` (= `id` client). Index Mongo sur `itemId`/`creatorId`/`type`/`key`. Le `user` et la
date sont **estampillés serveur** (utilisateur authentifié), pas pris du client.
> Historique : les mesures vivaient dans `item.meta.annotations` ; une migration one-way au
> chargement du plugin les déplace vers la collection et retire la clé `meta.annotations`.

Format renvoyé/attendu côté client (la `key` du document = `id`) :

```jsonc
{
  "id": "uuid",
  "type": "distance | point | level-h | level-v",
  "geometry": { /* coords en pixels image : {start,end} | {x,y} | {x} | {y} */ },
  "values": {
    "lengthPx": 0,            // distance
    "lengthMm": null,         // null si pas d'échelle exploitable
    "positionPx": null,
    "spacingSource": "PixelSpacing | ImagerPixelSpacing | none"
  },
  "frameIndex": 0,            // slice/frame dans un multi-frame
  "sopInstanceUID": "...",    // identifie l'image annotée
  "seriesInstanceUID": "...",
  "label": "",               // commentaire optionnel
  "user": { "id": "...", "login": "...", "name": "..." },
  "createdAt": "ISO-8601",
  "appVersion": "..."         // version de l'outil ayant produit la mesure
}
```

Conventions : géométrie toujours en **coordonnées pixel image** (indépendantes du zoom/pan),
horodatage en UTC ISO-8601. Persistance **unitaire** (POST/PUT/DELETE par mesure), plus de
réécriture de liste globale.

## Conventions repo

- **Licence** : Apache-2.0 (`LICENSE`). **`test_data/` est gitignoré** (PHI potentielle) —
  fournir ses propres DICOM anonymisés/synthétiques.
- **Qualité (CI GitHub Actions, `.github/workflows/ci.yml`)** :
  - SPA (`web/`) : `npm run lint` (ESLint flat config `eslint.config.js`), `npm run typecheck`,
    `npm test` (Vitest, jsdom), `npm run build`.
  - Plugin (`plugin/`) : `py_compile` + `pytest` (`tests/`, déps `pip install .[test]`).
- **Tests** : côté plugin, les fonctions DICOM pures sont isolées dans `dicom_tags.py` (sans
  dépendance Girder/Mongo) → testables avec pydicom seul. Côté SPA, helpers `measurements` et
  `store` (Cornerstone/girder mockés).
- **Raccourcis outils** : **touche simple** (D/P/H/V), sans modificateur (évite le conflit
  ⌘H = masquer sur macOS) ; ignorés si modificateur pressé ou focus dans un champ.
- **Test standalone sans Girder** : données dans `test_data/<étude>/*.dcm`, ouvrir
  `?standalone=CT` (annotations en `localStorage`). `&cpu` force le rendu CPU (headless).
- **Pièges Vite + Cornerstone** (à ne pas réintroduire) :
  - `optimizeDeps` (cf. `web/vite.config.ts`) : EXCLURE `@cornerstonejs/dicom-image-loader`
    (sinon l'URL du worker de décodage `new Worker(new URL('./...', import.meta.url))` ne
    résout plus → décodage bloqué) MAIS INCLURE les sous-chemins codecs `.../decodewasmjs`
    (glue CJS → sinon « does not provide an export named default » au chargement).
  - Pas de `React.StrictMode` (`web/src/main.tsx`) : le double-montage des effets en dev
    détruit/recrée le `RenderingEngine` pendant le `setStack` async → canvas 0×0.
