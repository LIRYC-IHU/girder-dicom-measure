import { defineConfig, type Plugin } from 'vite';
import react from '@vitejs/plugin-react';
import { readdirSync, statSync, createReadStream, existsSync } from 'node:fs';
import { resolve, join, extname } from 'node:path';

// Répertoire des données de test (à la racine du repo, hors `web/`).
const TEST_DATA_DIR = resolve(__dirname, '..', 'test_data');

/**
 * Plugin DEV : sert test_data/ pour tester le viewer en standalone (sans Girder).
 *  - GET /__testdata/manifest.json → { studies: [{ name, files: [relPath...] }] }
 *  - GET /__testdata/file?path=CT/CT000000.dcm → le fichier DICOM (octets bruts)
 */
function testDataPlugin(): Plugin {
  const listStudies = () => {
    if (!existsSync(TEST_DATA_DIR)) return [];
    return readdirSync(TEST_DATA_DIR)
      .filter((name) => !name.startsWith('.') && statSync(join(TEST_DATA_DIR, name)).isDirectory())
      .map((name) => {
        const dir = join(TEST_DATA_DIR, name);
        const files = readdirSync(dir)
          .filter((f) => extname(f).toLowerCase() === '.dcm')
          .sort() // tri lexical = ordre des coupes (noms zero-paddés) pour le test
          .map((f) => `${name}/${f}`);
        return { name, files };
      })
      .filter((s) => s.files.length > 0);
  };

  return {
    name: 'dmf-test-data',
    apply: 'serve',
    configureServer(server) {
      server.middlewares.use('/__testdata', (req, res) => {
        const url = new URL(req.url ?? '', 'http://localhost');

        if (url.pathname === '/manifest.json') {
          res.setHeader('Content-Type', 'application/json');
          res.end(JSON.stringify({ studies: listStudies() }));
          return;
        }

        if (url.pathname === '/file') {
          const rel = url.searchParams.get('path') ?? '';
          const abs = resolve(TEST_DATA_DIR, rel);
          // Anti path-traversal : rester sous TEST_DATA_DIR.
          if (!abs.startsWith(TEST_DATA_DIR) || !existsSync(abs)) {
            res.statusCode = 404;
            res.end('not found');
            return;
          }
          res.setHeader('Content-Type', 'application/dicom');
          createReadStream(abs).pipe(res);
          return;
        }

        res.statusCode = 404;
        res.end('not found');
      });
    },
  };
}

// Base RELATIVE en prod (`./`) : la SPA fonctionne quel que soit le chemin de montage
// (`/dmf/`, un chemin custom, ou un préfixe ajouté par un reverse-proxy) sans rebuild.
// En dev, on sert à la racine, on proxifie /api vers un Girder de dev et on expose test_data.
export default defineConfig(({ command }) => ({
  base: command === 'build' ? './' : '/',
  plugins: [react(), testDataPlugin()],
  optimizeDeps: {
    // Catch-22 Vite + dicom-image-loader :
    //  - on EXCLUT le loader pour que `new Worker(new URL('./...Worker.js', import.meta.url))`
    //    résolve depuis les vraies sources (sinon worker jamais lancé → décodage bloqué) ;
    //  - on INCLUT les codecs (glue CJS) pour l'interop CJS→ESM (sinon « no default export »).
    exclude: ['@cornerstonejs/dicom-image-loader'],
    // Sous-chemins EXACTS importés par les décodeurs (UMD → esbuild synthétise le default).
    include: [
      'dicom-parser',
      '@cornerstonejs/codec-charls/decodewasmjs',
      '@cornerstonejs/codec-libjpeg-turbo-8bit/decodewasmjs',
      '@cornerstonejs/codec-openjpeg/decodewasmjs',
      '@cornerstonejs/codec-openjph/wasmjs',
    ],
  },
  worker: { format: 'es' },
  server: {
    proxy: {
      '/api': {
        target: process.env.VITE_GIRDER_TARGET ?? 'http://localhost:8080',
        changeOrigin: true,
      },
    },
  },
}));
