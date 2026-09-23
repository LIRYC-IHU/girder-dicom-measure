// Défilement des coupes à la molette / au trackpad.
//
// Pourquoi ne pas utiliser `StackScrollTool` : son handler molette avance d'UNE coupe par
// événement `wheel`, quel que soit le `deltaY`. Une souris à crans envoie un événement par
// cran — correct ; un trackpad envoie des dizaines d'événements de quelques pixels pour un
// seul glissement (inertie comprise) → la série défile d'un bout à l'autre au moindre geste.
//
// On accumule donc les pixels et on n'avance qu'au franchissement d'un seuil, tout en
// conservant « un cran = une coupe » pour les molettes à crans.

/** Pixels de défilement à accumuler pour avancer d'une coupe (gestes fins : trackpad). */
export const PIXELS_PER_SLICE = 40;
/** Au-delà, l'événement est lu comme un cran de molette discret → exactement une coupe. */
export const COARSE_DELTA = 40;

const LINE_PX = 16; // deltaMode === 1 (lignes)
const PAGE_PX = 400; // deltaMode === 2 (pages)

/**
 * Fabrique un accumulateur : à chaque événement molette, rend le nombre de coupes à avancer
 * (0 le plus souvent avec un trackpad). L'état est privé à l'instance → une par viewport.
 */
export function createWheelStepper(
  pixelsPerSlice = PIXELS_PER_SLICE,
  coarseDelta = COARSE_DELTA,
): (deltaY: number, deltaMode?: number) => number {
  let accumulated = 0;

  return (deltaY: number, deltaMode = 0): number => {
    const px = deltaMode === 1 ? deltaY * LINE_PX : deltaMode === 2 ? deltaY * PAGE_PX : deltaY;
    if (!px) return 0;

    // Cran de molette (ou geste franc) : réponse immédiate, sans accumulation résiduelle.
    if (Math.abs(px) >= coarseDelta) {
      accumulated = 0;
      return Math.sign(px);
    }
    // Changement de sens : on repart de zéro plutôt que d'annuler le geste en cours.
    if (accumulated && Math.sign(px) !== Math.sign(accumulated)) accumulated = 0;

    accumulated += px;
    const steps = Math.trunc(accumulated / pixelsPerSlice) || 0; // `|| 0` : évite un -0
    if (steps) accumulated -= steps * pixelsPerSlice;
    return steps;
  };
}
