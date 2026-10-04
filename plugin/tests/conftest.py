"""Permet d'importer les modules PURS du plugin (dicom_tags) sans déclencher l'__init__
du package (qui importe Girder). On ajoute le dossier du package au sys.path.

Le dossier parent est ajouté aussi : les tests d'intégration importent le PACKAGE
(`girder_dicom_measure_flow.dicom_metadata`, imports relatifs) sans exiger qu'il soit installé.
"""

import os
import sys

_here = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(_here, ".."))
sys.path.insert(0, os.path.join(_here, "..", "girder_dicom_measure_flow"))
