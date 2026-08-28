// Préchargement du stack : une fois la coupe courante affichée, tout le reste doit finir en
// mémoire, en commençant par les voisines de la coupe RÉELLEMENT regardée.

import { describe, it, expect, vi, beforeEach } from 'vitest';

const { loadAndCacheImage } = vi.hoisted(() => ({ loadAndCacheImage: vi.fn() }));
vi.mock('@cornerstonejs/core', () => ({ imageLoader: { loadAndCacheImage } }));

import { prefetchStack } from './prefetch';

const IDS = Array.from({ length: 10 }, (_, i) => `img${i}`);

/** Laisse les promesses de chargement s'enchaîner (le pump est récursif via `finally`). */
async function drain(): Promise<void> {
  for (let i = 0; i < 50; i++) await Promise.resolve();
}

beforeEach(() => {
  loadAndCacheImage.mockReset();
  loadAndCacheImage.mockResolvedValue(undefined);
});

describe('prefetchStack', () => {
  it('charge TOUT le stack, une seule fois chacun', async () => {
    prefetchStack(IDS, () => 0, () => false, 3);
    await drain();

    const loaded = loadAndCacheImage.mock.calls.map((c) => c[0]);
    expect(new Set(loaded)).toEqual(new Set(IDS));
    expect(loaded).toHaveLength(IDS.length);
  });

  it('commence par les voisines de la coupe courante', async () => {
    prefetchStack(IDS, () => 5, () => false, 1);
    await drain();

    const order = loadAndCacheImage.mock.calls.map((c) => c[0]);
    expect(order.slice(0, 3)).toEqual(['img5', 'img4', 'img6']);
  });

  it('se réoriente si l’utilisateur se déplace pendant le chargement', async () => {
    // L'utilisateur saute à la fin de la série après les deux premiers chargements.
    let current = 0;
    loadAndCacheImage.mockImplementation(async () => {
      if (loadAndCacheImage.mock.calls.length === 2) current = 9;
    });

    prefetchStack(IDS, () => current, () => false, 1);
    await drain();

    const order = loadAndCacheImage.mock.calls.map((c) => c[0]);
    expect(order.slice(0, 2)).toEqual(['img0', 'img1']);
    // Ordre figé au démarrage → on aurait continué par img2 ; ici on suit l'utilisateur.
    expect(order[2]).toBe('img9');
  });

  it('s’arrête au démontage', async () => {
    let aborted = false;
    loadAndCacheImage.mockImplementation(async () => {
      aborted = true;
    });

    prefetchStack(IDS, () => 0, () => aborted, 1);
    await drain();

    expect(loadAndCacheImage.mock.calls.length).toBeLessThan(IDS.length);
  });
});
