import { describe, it, expect, vi, beforeEach } from 'vitest';

// Mock du metaData Cornerstone (évite de charger le vrai module lourd).
const { get } = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock('@cornerstonejs/core', () => ({ metaData: { get } }));

import {
  distancePx,
  distanceMm,
  isNonAnatomicScale,
  resolvePixelSpacing,
  readDicomInfo,
} from './measurements';

beforeEach(() => get.mockReset());

describe('distance', () => {
  it('distancePx = hypoténuse en pixels', () => {
    expect(distancePx({ x: 0, y: 0 }, { x: 3, y: 4 })).toBe(5);
  });

  it('distanceMm applique le spacing par axe', () => {
    const spacing = { rowMm: 2, colMm: 2, source: 'PixelSpacing' as const };
    expect(distanceMm({ x: 0, y: 0 }, { x: 3, y: 4 }, spacing)).toBe(10);
  });

  it('distanceMm = null sans échelle', () => {
    const spacing = { rowMm: null, colMm: null, source: 'none' as const };
    expect(distanceMm({ x: 0, y: 0 }, { x: 3, y: 4 }, spacing)).toBeNull();
  });
});

describe('isNonAnatomicScale', () => {
  it('vrai seulement pour ImagerPixelSpacing', () => {
    expect(isNonAnatomicScale('ImagerPixelSpacing')).toBe(true);
    expect(isNonAnatomicScale('PixelSpacing')).toBe(false);
    expect(isNonAnatomicScale('none')).toBe(false);
  });
});

describe('resolvePixelSpacing', () => {
  it('PixelSpacing fiable depuis imagePlaneModule', () => {
    get.mockImplementation((mod: string) =>
      mod === 'imagePlaneModule' ? { rowPixelSpacing: 0.5, columnPixelSpacing: 0.6 } : undefined,
    );
    expect(resolvePixelSpacing('id')).toEqual({ rowMm: 0.5, colMm: 0.6, source: 'PixelSpacing' });
  });

  it('repli sur ImagerPixelSpacing (0018,1164)', () => {
    get.mockImplementation((mod: string) => (mod === '0018,1164' ? [0.2, 0.3] : undefined));
    expect(resolvePixelSpacing('id')).toEqual({
      rowMm: 0.2,
      colMm: 0.3,
      source: 'ImagerPixelSpacing',
    });
  });

  it('none si aucune info exploitable', () => {
    get.mockReturnValue(undefined);
    expect(resolvePixelSpacing('id')).toEqual({ rowMm: null, colMm: null, source: 'none' });
  });
});

describe('readDicomInfo', () => {
  it('mappe les modules et masque les valeurs non-stringifiables', () => {
    get.mockImplementation((mod: string) => {
      if (mod === 'patientModule') return { patientName: 'DOE^JANE', patientId: 'X1' };
      if (mod === 'generalStudyModule') return { studyDate: {}, studyDescription: 'TDM' };
      if (mod === 'generalSeriesModule') return { modality: 'CT', seriesNumber: 3 };
      if (mod === 'imagePixelModule') return { rows: 512, columns: 512 };
      return undefined;
    });
    const info = readDicomInfo('id');
    expect(info.patientName).toBe('DOE^JANE');
    expect(info.modality).toBe('CT');
    expect(info.rows).toBe(512);
    expect(info.studyDate).toBeUndefined(); // objet vide → masqué (pas de "[object Object]")
  });
});
