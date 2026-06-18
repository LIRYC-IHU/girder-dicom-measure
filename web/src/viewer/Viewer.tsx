import { useEffect, useRef, useState } from 'react';
import {
  RenderingEngine,
  Enums as CoreEnums,
  eventTarget,
  type Types,
} from '@cornerstonejs/core';
import {
  ToolGroupManager,
  LengthTool,
  ProbeTool,
  PanTool,
  ZoomTool,
  StackScrollTool,
  Enums as ToolEnums,
  annotation as csAnnotation,
} from '@cornerstonejs/tools';
import { ensureCornerstoneInitialized, buildStackImageIds, prefetchStack } from './cornerstoneSetup';
import { resolvePixelSpacing, isNonAnatomicScale, readDicomInfo } from './measurements';
import {
  annotationToMeasurement,
  createLevelMeasurement,
  hydrateAnnotations,
  drawOverlay,
  setAnnotationColor,
  worldToPixel,
  COLOR_NEW,
} from './annotationSync';
import type { MeasurementStore } from '../annotations/store';
import type { MeasurementUser, SpacingSource } from '../annotations/types';
import type { DicomInfo } from './measurements';

/** API impérative exposée à App (panneau de mesures). */
export interface ViewerApi {
  gotoFrame(index: number): void;
  deleteMeasurement(id: string): void;
  setLabel(id: string, label: string): void;
}

const RENDERING_ENGINE_ID = 'dmf-engine';
const VIEWPORT_ID = 'dmf-viewport';
const TOOL_GROUP_ID = 'dmf-tools';

export type ActiveTool = 'distance' | 'point' | 'level-h' | 'level-v' | 'pan' | 'zoom';

// Outils portés par Cornerstone (clic gauche). Les niveaux H/V sont gérés à part.
const TOOL_NAME: Partial<Record<ActiveTool, string>> = {
  distance: LengthTool.toolName,
  point: ProbeTool.toolName,
  pan: PanTool.toolName,
  zoom: ZoomTool.toolName,
};

function isLevelTool(t: ActiveTool): t is 'level-h' | 'level-v' {
  return t === 'level-h' || t === 'level-v';
}

interface ViewerProps {
  /** URLs de téléchargement Girder, dans l'ordre des slices. */
  fileUrls: string[];
  activeTool: ActiveTool;
  store: MeasurementStore;
  user: MeasurementUser;
  onFrameChange?: (frameIndex: number) => void;
  /** Nombre réel de slices/frames du stack (≠ nombre de fichiers si multiframe). */
  onStackReady?: (frameCount: number) => void;
  /** Reçoit l'API impérative quand le viewer est prêt (et null au démontage). */
  onApiReady?: (api: ViewerApi | null) => void;
  /** Métadonnées DICOM de la coupe courante (item 4). */
  onDicomInfo?: (info: DicomInfo) => void;
}

