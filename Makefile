.PHONY: install dev build test clean

# Installe les deps de la SPA
install:
	cd web && npm install

# Lance la SPA en dev (vite) — nécessite un Girder accessible (cf. web/.env)
dev:
	cd web && npm run dev

# Construit la SPA et l'embarque dans le plugin Girder (web/dist → plugin/.../web_dist)
build:
	cd web && npm run build
	rm -rf plugin/girder_dicom_measure_flow/web_dist
	cp -r web/dist plugin/girder_dicom_measure_flow/web_dist

# Suite du plugin. Les tests d'intégration (Girder + MongoDB) sont ignorés si girder n'est
# pas installé ou si aucun MongoDB ne répond sur DMF_TEST_MONGO_URI (défaut localhost:27017).
PYTHON ?= python3
test:
	cd plugin && $(PYTHON) -m pytest -q

clean:
	rm -rf web/dist plugin/girder_dicom_measure_flow/web_dist web/node_modules
