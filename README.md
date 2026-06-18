# dicom-measure-flow

Outil web d'**annotation et de mesure d'imagerie DICOM 2D** (fluoroscopie, coupes de
scanner), adossé à un serveur [**Girder**](https://girder.readthedocs.io). Des *reviewers*
ouvrent une image stockée dans Girder, y posent des mesures, et les retrouvent en overlay
aux visites suivantes. Les mesures sont enregistrées dans Girder et **interrogeables par API**.

## Fonctionnalités

- **Mesures** : distance (en pixels et en mm), position (point *x, y*), niveaux horizontal
  et vertical (lignes guides).
- **Échelle px → mm** via `PixelSpacing`. En projection (fluoroscopie), l'échelle est au plan
  détecteur : la mesure est affichée mais signalée comme **non anatomique** (magnification).
- **Overlay** des mesures existantes à la réouverture ; les mesures des coupes voisines
  (jusqu'à ±3) s'affichent en transparence dégressive.
- **Multi-coupes** et **multiframe** (cine) ; préchargement en arrière-plan → défilement fluide.
- Plein écran, navigation à la molette **ou** aux flèches, raccourcis clavier.
- **Intégration Girder** : extraction automatique des métadonnées DICOM à l'upload, tri des
  coupes par numéro d'instance, panneaux « Métadonnées DICOM » et « Mesures » et lien
  « Annoter » directement dans la vue item.
- **Pas d'écran de login** : l'authentification est héritée de la session Girder.

## Démarrage rapide (Docker)

```bash
docker compose up --build
```

Girder est alors disponible sur **http://localhost:8080**. Au premier démarrage, un compte
**`admin` / `password`** et un espace de stockage sont créés automatiquement.

> L'image Girder officielle est `amd64` ; sur Apple Silicon elle tourne en émulation
> (déjà géré par le `docker-compose`).

Pour arrêter : `docker compose down` (ajouter `-v` pour repartir d'une base vierge).

## Installation sur un Girder existant (sans Docker)

Pour ajouter l'outil à un Girder déjà déployé sur la machine, derrière un reverse-proxy
(nginx, Traefik…).

**1. Construire et installer le plugin** (dans l'environnement Python de Girder) :

```bash
make build                 # compile la SPA et l'embarque dans plugin/.../web_dist/
pip install ./plugin       # installe le plugin (+ pydicom) dans le venv de Girder
# puis redémarrer Girder
```

Le plugin sert alors la SPA sous **`/dmf`** (configurable, voir plus bas) et expose les
routes `/api/v1/dmf/*`. L'extraction des métadonnées DICOM et le tri des coupes sont actifs
pour les nouveaux uploads (pour l'existant : `POST /api/v1/dmf/reprocess`, cf. plus bas).

**2. Configurer le reverse-proxy.** Deux points importants :

- Transmettre l'hôte public via `X-Forwarded-Host` / `X-Forwarded-Proto` — **requis** pour
  la protection CSRF des écritures (l'outil compare l'`Origin` à l'hôte public).
- Injecter le script d'intégration de la vue item (panneaux « Métadonnées DICOM » /
  « Mesures » + lien « Annoter ») via `sub_filter` — l'installation pip ne modifie pas le
  client Girder.

Exemple **nginx** (Girder servi à la racine du domaine) :

```nginx
server {
    listen 443 ssl;
    server_name imaging.example.com;
    # ... certificats TLS ...

    location / {
        proxy_pass http://127.0.0.1:8080;        # Girder local
        proxy_set_header Host              $host;
        proxy_set_header X-Forwarded-Host  $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;

        # WebSocket (notifications Girder)
        proxy_http_version 1.1;
        proxy_set_header Upgrade    $http_upgrade;
        proxy_set_header Connection "upgrade";

        # Intégration vue item : injecte le script (désactive gzip pour sub_filter)
        proxy_set_header Accept-Encoding "";
        sub_filter '</body>' '<script defer src="/dmf/girder-link.js"></script></body>';
        sub_filter_once on;
    }
}
```

> La SPA et l'API doivent rester sur la **même origine** que Girder (cookie de session).
> Le viewer reste accessible même sans le `sub_filter` (ouvrir directement `/dmf/?itemId=…`).

**3. (Optionnel) Changer le chemin du viewer.** Réglage Girder `dmf.viewer_path` (défaut
`/dmf`, un seul segment de chemin) — la SPA a une base relative et déduit l'API de son URL,
donc aucun rebuild n'est nécessaire. Penser à faire pointer la balise `<script>` du
`sub_filter` vers ce même chemin. Prend effet au redémarrage de Girder.

```bash
curl -H "Girder-Token: $TOKEN" -X PUT "$GIRDER/api/v1/system/setting" \
  --data-urlencode "key=dmf.viewer_path" --data-urlencode 'value="/annotate"'
```

## Utilisation (reviewer)

1. Se connecter à Girder sur http://localhost:8080.
2. Téléverser un ou plusieurs fichiers DICOM dans un *item* (les fichiers d'une même
   acquisition vont dans le même item).
3. Ouvrir l'item : la page affiche les **métadonnées DICOM**, la liste des **mesures** déjà
   présentes, et un lien **« Annoter »**.
4. Cliquer sur **« Annoter »** pour ouvrir le viewer.
5. Choisir un outil et mesurer :

   | Outil      | Raccourci      | Geste                         |
   | ---------- | -------------- | ----------------------------- |
   | Distance   | `D`            | clic début, clic fin          |
   | Position   | `P`            | un clic                       |
   | Niveau H   | `H`            | un clic (ligne horizontale)   |
   | Niveau V   | `V`            | un clic (ligne verticale)     |
   | Déplacer / Zoom | —         | boutons de la barre d'outils  |

- **Molette** ou **↑ / ↓** : changer de coupe.
- Panneau de droite : liste des mesures (label éditable, suppression, clic pour aller à la
  coupe correspondante) et infos DICOM de la coupe courante.
- **Plein écran** ; **← Girder** pour revenir à l'item.

Les mesures sont enregistrées automatiquement. À la réouverture, celles déjà enregistrées
apparaissent en **jaune**, celles créées pendant la session en **vert**.

## API des mesures (administrateurs / intégrations)

Les mesures sont stockées dans une collection dédiée et exposées sous `/api/v1/dmf` :

| Méthode  | Route                                   | Effet                                              |
| -------- | --------------------------------------- | -------------------------------------------------- |
| `GET`    | `/dmf/annotation?itemId=<id>`           | mesures d'un examen                                |
| `GET`    | `/dmf/annotation?folderId=<id>`         | mesures d'un dossier (items + sous-dossiers)       |
| `GET`    | `/dmf/annotation`                       | mes mesures (toutes, pour un administrateur)       |
| `GET`    | `…&type=distance&creatorId=<id>`        | filtres combinables ; paginé (`limit`/`offset`/`sort`) |
| `POST`   | `/dmf/annotation?itemId=<id>`           | crée une mesure (corps JSON)                       |
| `PUT`    | `/dmf/annotation/<key>`                 | met à jour (corps = champs modifiés)               |
| `DELETE` | `/dmf/annotation/<key>`                 | supprime                                           |

Authentification : **cookie de session** (même origine) ou en-tête `Girder-Token`. L'accès
est vérifié via l'examen/dossier (droits Girder). Les écritures exigent une `Origin` valide
(protection CSRF, compatible reverse-proxy via `X-Forwarded-*`). L'utilisateur et
l'horodatage sont fixés par le serveur.

**Maintenance (admin)** — pour indexer des examens DICOM téléversés *avant* l'installation
du plugin (extraction des métadonnées + tri des coupes) :

```bash
# Tous les examens, ou un dossier précis (récursif). Le -d '' évite une erreur 411.
curl -H "Girder-Token: $TOKEN" -X POST -d '' "$GIRDER/api/v1/dmf/reprocess"
curl -H "Girder-Token: $TOKEN" -X POST -d '' "$GIRDER/api/v1/dmf/reprocess?folderId=$FOLDER"
```

```bash
# Toutes les distances d'un examen (avec un token Girder)
curl -H "Girder-Token: $TOKEN" \
  "http://localhost:8080/api/v1/dmf/annotation?itemId=$ITEM&type=distance"

# Toutes les mesures d'un dossier (récursif)
curl -H "Girder-Token: $TOKEN" \
  "http://localhost:8080/api/v1/dmf/annotation?folderId=$FOLDER"
```

Chaque mesure a la forme :

```jsonc
{
  "id": "…",
  "type": "distance | point | level-h | level-v",
  "geometry": { /* coordonnées en pixels image */ },
  "values": { "lengthPx": 0, "lengthMm": null, "spacingSource": "PixelSpacing | ImagerPixelSpacing | none" },
  "frameIndex": 0,
  "sopInstanceUID": "…",
  "seriesInstanceUID": "…",
  "label": "",
  "user": { "id": "…", "login": "…", "name": "…" },
  "createdAt": "ISO-8601"
}
```

## Déploiement en production

- Servir Girder derrière **HTTPS** (reverse-proxy). Veiller à ce que Girder voie l'hôte
  public (`X-Forwarded-*`) — nécessaire à la protection CSRF des écritures.
- Le plugin sert la SPA sous `/dmf` (même origine que Girder).
- Populations d'utilisateurs mixtes (OIDC et/ou login classique) : transparent pour l'outil,
  qui hérite simplement de la session Girder.

Détails d'architecture, de configuration et limitations connues : voir
[CLAUDE.md](CLAUDE.md).

## Développement

Architecture et décisions techniques : [CLAUDE.md](CLAUDE.md). En bref :

```bash
make install   # dépendances de la SPA (web/)
make dev       # serveur Vite sur http://localhost:5173 (proxy /api vers un Girder de dev)
make build     # compile la SPA et l'embarque dans le plugin
```

- SPA : **Vite + React + Cornerstone3D** (`web/`).
- Plugin Girder : **Python** (`plugin/`) — extraction DICOM, tri, routes API, service de la SPA.
- Lint / typecheck / tests : `cd web && npm run lint && npm run typecheck && npm test` ;
  côté plugin : `cd plugin && pytest`.
- **Mode autonome** (sans Girder) pour tester avec des fichiers locaux placés dans
  `test_data/<étude>/*.dcm` : ouvrir `http://localhost:5173/?standalone=CT`
  (les mesures sont alors stockées dans le `localStorage`).

> ⚠️ Le dossier `test_data/` n'est **pas** versionné (il peut contenir des données patient).
> Fournissez vos propres DICOM, de préférence **anonymisés** ou synthétiques.

## Licence

[Apache 2.0](LICENSE).