export function Viewer({
  fileUrls,
  activeTool,
  store,
  user,
  onFrameChange,
  onStackReady,
  onApiReady,
  onDicomInfo,
}: ViewerProps) {
  const elementRef = useRef<HTMLDivElement>(null);
  const ghostCanvasRef = useRef<HTMLCanvasElement>(null);
  const engineRef = useRef<RenderingEngine | null>(null);
  // Outil courant lu par le handler de clic (niveaux) sans recréer le listener.
  const activeToolRef = useRef<ActiveTool>(activeTool);
  activeToolRef.current = activeTool;
  const [spacingSource, setSpacingSource] = useState<SpacingSource>('none');
  const [toolsReady, setToolsReady] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let disposed = false;
    const element = elementRef.current;
    const ghostCanvas = ghostCanvasRef.current;
    if (!element || !ghostCanvas) return;

    // Évite les boucles : on ignore les events émis pendant la reconstruction initiale.
    let hydrating = true;
    const modifyTimers = new Map<string, ReturnType<typeof setTimeout>>();
    const cleanups: Array<() => void> = [];

    (async () => {
      try {
        await ensureCornerstoneInitialized();
        if (disposed) return;

        const engine = new RenderingEngine(RENDERING_ENGINE_ID);
        engineRef.current = engine;
        engine.enableElement({
          viewportId: VIEWPORT_ID,
          type: CoreEnums.ViewportType.STACK,
          element,
        });

        const viewport = engine.getViewport(VIEWPORT_ID) as Types.IStackViewport;
        // Série multi-fichiers OU fichier multiframe (fluoro cine) → liste d'imageIds.
        const imageIds = await buildStackImageIds(fileUrls);
        if (disposed) return;
        await viewport.setStack(imageIds, 0);
        onStackReady?.(imageIds.length);
        engine.resize(true); // garantit le dimensionnement du canvas après le layout
        viewport.render();

        // Toolgroup partagé.
        ToolGroupManager.destroyToolGroup(TOOL_GROUP_ID);
        const toolGroup = ToolGroupManager.createToolGroup(TOOL_GROUP_ID)!;
        [LengthTool, ProbeTool, PanTool, ZoomTool, StackScrollTool].forEach((t) =>
          toolGroup.addTool(t.toolName),
        );
        toolGroup.addViewport(VIEWPORT_ID, RENDERING_ENGINE_ID);
        toolGroup.setToolActive(StackScrollTool.toolName, {
          bindings: [{ mouseButton: ToolEnums.MouseBindings.Wheel }],
        });

        // Préchargement du stack autour de la slice courante → défilement fluide.
        prefetchStack(imageIds, 0, () => disposed);

        // Le toolgroup existe → l'effet de bascule d'outil peut appliquer l'outil courant.
        setToolsReady(true);

        // Mesures déjà présentes au chargement = « existantes » → affichées en jaune.
        // Celles créées dans la session resteront vertes (item 2).
        const existingIds = new Set(store.all().map((m) => m.id));

        // --- Item 2a : reconstruire les mesures sauvegardées (overlay éditable) ---
        await hydrateAnnotations(store.all(), viewport, element);
        viewport.render();
        hydrating = false;

        // --- Item 2b : calque de fantômes (slices ±1 à 30%) ---
        const renderGhosts = () => {
          const dpr = window.devicePixelRatio || 1;
          const w = element.clientWidth;
          const h = element.clientHeight;
          ghostCanvas.width = w * dpr;
          ghostCanvas.height = h * dpr;
          ghostCanvas.style.width = `${w}px`;
          ghostCanvas.style.height = `${h}px`;
          const ctx = ghostCanvas.getContext('2d');
          if (!ctx) return;
          ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
          ctx.clearRect(0, 0, w, h);
          drawOverlay(ctx, viewport, store.all(), viewport.getCurrentImageIdIndex(), w, h, existingIds);
        };

        // Spacing + métadonnées DICOM de la slice courante (bannière + item 4).
        const refreshSpacing = () => {
          const id = viewport.getCurrentImageId();
          if (!id) return;
          setSpacingSource(resolvePixelSpacing(id).source);
          onDicomInfo?.(readDicomInfo(id));
        };
        refreshSpacing();
        renderGhosts();

        // IMAGE_RENDERED couvre pan/zoom/changement de slice → on redessine les fantômes.
        const onRendered = () => renderGhosts();
        element.addEventListener(CoreEnums.Events.IMAGE_RENDERED, onRendered);
        cleanups.push(() =>
          element.removeEventListener(CoreEnums.Events.IMAGE_RENDERED, onRendered),
        );

        // Redimensionnement du conteneur (layout tardif, plein écran, fenêtre) → resize CS.
        const resizeObserver = new ResizeObserver(() => {
          if (element.clientWidth === 0 || element.clientHeight === 0) return;
          engine.resize(true);
          viewport.render();
          renderGhosts();
        });
        resizeObserver.observe(element);
        cleanups.push(() => resizeObserver.disconnect());

        const onNewImage = () => {
          onFrameChange?.(viewport.getCurrentImageIdIndex());
          refreshSpacing();
        };
        element.addEventListener(CoreEnums.Events.STACK_NEW_IMAGE, onNewImage);
        cleanups.push(() =>
          element.removeEventListener(CoreEnums.Events.STACK_NEW_IMAGE, onNewImage),
        );

        // --- Item 3 : pose d'un niveau H/V au clic (hors outils Cornerstone) ---
        const onLevelClick = (e: MouseEvent) => {
          const tool = activeToolRef.current;
          if (!isLevelTool(tool)) return;
          const id = viewport.getCurrentImageId();
          if (!id) return;
          const world = viewport.canvasToWorld([e.offsetX, e.offsetY]);
          const px = worldToPixel(viewport, id, world);
          if (!px) return;
          const m = createLevelMeasurement(tool, px, viewport, user);
          if (m) {
            void store.add(m);
            renderGhosts();
          }
        };
        element.addEventListener('click', onLevelClick);
        cleanups.push(() => element.removeEventListener('click', onLevelClick));

        // --- Item 5 : flèches haut/bas pour changer de slice (complément molette) ---
        const onArrowKey = (e: KeyboardEvent) => {
          if (e.key !== 'ArrowUp' && e.key !== 'ArrowDown') return;
          if (e.metaKey || e.ctrlKey || e.altKey) return;
          if ((e.target as HTMLElement)?.tagName === 'INPUT') return; // saisie de label
          const n = viewport.getImageIds().length;
          const cur = viewport.getCurrentImageIdIndex();
          const nextIdx = Math.min(n - 1, Math.max(0, cur + (e.key === 'ArrowDown' ? 1 : -1)));
          if (nextIdx !== cur) {
            e.preventDefault();
            void viewport.setImageIdIndex(nextIdx);
          }
        };
        window.addEventListener('keydown', onArrowKey);
        cleanups.push(() => window.removeEventListener('keydown', onArrowKey));

        // --- Item 1 : persistance des mesures dans Girder ---
        const onCompleted = (evt: Event) => {
          if (hydrating) return;
          const ann = (evt as CustomEvent).detail?.annotation;
          const m = ann && annotationToMeasurement(ann, viewport, user);
          if (m) {
            void store.add(m);
            // Nouvelle mesure de la session → vert (deviendra jaune au rechargement).
            if (ann.annotationUID) setAnnotationColor(ann.annotationUID, COLOR_NEW);
            viewport.render();
          }
        };
        const onModified = (evt: Event) => {
          if (hydrating) return;
          const ann = (evt as CustomEvent).detail?.annotation;
          if (!ann?.annotationUID) return;
          // Débounce : un drag émet de nombreux MODIFIED.
          const uid = ann.annotationUID as string;
          clearTimeout(modifyTimers.get(uid));
          modifyTimers.set(
            uid,
            setTimeout(() => {
              const m = annotationToMeasurement(ann, viewport, user);
              if (m) void store.update(m.id, { geometry: m.geometry, values: m.values });
            }, 300),
          );
        };
        const onRemoved = (evt: Event) => {
          const ann = (evt as CustomEvent).detail?.annotation;
          if (ann?.annotationUID) void store.remove(ann.annotationUID as string);
        };

        eventTarget.addEventListener(ToolEnums.Events.ANNOTATION_COMPLETED, onCompleted);
        eventTarget.addEventListener(ToolEnums.Events.ANNOTATION_MODIFIED, onModified);
        eventTarget.addEventListener(ToolEnums.Events.ANNOTATION_REMOVED, onRemoved);
        cleanups.push(() => {
          eventTarget.removeEventListener(ToolEnums.Events.ANNOTATION_COMPLETED, onCompleted);
          eventTarget.removeEventListener(ToolEnums.Events.ANNOTATION_MODIFIED, onModified);
          eventTarget.removeEventListener(ToolEnums.Events.ANNOTATION_REMOVED, onRemoved);
        });

        // --- API impérative pour le panneau de mesures (item 1) ---
        const api: ViewerApi = {
          gotoFrame: (index) => {
            const n = viewport.getImageIds().length;
            void viewport.setImageIdIndex(Math.min(n - 1, Math.max(0, index)));
          },
          deleteMeasurement: (id) => {
            // distance/point : removeAnnotation → ANNOTATION_REMOVED → onRemoved → store.remove.
            // niveaux (hors Cornerstone) : suppression directe du store.
            if (csAnnotation.state.getAnnotation(id)) {
              csAnnotation.state.removeAnnotation(id);
            } else {
              void store.remove(id);
            }
            renderGhosts();
            viewport.render();
          },
          setLabel: (id, label) => {
            void store.update(id, { label });
            const csAnn = csAnnotation.state.getAnnotation(id);
            if (csAnn?.data) {
              csAnn.data.label = label;
              viewport.render();
            }
          },
        };
        onApiReady?.(api);
        cleanups.push(() => onApiReady?.(null));
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
    })();

    return () => {
      disposed = true;
      modifyTimers.forEach((t) => clearTimeout(t));
      cleanups.forEach((fn) => fn());
      ToolGroupManager.destroyToolGroup(TOOL_GROUP_ID);
      engineRef.current?.destroy();
      engineRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fileUrls.join('|')]);

  // Bascule de l'outil actif (bouton gauche). Dépend de `toolsReady` : au 1er montage
  // l'effet tournait avant la création du toolgroup → aucun outil lié (bug item 1).
  useEffect(() => {
    const toolGroup = ToolGroupManager.getToolGroup(TOOL_GROUP_ID);
    if (!toolGroup) return;
    // Pour un niveau (géré au clic, hors Cornerstone) : tous les outils Cornerstone passifs.
    (Object.keys(TOOL_NAME) as ActiveTool[]).forEach((key) => {
      const toolName = TOOL_NAME[key]!;
      if (!isLevelTool(activeTool) && key === activeTool) {
        toolGroup.setToolActive(toolName, {
          bindings: [{ mouseButton: ToolEnums.MouseBindings.Primary }],
        });
      } else {
        toolGroup.setToolPassive(toolName);
      }
    });
  }, [activeTool, toolsReady]);

  if (error) return <div className="error">Erreur viewer : {error}</div>;

  return (
    <div className="viewport">
      {isNonAnatomicScale(spacingSource) && (
        <div className="banner">
          ⚠️ Échelle au plan détecteur (ImagerPixelSpacing) — la mesure en mm n'est pas une
          dimension anatomique réelle (magnification non corrigée).
        </div>
      )}
      <div className="viewport-stage">
        <div
          ref={elementRef}
          className="cs-element"
          onContextMenu={(e) => e.preventDefault()}
        />
        {/* Calque des fantômes : non interactif, au-dessus du canvas Cornerstone. */}
        <canvas ref={ghostCanvasRef} className="ghost-layer" />
      </div>
    </div>
  );
}
