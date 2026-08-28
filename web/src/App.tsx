import { useEffect, useState } from 'react';
import { girder, type DmfConfig } from './api/girder';
import { AnnotationStore, LocalAnnotationStore, type MeasurementStore } from './annotations/store';
import { Viewer, type ActiveTool, type ViewerApi } from './viewer/Viewer';
import type { DicomInfo } from './viewer/measurements';
import type { Measurement, MeasurementType, MeasurementUser } from './annotations/types';

const params = new URLSearchParams(window.location.search);

// Raccourcis = touche SIMPLE (sans modificateur) → standard en visualisation médicale et
// sans conflit avec les raccourcis OS/navigateur (ex. ⌘H = masquer sous macOS). Ignorés
// quand un modificateur est pressé ou que le focus est dans un champ de saisie.
const TOOLS: { id: ActiveTool; label: string; shortcut?: string }[] = [
  { id: 'distance', label: 'Distance', shortcut: 'd' },
  { id: 'point', label: 'Position', shortcut: 'p' },
  { id: 'level-h', label: 'Niveau H', shortcut: 'h' },
  { id: 'level-v', label: 'Niveau V', shortcut: 'v' },
  { id: 'pan', label: 'Déplacer' },
  { id: 'zoom', label: 'Zoom' },
];

const SHORTCUT_TO_TOOL: Record<string, ActiveTool> = Object.fromEntries(
  TOOLS.filter((t) => t.shortcut).map((t) => [t.shortcut, t.id]),
);

const TYPE_LABEL: Record<MeasurementType, string> = {
  distance: 'Distance',
  point: 'Position',
  'level-h': 'Niveau H',
  'level-v': 'Niveau V',
};

function measurementSummary(m: Measurement): string {
  if (m.type === 'distance') {
    const mm = m.values.lengthMm;
    return mm != null ? `${mm.toFixed(1)} mm` : `${Math.round(m.values.lengthPx ?? 0)} px`;
  }
  if (m.type === 'point' && m.geometry?.point) {
    return `(${Math.round(m.geometry.point.x)}, ${Math.round(m.geometry.point.y)})`;
  }
  if (m.type === 'level-h' && m.geometry?.y != null) return `y = ${Math.round(m.geometry.y)}`;
  if (m.type === 'level-v' && m.geometry?.x != null) return `x = ${Math.round(m.geometry.x)}`;
  return '';
}

/** Libellé du transport des pixels (cf. réglage `dmf.compression` côté plugin). */
function transportLabel(config: DmfConfig): string | null {
  if (config.compression === 'lossy') {
    return `JPEG 2000 avec perte (~${Math.round(config.lossyRatio)}:1)`;
  }
  if (config.compression === 'lossless') return 'JPEG-LS sans perte';
  return null; // aucune recompression → rien à signaler
}

