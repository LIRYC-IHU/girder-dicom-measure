.PHONY: install dev build clean

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

clean:
	rm -rf web/dist plugin/girder_dicom_measure_flow/web_dist web/node_modules
