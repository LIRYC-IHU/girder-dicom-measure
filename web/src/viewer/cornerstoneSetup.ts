// Initialisation de Cornerstone3D : core + loader DICOM + outils.
//
// Point auth IMPORTANT (cf. CLAUDE.md) : le loader DICOM doit envoyer le cookie de session
// Girder (prod) ou le token de dev. On passe par `beforeSend` pour activer withCredentials
// et, en dev, ajouter le header Girder-Token.
//
// NB : l'API exacte d'init de @cornerstonejs/dicom-image-loader a bougé entre versions
// majeures — adapter `dicomImageLoaderInit(...)` à la version réellement installée.

import { init as coreInit, setUseCPURendering } from '@cornerstonejs/core';
import {
  init as toolsInit,
  addTool,
  LengthTool,
  ProbeTool,
  PanTool,
  ZoomTool,
  StackScrollTool,
} from '@cornerstonejs/tools';
import { init as dicomImageLoaderInit, wadouri } from '@cornerstonejs/dicom-image-loader';
import { girderToken } from '../api/girder';

let initialized = false;

export async function ensureCornerstoneInitialized(): Promise<void> {
  if (initialized) return;

  // Échappatoire dev : ?cpu force le rendu CPU (utile en environnement headless où le
  // readback WebGL ressort noir). NE PAS utiliser en prod (rendu GPU bien plus rapide).
  if (new URLSearchParams(window.location.search).has('cpu')) {
    setUseCPURendering(true);
  }

  await coreInit();
  await dicomImageLoaderInit({
    // Token de session (injecté en prod / dev) pour le fetch des pixels + withCredentials.
    beforeSend: (xhr: XMLHttpRequest) => {
      xhr.withCredentials = true;
      if (girderToken) xhr.setRequestHeader('Girder-Token', girderToken);
    },
  } as Parameters<typeof dicomImageLoaderInit>[0]);
  await toolsInit();

  // Outils MVP : distance (Length), position (Probe), navigation.
  // level-h / level-v : à brancher sur un outil dédié (cf. measurements.ts → TODO).
  addTool(LengthTool);
  addTool(ProbeTool);
  addTool(PanTool);
  addTool(ZoomTool);
  addTool(StackScrollTool);

  initialized = true;
}

/** Construit l'imageId Cornerstone (loader WADO-URI) à partir d'une URL de fichier Girder. */
export function wadoUriImageId(fileDownloadUrl: string): string {
  const absolute = new URL(fileDownloadUrl, window.location.origin).href;
  return `wadouri:${absolute}`;
}

/**
 * Construit la liste d'imageIds du stack à partir des URLs fournies.
 *  - Plusieurs URLs → un imageId chacune. C'est le cas d'une série de coupes (CT) ET celui
 *    d'une boucle multi-frame déjà développée par le serveur (une URL par frame) : chaque
 *    image se charge et s'affiche indépendamment.
 *  - Une seule URL → on sonde `NumberOfFrames` (0028,0008) et, si multiframe, on développe
 *    en un imageId par frame (`&frame=N`, 1-based). Ce repli couvre les sources DÉJÀ
 *    compressées, que le serveur ne découpe pas, et le mode autonome sans Girder. Il coûte
 *    le téléchargement du fichier entier avant le premier affichage.
 * Le dataset est mis en cache par le loader → pas de double téléchargement au décodage.
 * Suppose Cornerstone initialisé (ensureCornerstoneInitialized).
 */
export async function buildStackImageIds(fileUrls: string[]): Promise<string[]> {
  const bases = fileUrls.map(wadoUriImageId);
  if (bases.length !== 1) return bases; // série multi-fichiers : pas de sondage

  const url = new URL(fileUrls[0], window.location.origin).href;
  try {
    // load(uri, loadRequest?, imageId?) : les 2 derniers ont un défaut au runtime.
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const dataSet: any = await (wadouri.dataSetCacheManager.load as any)(url);
    const frames = Number(dataSet?.intString?.('x00280008')) || 1;
    if (frames > 1) {
      return Array.from({ length: frames }, (_, i) => `${bases[0]}&frame=${i + 1}`);
    }
  } catch {
    // Sondage impossible → on retombe sur un imageId unique.
  }
  return bases;
}
