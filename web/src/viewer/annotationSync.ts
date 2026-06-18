// Pont entre les annotations Cornerstone et notre modèle `Measurement` persisté dans Girder.
//
//  - annotationToMeasurement : Cornerstone (world) → Measurement (pixels image) [item 1]
//  - createLevelMeasurement  : clic → mesure de niveau H/V                        [item 3]
//  - hydrateAnnotations      : Measurement → annotations Cornerstone éditables    [item 2a]
//  - drawOverlay             : niveaux + fantômes des slices adjacentes (±3)      [items 2b/4]

import { utilities as csUtils, metaData, imageLoader, type Types } from '@cornerstonejs/core';
import { LengthTool, ProbeTool, annotation as csAnnotation } from '@cornerstonejs/tools';
import {
  APP_VERSION,
  type Measurement,
  type MeasurementType,
  type MeasurementUser,
  type Point2D,
} from '../annotations/types';
import { resolvePixelSpacing, distancePx, distanceMm } from './measurements';

type StackViewport = Types.IStackViewport;
// Forme minimale d'une annotation Cornerstone qu'on manipule (typage volontairement souple).
interface CsAnnotation {
  annotationUID?: string;
  metadata?: { toolName?: string; referencedImageId?: string } & Record<string, unknown>;
  data?: { handles?: { points?: Types.Point3[] }; label?: string; cachedStats?: unknown };
}

const TOOL_TO_TYPE: Record<string, MeasurementType> = {
  [LengthTool.toolName]: 'distance',
  [ProbeTool.toolName]: 'point',
};
const TYPE_TO_TOOL: Partial<Record<MeasurementType, string>> = {
  distance: LengthTool.toolName,
  point: ProbeTool.toolName,
};

/** Les types portés par Cornerstone (distance/point). Les niveaux sont rendus par l'overlay. */
export function isCornerstoneBacked(type: MeasurementType): boolean {
  return type in TYPE_TO_TOOL;
}

// Couleurs : jaune = mesure déjà enregistrée (rechargée), vert = créée dans la session.
export const COLOR_EXISTING = 'rgb(255, 255, 0)';
export const COLOR_NEW = 'rgb(124, 252, 0)';

/** Force la couleur d'une annotation Cornerstone (distance/point). */
export function setAnnotationColor(uid: string, color: string): void {
  csAnnotation.config.style.setAnnotationStyles(uid, { color });
}

// --- Conversions world ↔ pixel robustes ----------------------------------
//
// `csUtils.worldToImageCoords`/`imageToWorldCoords` s'appuient sur l'imagePlaneModule
// (rowCosines, PixelSpacing…). En projection (fluoroscopie) ces tags sont ABSENTS → ces
// fonctions lèvent une exception. On tente donc d'abord la voie métadonnées (correcte pour
// le scanner, où chaque coupe a son propre z), puis on retombe sur la géométrie réelle du
// viewport (vtkImageData), qui fonctionne même sans métadonnées spatiales.

export function worldToPixel(
  viewport: StackViewport,
  imageId: string,
  world: Types.Point3,
): Point2D | null {
  try {
    const p = csUtils.worldToImageCoords(imageId, world);
    if (p) return { x: p[0], y: p[1] };
  } catch {
    /* pas de métadonnées spatiales → fallback viewport */
  }
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const vtk = (viewport.getImageData() as any)?.imageData;
  const idx = vtk?.worldToIndex?.(world);
  return idx ? { x: idx[0], y: idx[1] } : null;
}

export function pixelToWorld(
  viewport: StackViewport,
  imageId: string,
  pt: Point2D,
): Types.Point3 | null {
  try {
    const w = csUtils.imageToWorldCoords(imageId, [pt.x, pt.y]);
    if (w) return w;
  } catch {
    /* fallback viewport */
  }
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const vtk = (viewport.getImageData() as any)?.imageData;
  const w = vtk?.indexToWorld?.([pt.x, pt.y, 0]);
  return w ? [w[0], w[1], w[2]] : null;
}

// ---------------------------------------------------------------------------
// Item 1 — Cornerstone → Measurement
// ---------------------------------------------------------------------------