function formatTimestamp(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

// Champs DICOM affichés (item 4), dans l'ordre, avec leur libellé.
const DICOM_FIELDS: { key: keyof DicomInfo; label: string }[] = [
  { key: 'patientName', label: 'Patient' },
  { key: 'patientId', label: 'ID patient' },
  { key: 'studyDate', label: 'Date étude' },
  { key: 'studyDescription', label: 'Étude' },
  { key: 'modality', label: 'Modalité' },
  { key: 'seriesDescription', label: 'Série' },
];

interface Ready {
  fileUrls: string[];
  user: MeasurementUser;
  store: MeasurementStore;
  /** Libellé de la source (étude/itemId) — le nombre de frames est résolu par le Viewer. */
  source: string;
}

// --- Mode standalone (dev, sans Girder) : ?standalone[=CT|Fluoro] ---
interface TestStudy {
  name: string;
  files: string[];
}

async function loadStandalone(requested: string | null): Promise<Ready> {
  const manifest = await fetch('/__testdata/manifest.json').then((r) => r.json());
  const studies: TestStudy[] = manifest.studies ?? [];
  if (studies.length === 0) throw new Error('Aucune étude dans test_data/.');
  const study = studies.find((s) => s.name === requested) ?? studies[0];

  const store = new LocalAnnotationStore(study.name);
  await store.load();
  return {
    fileUrls: study.files.map((p) => `/__testdata/file?path=${encodeURIComponent(p)}`),
    user: { id: 'dev', login: 'dev', name: 'Dev (standalone)' },
    store,
    source: `${study.name} [standalone]`,
  };
}

// --- Mode Girder (prod) : ?itemId=<girderItemId> ---
async function loadGirder(itemId: string): Promise<Ready> {
  // Fichiers DANS L'ORDRE DES COUPES (tri serveur via item.dicom.files, sinon repli).
  const [user, fileUrls] = await Promise.all([girder.me(), girder.orderedFileUrls(itemId)]);
  const store = new AnnotationStore(itemId);
  await store.load();
  return { fileUrls, user, store, source: `item ${itemId}` };
}

export default function App() {
  const standalone = params.has('standalone');
  const itemId = params.get('itemId');
  const [ready, setReady] = useState<Ready | null>(null);
  const [frameCount, setFrameCount] = useState<number | null>(null);
  const [currentFrame, setCurrentFrame] = useState(0);
  const [activeTool, setActiveTool] = useState<ActiveTool>('distance');
  const [measurements, setMeasurements] = useState<Measurement[]>([]);
  const [viewerApi, setViewerApi] = useState<ViewerApi | null>(null);
  const [dicomInfo, setDicomInfo] = useState<DicomInfo | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [config, setConfig] = useState<DmfConfig | null>(null);

  // Compression appliquée par le serveur : affichée dans le panneau d'infos (une image
  // transportée AVEC PERTE doit être signalée à qui mesure dessus).
  useEffect(() => {
    if (standalone) return; // pas de plugin Girder en mode autonome
    girder.config().then(setConfig).catch(() => undefined);
  }, [standalone]);

  // Raccourcis de sélection d'outil (touche simple, sans modificateur).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.target as HTMLElement)?.tagName === 'INPUT') return; // ne pas voler la saisie
      if (e.metaKey || e.ctrlKey || e.altKey || e.shiftKey) return; // laisser les raccourcis OS
      const tool = SHORTCUT_TO_TOOL[e.key.toLowerCase()];
      if (tool) {
        e.preventDefault();
        setActiveTool(tool);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  // Liste réactive des mesures + remontée des échecs de sauvegarde.
  useEffect(() => {
    if (!ready) return;
    const update = () => setMeasurements(ready.store.all());
    update();
    const unsubList = ready.store.subscribe(update);
    const unsubErr = ready.store.subscribeErrors((e) =>
      setSaveError(e instanceof Error ? e.message : String(e)),
    );
    return () => {
      unsubList();
      unsubErr();
    };
  }, [ready]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        if (standalone) return void apply(await loadStandalone(params.get('standalone')));
        if (itemId) return void apply(await loadGirder(itemId));
        setError('Fournir ?itemId=<id> (Girder) ou ?standalone[=CT] (test local).');
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      }
      function apply(r: Ready) {
        if (!cancelled) setReady(r);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [standalone, itemId]);

  if (error) return <div className="error">{error}</div>;
  if (!ready) {
    return (
      <div className="loading" role="status">
        <div className="spinner" aria-hidden="true" />
        <span>Chargement de l'examen…</span>
      </div>
    );
  }

  return (
    <div className="app">
      {saveError && (
        <div className="save-error" role="alert">
          Échec de l'enregistrement : {saveError}
          <button onClick={() => setSaveError(null)} aria-label="Fermer">
            ✕
          </button>
        </div>
      )}
      <div className="toolbar">
        {itemId && (
          <button
            className="back-girder"
            title="Retour à l'item Girder"
            onClick={() => {
              window.location.href = `/#item/${itemId}`;
            }}
          >
            ← Girder
          </button>
        )}
        {TOOLS.map((t) => (
          <button
            key={t.id}
            className={activeTool === t.id ? 'active' : ''}
            onClick={() => setActiveTool(t.id)}
            title={t.shortcut ? `Raccourci : ${t.shortcut.toUpperCase()}` : undefined}
          >
            {t.label}
            {t.shortcut && <span className="kbd">{t.shortcut.toUpperCase()}</span>}
          </button>
        ))}
        <button onClick={() => document.querySelector('.viewport')?.requestFullscreen?.()}>
          Plein écran
        </button>
        <span className="spacer" />
        <span className="user">
          {ready.source} — {frameCount ? `${currentFrame + 1}/${frameCount}` : '…'} image(s) ·{' '}
          {ready.user.name}
        </span>
      </div>
      <div className="body">
        <Viewer
          fileUrls={ready.fileUrls}
          activeTool={activeTool}
          store={ready.store}
          user={ready.user}
          onFrameChange={setCurrentFrame}
          onStackReady={setFrameCount}
          onApiReady={setViewerApi}
          onDicomInfo={setDicomInfo}
        />
        <aside className="panel">
          {dicomInfo && (
            <div className="dicom-info">
              <div className="dicom-head">Infos DICOM</div>
              {DICOM_FIELDS.filter((f) => dicomInfo[f.key] != null).map((f) => (
                <div className="dicom-row" key={f.key}>
                  <span className="dicom-key">{f.label}</span>
                  <span className="dicom-val">{String(dicomInfo[f.key])}</span>
                </div>
              ))}
              {dicomInfo.rows != null && (
                <div className="dicom-row">
                  <span className="dicom-key">Dimensions</span>
                  <span className="dicom-val">
                    {dicomInfo.columns} × {dicomInfo.rows}
                  </span>
                </div>
              )}
              {config && transportLabel(config) && (
                <div className={`dicom-row ${config.compression === 'lossy' ? 'warn' : ''}`}>
                  <span className="dicom-key">Transport</span>
                  <span className="dicom-val">{transportLabel(config)}</span>
                </div>
              )}
              {!DICOM_FIELDS.some((f) => dicomInfo[f.key] != null) && (
                <div className="dicom-row empty">Métadonnées limitées (projection)</div>
              )}
            </div>
          )}
          <div className="panel-head">Mesures ({measurements.length})</div>
          <ul className="mlist">
            {measurements.length === 0 && <li className="empty">Aucune mesure</li>}
            {measurements.map((m) => (
              <li
                key={m.id}
                className={`mrow ${m.frameIndex === currentFrame ? 'on-frame' : ''}`}
              >
                <button
                  className="mrow-go"
                  title="Aller à la coupe"
                  onClick={() => viewerApi?.gotoFrame(m.frameIndex)}
                >
                  <span className="mtype">{TYPE_LABEL[m.type]}</span>
                  <span className="mmeta">
                    coupe {m.frameIndex + 1} · {measurementSummary(m)}
                  </span>
                  <span className="msub">
                    {m.user?.name ?? '—'} · {formatTimestamp(m.createdAt)}
                  </span>
                </button>
                <div className="mrow-actions">
                  <input
                    className="mlabel"
                    defaultValue={m.label ?? ''}
                    placeholder="label…"
                    onBlur={(e) => viewerApi?.setLabel(m.id, e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') (e.target as HTMLInputElement).blur();
                    }}
                  />
                  <button
                    className="mdel"
                    title="Supprimer"
                    onClick={() => {
                      if (window.confirm('Supprimer cette mesure ?')) {
                        viewerApi?.deleteMeasurement(m.id);
                      }
                    }}
                  >
                    ✕
                  </button>
                </div>
              </li>
            ))}
          </ul>
        </aside>
      </div>
    </div>
  );
}
