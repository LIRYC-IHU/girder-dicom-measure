"""Permet d'importer les modules PURS du plugin (dicom_tags) sans déclencher l'__init__
du package (qui importe Girder). On ajoute le dossier du package au sys.path."""

import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "girder_dicom_measure_flow")
)
