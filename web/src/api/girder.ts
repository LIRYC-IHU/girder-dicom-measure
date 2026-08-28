// Client REST Girder — routes DÉDIÉES du plugin (/api/v1/dmf/*).
//
// Auth durcie (option 1) : aucun token n'est exposé dans la page en prod. La SPA, servie
// même origine par Girder, s'authentifie via le COOKIE de session (`credentials: 'include'`)
// sur les routes /dmf (qui acceptent le cookie). L'écriture est protégée côté serveur par
// une vérification d'Origin (CSRF).
//
// En DEV (`vite dev`, cross-origin), le cookie n'est pas disponible → on envoie le token
// `VITE_GIRDER_TOKEN` en header `Girder-Token` (accepté par les routes /dmf).

import type { MeasurementUser } from '../annotations/types';

/**
 * Racine de l'API dérivée de l'URL de la page (aucune config) :
 * la SPA est servie sous `<racinePublique>/<segment>/` → l'API Girder est à
 * `<racinePublique>/api/v1/dmf`. On retire donc le dernier segment du chemin courant.
 * Gère la racine (`/dmf/` → `/api/v1/dmf`) ET un préfixe de reverse-proxy
 * (`/girder/dmf/` → `/girder/api/v1/dmf`). En dev (page à `/`) → `/api/v1/dmf` (proxifié).
 * NB : le viewer doit être monté sur UN seul segment de chemin.
 */
function deriveApiRoot(): string {
  const segs = window.location.pathname.split('/').filter(Boolean);
  segs.pop(); // retire le segment de montage du viewer
  const root = segs.length ? `/${segs.join('/')}/` : '/';
  return `${root}api/v1/dmf`;
}

const API = deriveApiRoot();

/** Token de DEV uniquement (cross-origin). En prod : undefined → auth par cookie. */
export const girderToken: string | undefined = import.meta.env.VITE_GIRDER_TOKEN;

async function req<T = unknown>(path: string, opts: RequestInit = {}): Promise<T> {
  const headers = new Headers(opts.headers);
  if (girderToken) headers.set('Girder-Token', girderToken);

  const res = await fetch(`${API}${path}`, { credentials: 'include', ...opts, headers });
  if (!res.ok) {
    throw new Error(`Girder ${opts.method ?? 'GET'} ${path} → ${res.status} ${res.statusText}`);
  }
  const ct = res.headers.get('content-type') ?? '';
  return (ct.includes('application/json') ? res.json() : (res as unknown)) as T;
}

interface DmfFile {
  id: string;
  name: string;
  /** Frames adressables séparément. 1 = télécharger le fichier entier (cas courant) ;
   *  > 1 = boucle multi-frame livrée frame par frame (le serveur transcode à la demande,
   *  la première image s'affiche sans attendre l'encodage de toute la boucle). */
  frames?: number;
}

/** Mode de recompression des pixels appliqué par le serveur avant envoi. */
export type CompressionMode = 'none' | 'lossless' | 'lossy';

/** Configuration publique (`GET /dmf/config`) — lisible sans être authentifié. */
export interface DmfConfig {
  viewerPath: string;
  compression: CompressionMode;
  lossyRatio: number;
}


export const girder = {
  /** Identité de l'utilisateur courant (déjà au format des annotations). */
  me: () => req<MeasurementUser>('/user'),

  /** Configuration publique (chemin du viewer, compression des pixels). */
  config: () => req<DmfConfig>('/config'),

  /** URL des pixels d'un fichier (consommée par Cornerstone via wadouri:). */
  fileDownloadUrl: (fileId: string) =>
    `${API}/file/${fileId}${girderToken ? `?token=${girderToken}` : ''}`,

  /** URL d'UNE frame, servie comme un DICOM mono-frame indépendant. */
  frameDownloadUrl: (fileId: string, index: number) =>
    `${API}/file/${fileId}/frame/${index}${girderToken ? `?token=${girderToken}` : ''}`,

  /**
   * URLs du stack, DANS L'ORDRE DES COUPES (tri serveur, repli côté serveur si absent).
   * Une boucle multi-frame est développée ici en une URL par frame : chacune se télécharge
   * et s'affiche indépendamment, au lieu d'attendre le fichier entier.
   */
  async orderedFileUrls(itemId: string): Promise<string[]> {
    const files = await req<DmfFile[]>(`/item/${itemId}/files`);
    return files.flatMap((f) =>
      (f.frames ?? 1) > 1
        ? Array.from({ length: f.frames! }, (_, i) => girder.frameDownloadUrl(f.id, i))
        : [girder.fileDownloadUrl(f.id)],
    );
  },

  /** Mesures d'un item (collection `dmf_annotation`). */
  listAnnotations: <A = unknown>(itemId: string) =>
    req<A[]>(`/annotation?itemId=${encodeURIComponent(itemId)}`),

  createAnnotation: <A = unknown>(itemId: string, measurement: A) =>
    req(`/annotation?itemId=${encodeURIComponent(itemId)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(measurement),
    }),

  updateAnnotation: <A = unknown>(key: string, patch: Partial<A>) =>
    req(`/annotation/${encodeURIComponent(key)}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    }),

  deleteAnnotation: (key: string) =>
    req(`/annotation/${encodeURIComponent(key)}`, { method: 'DELETE' }),
};
