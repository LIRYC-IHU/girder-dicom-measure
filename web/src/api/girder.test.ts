// Construction des URLs du stack : c'est ici que se décide « fichier entier » vs
// « une URL par frame » (affichage progressif). Une erreur de cardinalité ici donnerait un
// stack au mauvais nombre de coupes, donc des mesures rattachées à la mauvaise image.

import { describe, expect, it, vi, afterEach } from 'vitest';
import { girder } from './girder';

function mockFiles(files: Array<{ id: string; name: string; frames?: number }>) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => ({
      ok: true,
      headers: new Headers({ 'content-type': 'application/json' }),
      json: async () => files,
    })),
  );
}

afterEach(() => vi.unstubAllGlobals());

describe('orderedFileUrls', () => {
  it('sert une URL par fichier quand il n’y a rien à découper', async () => {
    mockFiles([
      { id: 'a', name: 'CT0.dcm', frames: 1 },
      { id: 'b', name: 'CT1.dcm', frames: 1 },
    ]);
    const urls = await girder.orderedFileUrls('item');
    expect(urls).toHaveLength(2);
    expect(urls.every((u) => !u.includes('/frame/'))).toBe(true);
  });

  it('développe une boucle multi-frame en une URL par frame, dans l’ordre', async () => {
    mockFiles([{ id: 'cine', name: 'loop.dcm', frames: 3 }]);
    const urls = await girder.orderedFileUrls('item');
    expect(urls).toEqual([
      girder.frameDownloadUrl('cine', 0),
      girder.frameDownloadUrl('cine', 1),
      girder.frameDownloadUrl('cine', 2),
    ]);
    expect(urls[0]).toContain('/file/cine/frame/0');
  });

  it('retombe sur le fichier entier si le serveur n’annonce pas de frames', async () => {
    // Serveur antérieur au découpage, ou source déjà compressée : le champ est absent.
    mockFiles([{ id: 'legacy', name: 'loop.dcm' }]);
    expect(await girder.orderedFileUrls('item')).toEqual([girder.fileDownloadUrl('legacy')]);
  });
});
