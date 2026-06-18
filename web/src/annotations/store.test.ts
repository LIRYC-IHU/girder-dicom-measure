import { describe, it, expect, vi, beforeEach } from 'vitest';
import type { Measurement } from './types';

// Mock du client REST (pour tester AnnotationStore sans réseau).
const girderMock = vi.hoisted(() => ({
  listAnnotations: vi.fn(async () => [] as Measurement[]),
  createAnnotation: vi.fn(async () => ({})),
  updateAnnotation: vi.fn(async () => ({})),
  deleteAnnotation: vi.fn(async () => ({})),
}));
vi.mock('../api/girder', () => ({ girder: girderMock }));

import { AnnotationStore, LocalAnnotationStore } from './store';

function measure(id: string): Measurement {
  return {
    id,
    type: 'point',
    geometry: { point: { x: 1, y: 2 } },
    values: { spacingSource: 'none' },
    frameIndex: 0,
    user: { id: 'u', login: 'u', name: 'U' },
    createdAt: '2024-01-01T00:00:00Z',
    appVersion: 'test',
  };
}

describe('LocalAnnotationStore', () => {
  beforeEach(() => localStorage.clear());

  it('add / update / remove + persistance localStorage', async () => {
    const store = new LocalAnnotationStore('CT');
    await store.add(measure('a'));
    expect(store.all().map((m) => m.id)).toEqual(['a']);

    await store.update('a', { label: 'aorte' });
    expect(store.all()[0].label).toBe('aorte');

    // Persisté : un nouveau store relit le localStorage.
    const reloaded = new LocalAnnotationStore('CT');
    await reloaded.load();
    expect(reloaded.all()[0].label).toBe('aorte');

    await store.remove('a');
    expect(store.all()).toEqual([]);
  });

  it('notifie les abonnés à chaque changement', async () => {
    const store = new LocalAnnotationStore('CT');
    const cb = vi.fn();
    store.subscribe(cb);
    await store.add(measure('a'));
    expect(cb).toHaveBeenCalled();
  });
});

describe('AnnotationStore (Girder)', () => {
  beforeEach(() => {
    girderMock.createAnnotation.mockReset().mockResolvedValue({});
  });

  it('add appelle createAnnotation', async () => {
    const store = new AnnotationStore('item1');
    await store.add(measure('a'));
    expect(girderMock.createAnnotation).toHaveBeenCalledWith('item1', expect.objectContaining({ id: 'a' }));
  });

  it('remonte les échecs de persistance via subscribeErrors', async () => {
    const store = new AnnotationStore('item1');
    const onError = vi.fn();
    store.subscribeErrors(onError);
    girderMock.createAnnotation.mockRejectedValueOnce(new Error('boom'));
    await store.add(measure('a')); // l'échec ne rejette pas, il est signalé
    expect(onError).toHaveBeenCalledWith(expect.any(Error));
    // L'état optimiste reste présent (la mesure est dans la liste).
    expect(store.all().map((m) => m.id)).toEqual(['a']);
  });
});