export function annotationToMeasurement(
  ann: CsAnnotation,
  viewport: StackViewport,
  user: MeasurementUser,
): Measurement | null {
  const toolName = ann.metadata?.toolName;
  const type = toolName ? TOOL_TO_TYPE[toolName] : undefined;
  const imageId = ann.metadata?.referencedImageId;
  const points = ann.data?.handles?.points ?? [];
  if (!type || !imageId || !ann.annotationUID) return null;

  const toPx = (w?: Types.Point3): Point2D | null =>
    w ? worldToPixel(viewport, imageId, w) : null;

  const imageIds = viewport.getImageIds();
  const frameIndex = Math.max(0, imageIds.indexOf(imageId));
  const spacing = resolvePixelSpacing(imageId);
  const sop = (metaData.get('sopCommonModule', imageId) as { sopInstanceUID?: string } | undefined)
    ?.sopInstanceUID;
  const series = (
    metaData.get('generalSeriesModule', imageId) as { seriesInstanceUID?: string } | undefined
  )?.seriesInstanceUID;

  const base = {
    id: ann.annotationUID,
    type,
    frameIndex,
    sopInstanceUID: sop,
    seriesInstanceUID: series,
    label: ann.data?.label,
    user,
    createdAt: new Date().toISOString(),
    appVersion: APP_VERSION,
  };

  if (type === 'distance') {
    const start = toPx(points[0]);
    const end = toPx(points[1]);
    if (!start || !end) return null;
    return {
      ...base,
      geometry: { start, end },
      values: {
        lengthPx: distancePx(start, end),
        lengthMm: distanceMm(start, end, spacing),
        spacingSource: spacing.source,
      },
    };
  }

  // point
  const p = toPx(points[0]);
  if (!p) return null;
  return {
    ...base,
    geometry: { point: p },
    values: { positionPx: p, spacingSource: spacing.source },
  };
}

// ---------------------------------------------------------------------------
// Item 3 — niveaux H/V (gérés hors outils Cornerstone : clic → ligne guide)
// ---------------------------------------------------------------------------

/**
 * Construit une mesure de niveau depuis un point image cliqué.
 *  - level-h : ligne horizontale → on ne retient que `y`.
 *  - level-v : ligne verticale  → on ne retient que `x`.
 */
export function createLevelMeasurement(
  type: 'level-h' | 'level-v',
  imageCoord: Point2D,
  viewport: StackViewport,
  user: MeasurementUser,
): Measurement | null {
  const imageId = viewport.getCurrentImageId();
  if (!imageId) return null;

  const frameIndex = Math.max(0, viewport.getImageIds().indexOf(imageId));
  const spacing = resolvePixelSpacing(imageId);
  const sop = (metaData.get('sopCommonModule', imageId) as { sopInstanceUID?: string } | undefined)
    ?.sopInstanceUID;
  const series = (
    metaData.get('generalSeriesModule', imageId) as { seriesInstanceUID?: string } | undefined
  )?.seriesInstanceUID;

  const geometry = type === 'level-h' ? { y: imageCoord.y } : { x: imageCoord.x };
  const positionPx = type === 'level-h' ? imageCoord.y : imageCoord.x;

  return {
    id: crypto.randomUUID(),
    type,
    geometry,
    values: { positionPx, spacingSource: spacing.source },
    frameIndex,
    sopInstanceUID: sop,
    seriesInstanceUID: series,
    label: '',
    user,
    createdAt: new Date().toISOString(),
    appVersion: APP_VERSION,
  };
}

// ---------------------------------------------------------------------------
// Item 2a — Measurement → annotations Cornerstone (overlay éditable au chargement)
// ---------------------------------------------------------------------------

export async function hydrateAnnotations(
  measurements: Measurement[],
  viewport: StackViewport,
  element: HTMLDivElement,
): Promise<void> {
  const imageIds = viewport.getImageIds();

  // Précharge les images des slices annotées : imageToWorldCoords a besoin de
  // l'imagePlaneModule, qui n'est enregistré qu'une fois l'image chargée. Sans ça,
  // les mesures sur des slices non courantes seraient silencieusement ignorées.
  const neededImageIds = [
    ...new Set(measurements.map((m) => imageIds[m.frameIndex]).filter(Boolean)),
  ];
  await Promise.all(
    neededImageIds.map((id) => imageLoader.loadAndCacheImage(id).catch(() => undefined)),
  );

  for (const m of measurements) {
    const toolName = TYPE_TO_TOOL[m.type];
    if (!toolName) continue; // type pas encore géré (niveaux)
    if (csAnnotation.state.getAnnotation(m.id)) continue; // déjà présent

    const imageId = imageIds[m.frameIndex];
    if (!imageId) continue;

    const toWorld = (pt?: Point2D): Types.Point3 | undefined =>
      pt ? (pixelToWorld(viewport, imageId, pt) ?? undefined) : undefined;

    const points =
      m.type === 'distance'
        ? [toWorld(m.geometry?.start), toWorld(m.geometry?.end)]
        : [toWorld(m.geometry?.point)];
    if (points.some((p) => !p)) continue;

    const viewRef = viewport.getViewReference({ sliceIndex: m.frameIndex });
    const annObj = {
      annotationUID: m.id,
      highlighted: false,
      isLocked: false,
      isVisible: true,
      invalidated: true, // force le recalcul des stats au rendu
      metadata: { ...viewRef, toolName, referencedImageId: imageId },
      data: {
        handles: { points: points as Types.Point3[], activeHandleIndex: null, textBox: {} },
        label: m.label,
        cachedStats: {},
      },
    };
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    csAnnotation.state.addAnnotation(annObj as any, element);
    // Mesure rechargée → jaune (item 2).
    setAnnotationColor(m.id, COLOR_EXISTING);
  }
}

