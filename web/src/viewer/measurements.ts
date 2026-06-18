// Conversion px → mm et extraction du pixel spacing.
//
// Cornerstone calcule déjà la longueur en mm quand l'imagePlaneModule porte un
// pixelSpacing. Mais pour nos mesures « position » / « niveau » (qu'on gère hors outil
// Length), et pour DÉCIDER quoi afficher en fluoroscopie, on a besoin de connaître la
// SOURCE du spacing — cf. CLAUDE.md § Échelle.

import { metaData } from '@cornerstonejs/core';
import type { SpacingSource } from '../annotations/types';

export interface PixelSpacingInfo {
  rowMm: number | null; // taille d'un pixel en Y (mm)
  colMm: number | null; // taille d'un pixel en X (mm)
  source: SpacingSource;
}

/**
 * Résout le spacing exploitable pour un imageId.
 * Priorité PixelSpacing (0028,0030) — fiable — puis ImagerPixelSpacing (0018,1164) —
 * au plan détecteur, donc à signaler comme non-anatomique en projection.
 */
export function resolvePixelSpacing(imageId: string): PixelSpacingInfo {
  const imagePlane = metaData.get('imagePlaneModule', imageId) as
    | { rowPixelSpacing?: number; columnPixelSpacing?: number }
    | undefined;
  if (imagePlane?.rowPixelSpacing && imagePlane?.columnPixelSpacing) {
    return {
      rowMm: imagePlane.rowPixelSpacing,
      colMm: imagePlane.columnPixelSpacing,
      source: 'PixelSpacing',
    };
  }

  // Fallback : ImagerPixelSpacing si le loader l'a exposé.
  const imager = metaData.get('0018,1164', imageId) as number[] | string | undefined;
  const parsed = Array.isArray(imager)
    ? imager
    : typeof imager === 'string'
      ? imager.split('\\').map(Number)
      : null;
  if (parsed && parsed.length === 2 && parsed.every((n) => n > 0)) {
    return { rowMm: parsed[0], colMm: parsed[1], source: 'ImagerPixelSpacing' };
  }

  return { rowMm: null, colMm: null, source: 'none' };
}

/** Distance euclidienne en pixels entre deux points image. */
export function distancePx(a: { x: number; y: number }, b: { x: number; y: number }): number {
  return Math.hypot(b.x - a.x, b.y - a.y);
}

/** Distance en mm si le spacing est connu, sinon null. */
export function distanceMm(
  a: { x: number; y: number },
  b: { x: number; y: number },
  spacing: PixelSpacingInfo,
): number | null {
  if (spacing.rowMm == null || spacing.colMm == null) return null;
  const dx = (b.x - a.x) * spacing.colMm;
  const dy = (b.y - a.y) * spacing.rowMm;
  return Math.hypot(dx, dy);
}

/** Vrai si la mesure mm doit être affichée avec un avertissement « plan détecteur ». */
export function isNonAnatomicScale(source: SpacingSource): boolean {
  return source === 'ImagerPixelSpacing';
}

// --- Métadonnées DICOM lisibles (item 4) ----------------------------------
// Lues du parsing client (Cornerstone metaData). Champs absents = undefined (ex: fluoro
// sans infos patient). Côté serveur, les mêmes infos sont aussi dans item.dicom.meta.

export interface DicomInfo {
  patientName?: string;
  patientId?: string;
  studyDate?: string;
  studyDescription?: string;
  modality?: string;
  seriesNumber?: number;
  seriesDescription?: string;
  rows?: number;
  columns?: number;
}

function str(v: unknown): string | undefined {
  if (v == null || v === '') return undefined;
  if (v instanceof Date) return v.toLocaleDateString();
  if (typeof v === 'object') {
    // PersonName-like ({Alphabetic}) ou {value} ; sinon on masque (pas de "[object Object]").
    const o = v as Record<string, unknown>;
    const alt = o.Alphabetic ?? o.value;
    return alt != null ? String(alt) : undefined;
  }
  // Date DICOM "YYYYMMDD" → "YYYY-MM-DD".
  const s = String(v);
  return /^\d{8}$/.test(s) ? `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6, 8)}` : s;
}

export function readDicomInfo(imageId: string): DicomInfo {
  const get = (m: string) => metaData.get(m, imageId) as Record<string, unknown> | undefined;
  const patient = get('patientModule');
  const study = get('generalStudyModule');
  const series = get('generalSeriesModule');
  const pixel = get('imagePixelModule');
  return {
    patientName: str(patient?.patientName),
    patientId: str(patient?.patientId),
    studyDate: str(study?.studyDate),
    studyDescription: str(study?.studyDescription),
    modality: str(series?.modality),
    seriesNumber: series?.seriesNumber as number | undefined,
    seriesDescription: str(series?.seriesDescription),
    rows: pixel?.rows as number | undefined,
    columns: pixel?.columns as number | undefined,
  };
}
