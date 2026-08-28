// Modèle de données des annotations (cf. CLAUDE.md § « Modèle de données »).
// Les coordonnées sont TOUJOURS en pixels image (indépendantes du zoom/pan).

export type MeasurementType = 'distance' | 'point' | 'level-h' | 'level-v';

export type SpacingSource = 'PixelSpacing' | 'ImagerPixelSpacing' | 'none';

export interface Point2D {
  x: number;
  y: number;
}

export interface MeasurementGeometry {
  /** distance */
  start?: Point2D;
  end?: Point2D;
  /** point */
  point?: Point2D;
  /** level-h → y ; level-v → x */
  x?: number;
  y?: number;
}

export interface MeasurementValues {
  lengthPx?: number;
  lengthMm?: number | null; // null si aucune échelle exploitable
  positionPx?: Point2D | number;
  spacingSource: SpacingSource;
}

export interface MeasurementUser {
  id: string;
  login: string;
  name: string;
}

export interface Measurement {
  id: string;
  type: MeasurementType;
  geometry: MeasurementGeometry;
  values: MeasurementValues;
  /** index de slice/frame dans un multi-frame */
  frameIndex: number;
  sopInstanceUID?: string;
  seriesInstanceUID?: string;
  label?: string;
  user: MeasurementUser;
  createdAt: string; // ISO-8601 UTC
  appVersion: string;
}

export const APP_VERSION = '0.2.1';
