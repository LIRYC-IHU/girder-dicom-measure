// Préchargement du stack en arrière-plan.
//
// Module à part : il ne dépend que du chargeur d'images, pas de l'initialisation de
// Cornerstone ni des outils — ce qui le rend testable sans monter toute la pile.

import { imageLoader } from '@cornerstonejs/core';

/**
 * Précharge TOUT le stack en arrière-plan (cache image Cornerstone) : une fois la coupe
 * courante affichée, le reste continue d'arriver, et le défilement ne touche plus le réseau.
 *
 * L'ordre est SÉQUENTIEL (0, 1, 2, …) : le nombre d'images chargées est alors le préfixe
 * réellement disponible, ce qui rend le compteur de progression lisible (« 1/35/146 »).
 * Un ordre réorienté autour de la coupe regardée servait l'utilisateur qui saute au milieu
 * pendant le chargement, mais laissait des trous impossibles à résumer en un chiffre.
 *
 * La concurrence est bornée pour ne saturer ni les workers de décodage ni le serveur (qui
 * encode une frame par requête). `isAborted` coupe tout au démontage. Fire-and-forget : ne
 * bloque pas le rendu initial. `onProgress` reçoit le nombre d'images terminées (succès ou
 * échec : une image qui ne chargera jamais ne doit pas figer le compteur).
 */
export function prefetchStack(
  imageIds: string[],
  isAborted: () => boolean,
  onProgress?: (loaded: number) => void,
  concurrency = 6,
): void {
  let next = 0;
  let loaded = 0;

  const pump = (): void => {
    if (isAborted() || next >= imageIds.length) return;
    const index = next++;
    imageLoader
      .loadAndCacheImage(imageIds[index])
      .catch(() => undefined)
      .finally(() => {
        loaded++;
        if (!isAborted()) onProgress?.(loaded);
        pump();
      });
  };
  for (let k = 0; k < Math.min(concurrency, imageIds.length); k++) pump();
}
