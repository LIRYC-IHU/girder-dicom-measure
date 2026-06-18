// Persistance des annotations.
//
// PROD (Girder) : collection dédiée `dmf_annotation` via les routes /api/v1/dmf/annotation
// → persistance UNITAIRE par mesure (POST/PUT/DELETE), interrogeable par l'API.
// DEV (standalone) : localStorage (liste entière réécrite).
// Les stores sont OBSERVABLES (subscribe) → la liste de l'UI se met à jour automatiquement.

import { girder } from '../api/girder';
import type { Measurement } from './types';

/** Contrat consommé par le Viewer et le panneau de mesures. */
export interface MeasurementStore {
  load(): Promise<Measurement[]>;
  all(): Measurement[];
  add(m: Measurement): Promise<unknown>;
  update(id: string, patch: Partial<Measurement>): Promise<unknown>;
  remove(id: string): Promise<unknown>;
  /** S'abonner aux changements de la liste. Renvoie une fonction de désabonnement. */
  subscribe(cb: () => void): () => void;
  /** S'abonner aux échecs de persistance (réseau, droits, CSRF…). */
  subscribeErrors(cb: (error: unknown) => void): () => void;
}

/**
 * Base : état en mémoire + observateurs. La persistance est UNITAIRE (par mesure), déléguée
 * aux sous-classes ; les écritures sont sérialisées pour préserver l'ordre.
 */
abstract class BaseStore implements MeasurementStore {
  protected items: Measurement[] = [];
  private listeners = new Set<() => void>();
  private errorListeners = new Set<(error: unknown) => void>();
  private writeChain: Promise<unknown> = Promise.resolve();

  abstract load(): Promise<Measurement[]>;
  protected abstract persistAdd(m: Measurement): Promise<unknown>;
  protected abstract persistUpdate(id: string, patch: Partial<Measurement>): Promise<unknown>;
  protected abstract persistRemove(id: string): Promise<unknown>;

  all(): Measurement[] {
    return [...this.items];
  }

  add(m: Measurement): Promise<unknown> {
    this.items = [...this.items, m];
    this.notify();
    return this.enqueue(() => this.persistAdd(m));
  }

  update(id: string, patch: Partial<Measurement>): Promise<unknown> {
    this.items = this.items.map((m) => (m.id === id ? { ...m, ...patch } : m));
    this.notify();
    return this.enqueue(() => this.persistUpdate(id, patch));
  }

  remove(id: string): Promise<unknown> {
    this.items = this.items.filter((m) => m.id !== id);
    this.notify();
    return this.enqueue(() => this.persistRemove(id));
  }

  subscribe(cb: () => void): () => void {
    this.listeners.add(cb);
    return () => this.listeners.delete(cb);
  }

  subscribeErrors(cb: (error: unknown) => void): () => void {
    this.errorListeners.add(cb);
    return () => this.errorListeners.delete(cb);
  }

  protected notify(): void {
    this.listeners.forEach((cb) => cb());
  }

  /**
   * Sérialise les écritures (évite les races entre add/update/remove rapprochés). Un échec
   * est signalé aux abonnés `subscribeErrors` (au lieu d'être avalé silencieusement) sans
   * casser la chaîne des opérations suivantes.
   */
  private enqueue(op: () => Promise<unknown>): Promise<unknown> {
    this.writeChain = this.writeChain
      .catch(() => undefined)
      .then(op)
      .catch((error) => {
        this.errorListeners.forEach((cb) => cb(error));
      });
    return this.writeChain;
  }
}

/** Store de PRODUCTION : collection Girder `dmf_annotation`. */
export class AnnotationStore extends BaseStore {
  constructor(private readonly itemId: string) {
    super();
  }

  async load(): Promise<Measurement[]> {
    this.items = await girder.listAnnotations<Measurement>(this.itemId);
    this.notify();
    return this.all();
  }

  protected persistAdd(m: Measurement): Promise<unknown> {
    return girder.createAnnotation(this.itemId, m);
  }

  protected persistUpdate(id: string, patch: Partial<Measurement>): Promise<unknown> {
    return girder.updateAnnotation(id, patch);
  }

  protected persistRemove(id: string): Promise<unknown> {
    return girder.deleteAnnotation(id);
  }
}

/** Store de DÉVELOPPEMENT : localStorage (clé par étude). */
export class LocalAnnotationStore extends BaseStore {
  private readonly key: string;

  constructor(studyKey: string) {
    super();
    this.key = `dmf:annotations:${studyKey}`;
  }

  async load(): Promise<Measurement[]> {
    try {
      this.items = JSON.parse(localStorage.getItem(this.key) ?? '[]');
    } catch {
      this.items = [];
    }
    this.notify();
    return this.all();
  }

  // localStorage : on réécrit simplement la liste entière à chaque opération.
  private save(): Promise<unknown> {
    localStorage.setItem(this.key, JSON.stringify(this.items));
    return Promise.resolve();
  }
  protected persistAdd(): Promise<unknown> {
    return this.save();
  }
  protected persistUpdate(): Promise<unknown> {
    return this.save();
  }
  protected persistRemove(): Promise<unknown> {
    return this.save();
  }
}
