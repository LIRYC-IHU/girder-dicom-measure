// Préchargement du stack en arrière-plan.
//
// Module à part : il ne dépend que du chargeur d'images, pas de l'initialisation de
// Cornerstone ni des outils — ce qui le rend testable sans monter toute la pile.

import { imageLoader } from '@cornerstonejs/core';

/**
 * Précharge TOUT le stack en arrière-plan (cache image Cornerstone) : une fois la coupe
 * courante affichée, le reste continue d'arriver, et le défilement ne touche plus le réseau.
 *
 * L'ordre est recalculé À CHAQUE créneau libéré, en repartant de la coupe RÉELLEMENT affichée
 * (`currentIndex`) : depuis que les boucles sont livrées frame par frame, le préchargement
 * dure quelques secondes, et un ordre figé au démarrage ferait attendre l'utilisateur qui
 * saute d'emblée au milieu de la série — ses voisines seraient en fin de file.
 *
 * La concurrence est bornée pour ne saturer ni les workers de décodage ni le serveur (qui
 * encode une frame par requête). `isAborted` coupe tout au démontage. Fire-and-forget : ne
 * bloque pas le rendu initial.
 */
export function prefetchStack(
  imageIds: string[],
  currentIndex: () => number,
  isAborted: () => boolean,
  concurrency = 6,
): void {
  const pending = new Set(imageIds.map((_, i) => i));

  const nearestPending = (): number => {
    const target = currentIndex();
    let best = -1;
    let bestDistance = Infinity;
    for (const index of pending) {
      const distance = Math.abs(index - target);
      if (distance < bestDistance) {
        bestDistance = distance;
        best = index;
      }
    }
    return best;
  };

  const pump = (): void => {
    if (isAborted() || pending.size === 0) return;
    const index = nearestPending();
    pending.delete(index);
    imageLoader
      .loadAndCacheImage(imageIds[index])
      .catch(() => undefined)
      .finally(pump);
  };
  for (let k = 0; k < Math.min(concurrency, imageIds.length); k++) pump();
}
