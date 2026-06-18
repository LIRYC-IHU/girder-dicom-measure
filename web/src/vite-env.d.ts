/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Base de l'API Girder (défaut: /api/v1). */
  readonly VITE_GIRDER_API?: string;
  /** Token Girder de DEV uniquement (jamais en prod, où le cookie de session suffit). */
  readonly VITE_GIRDER_TOKEN?: string;
  /** Cible du proxy dev (défaut: http://localhost:8080). */
  readonly VITE_GIRDER_TARGET?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
