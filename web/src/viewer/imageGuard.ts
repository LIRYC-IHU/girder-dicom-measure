// Garde « pas de mesure sur une image absente ».
//
// Quand une coupe ne se charge pas (objet DICOM sans pixels, 404, décodage impossible…),
// Cornerstone avance quand même l'index courant (`getCurrentImageId()` désigne la coupe en
// échec) mais laisse À L'ÉCRAN la dernière image chargée. Un outil de mesure posé à ce
// moment référence donc l'image en échec — son SOPInstanceUID, son index — avec une géométrie
// tracée sur une autre image : mesure non calibrable, et fausse. C'est ce qui s'est produit
// sur les états de présentation (PR) DEFINE-PFA empilés en coupe 1 par le plugin < 0.6.0.
//
// Module sans dépendance Cornerstone à l'exécution → testable seul.

/** Ce dont la garde a besoin du viewport (`StackViewport.getCornerstoneImage`, absent de
 *  l'interface `IStackViewport` publique). */
export interface DisplayedImageSource {
  getCornerstoneImage?: () => { imageId?: string } | undefined | null;
}

/**
 * L'image `imageId` est-elle RÉELLEMENT celle affichée ? Faux si elle a échoué, si elle est
 * encore en chargement, ou si aucun identifiant n'est fourni : dans tous ces cas, ce qui est à
 * l'écran n'est pas l'image que la mesure référencerait.
 */
export function isImageDisplayed(
  viewport: DisplayedImageSource,
  imageId: string | null | undefined,
): boolean {
  if (!imageId) return false;
  return viewport.getCornerstoneImage?.()?.imageId === imageId;
}

/** `imageId` d'un événement `IMAGE_LOAD_ERROR` (forme `{ imageId, imageIdIndex, error }`). */
export function failedImageId(detail: unknown): string | null {
  const id = (detail as { imageId?: unknown } | null | undefined)?.imageId;
  return typeof id === 'string' ? id : null;
}
