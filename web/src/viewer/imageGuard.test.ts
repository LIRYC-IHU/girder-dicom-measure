// Garde : aucune mesure sur une coupe dont l'image n'est pas réellement affichée.

import { describe, it, expect } from 'vitest';
import { isImageDisplayed, failedImageId } from './imageGuard';

const showing = (imageId?: string) => ({
  getCornerstoneImage: () => (imageId ? { imageId } : undefined),
});

describe('isImageDisplayed', () => {
  it("accepte la coupe dont l'image est à l'écran", () => {
    expect(isImageDisplayed(showing('wadouri:xa#0'), 'wadouri:xa#0')).toBe(true);
  });

  it("refuse une coupe en échec alors que l'image précédente reste affichée", () => {
    // Cas PR DEFINE-PFA : index courant sur le PR (404), boucle XA encore à l'écran.
    expect(isImageDisplayed(showing('wadouri:xa#0'), 'wadouri:pr')).toBe(false);
  });

  it("refuse tant qu'aucune image n'a été affichée", () => {
    expect(isImageDisplayed(showing(undefined), 'wadouri:pr')).toBe(false);
    expect(isImageDisplayed({}, 'wadouri:pr')).toBe(false);
  });

  it('refuse sans identifiant de coupe', () => {
    expect(isImageDisplayed(showing('wadouri:xa#0'), undefined)).toBe(false);
    expect(isImageDisplayed(showing('wadouri:xa#0'), '')).toBe(false);
  });
});

describe('failedImageId', () => {
  it("lit l'imageId d'un événement IMAGE_LOAD_ERROR", () => {
    expect(failedImageId({ imageId: 'wadouri:pr', imageIdIndex: 0 })).toBe('wadouri:pr');
  });

  it('ignore les formes inattendues', () => {
    expect(failedImageId(undefined)).toBeNull();
    expect(failedImageId([{ status: 'rejected' }])).toBeNull();
    expect(failedImageId({ imageId: 3 })).toBeNull();
  });
});
