# Image Girder + notre plugin, avec la SPA prébuild embarquée.
#
# Étape 1 : build de la SPA (Vite) → /web/dist
# Étape 2 : Girder + plugin (pip install) ; on copie dist/ dans le plugin (web_dist).

FROM node:20-bookworm AS web
WORKDIR /web
COPY web/package.json web/package-lock.json* ./
RUN npm install
COPY web/ ./
RUN npm run build

FROM girder/girder:latest
# Plugin Python + bundle SPA embarqué
COPY plugin /opt/dmf-plugin
COPY --from=web /web/dist /opt/dmf-plugin/girder_dicom_measure_flow/web_dist
RUN pip install --no-cache-dir /opt/dmf-plugin

# Injecte le lien « Annoter » dans l'index du client web Girder (idempotent).
RUN python3 - <<'PY'
import glob, os
TAG = '<script defer src="/dmf/girder-link.js"></script>'
candidates = glob.glob('/girder/girder/web/dist/index.html') + glob.glob('/girder/girder/web/index.html')
for path in candidates:
    if not os.path.isfile(path):
        continue
    html = open(path, encoding='utf-8').read()
    if 'dmf/girder-link.js' in html:
        continue
    html = html.replace('</body>', TAG + '</body>', 1) if '</body>' in html else html + TAG
    open(path, 'w', encoding='utf-8').write(html)
    print('[dmf] lien Annoter injecté dans', path)
PY
EXPOSE 8080
