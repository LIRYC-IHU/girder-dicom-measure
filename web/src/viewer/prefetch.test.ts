// Préchargement du stack : une fois la coupe courante affichée, tout le reste doit finir en
// mémoire, dans l'ordre des coupes, en rendant compte de l'avancement.

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
    prefetchStack(IDS, () => false, undefined, 3);
    await drain();

    const loaded = loadAndCacheImage.mock.calls.map((c) => c[0]);
    expect(new Set(loaded)).toEqual(new Set(IDS));
    expect(loaded).toHaveLength(IDS.length);
  });

  it('demande les images dans l’ordre des coupes', async () => {
    prefetchStack(IDS, () => false, undefined, 1);
    await drain();

    expect(loadAndCacheImage.mock.calls.map((c) => c[0])).toEqual(IDS);
  });

  it('rend compte de l’avancement, échecs compris', async () => {
    loadAndCacheImage.mockImplementation((id: string) =>
      id === 'img3' ? Promise.reject(new Error('boom')) : Promise.resolve(undefined),
    );
    const progress: number[] = [];

    prefetchStack(IDS, () => false, (n) => progress.push(n), 1);
    await drain();

    expect(progress).toEqual([1, 2, 3, 4, 5, 6, 7, 8, 9, 10]);
  });

  it('s’arrête au démontage', async () => {
    let aborted = false;
    loadAndCacheImage.mockImplementation(async () => {
      aborted = true;
    });

    prefetchStack(IDS, () => aborted, undefined, 1);
    await drain();

    expect(loadAndCacheImage.mock.calls.length).toBeLessThan(IDS.length);
  });
});