// ---------------------------------------------------------------------------
// Items 2b + 3 + 4 — overlay : niveaux (toutes slices) + fantômes des mesures des
// slices adjacentes, avec opacité décroissant avec la distance (jusqu'à ±3).
// ---------------------------------------------------------------------------

// Opacité par distance à la slice courante (index = distance). Au-delà : masqué.
const GHOST_OPACITY = [1, 0.5, 0.3, 0.15];

export function ghostOpacity(distance: number): number {
  return GHOST_OPACITY[distance] ?? 0;
}

export function drawOverlay(
  ctx: CanvasRenderingContext2D,
  viewport: StackViewport,
  measurements: Measurement[],
  currentIndex: number,
  width: number,
  height: number,
  existingIds: Set<string>,
): void {
  const currentImageId = viewport.getCurrentImageId();
  if (!currentImageId) return;

  const toCanvas = (pt: Point2D): Types.Point2 | null => {
    const w = pixelToWorld(viewport, currentImageId, pt);
    return w ? viewport.worldToCanvas(w) : null;
  };

  ctx.save();
  ctx.lineWidth = 1.5;

  for (const m of measurements) {
    const distance = Math.abs(m.frameIndex - currentIndex);
    const opacity = ghostOpacity(distance);
    if (opacity === 0) continue;

    // distance/point de la slice COURANTE : rendus par Cornerstone (éditables) → on saute.
    // Les niveaux ne sont jamais portés par Cornerstone → toujours dessinés ici.
    if (distance === 0 && (m.type === 'distance' || m.type === 'point')) continue;

    // Jaune si mesure rechargée/enregistrée, vert si créée dans la session courante (item 2).
    const color = existingIds.has(m.id) ? COLOR_EXISTING : COLOR_NEW;
    ctx.strokeStyle = color;
    ctx.fillStyle = color;
    ctx.globalAlpha = opacity;

    if (m.type === 'distance' && m.geometry?.start && m.geometry?.end) {
      const a = toCanvas(m.geometry.start);
      const b = toCanvas(m.geometry.end);
      if (a && b) {
        ctx.beginPath();
        ctx.moveTo(a[0], a[1]);
        ctx.lineTo(b[0], b[1]);
        ctx.stroke();
        drawDot(ctx, a);
        drawDot(ctx, b);
      }
    } else if (m.type === 'point' && m.geometry?.point) {
      const p = toCanvas(m.geometry.point);
      if (p) drawCross(ctx, p);
    } else if (m.type === 'level-h' && m.geometry?.y != null) {
      const p = toCanvas({ x: 0, y: m.geometry.y });
      if (p) {
        ctx.beginPath();
        ctx.moveTo(0, p[1]);
        ctx.lineTo(width, p[1]);
        ctx.stroke();
      }
    } else if (m.type === 'level-v' && m.geometry?.x != null) {
      const p = toCanvas({ x: m.geometry.x, y: 0 });
      if (p) {
        ctx.beginPath();
        ctx.moveTo(p[0], 0);
        ctx.lineTo(p[0], height);
        ctx.stroke();
      }
    }
  }

  ctx.restore();
}

function drawDot(ctx: CanvasRenderingContext2D, p: Types.Point2) {
  ctx.beginPath();
  ctx.arc(p[0], p[1], 3, 0, Math.PI * 2);
  ctx.fill();
}

function drawCross(ctx: CanvasRenderingContext2D, p: Types.Point2) {
  const s = 6;
  ctx.beginPath();
  ctx.moveTo(p[0] - s, p[1]);
  ctx.lineTo(p[0] + s, p[1]);
  ctx.moveTo(p[0], p[1] - s);
  ctx.lineTo(p[0], p[1] + s);
  ctx.stroke();
}
