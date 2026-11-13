"""Encodage JPEG 2000 exécuté dans un PROCESSUS FILS (cf. `transcode._encodeInWorker`).

Pourquoi un processus : l'encodeur openjpeg (pylibjpeg-openjpeg) n'est pas thread-safe — deux
encodages simultanés dans un même processus le font planter en segfault. Des processus
séparés ont chacun leur encodeur : on retrouve le parallélisme, et un crash natif ne tue plus
que le processus fils, plus Girder.

Pourquoi un module de PREMIER NIVEAU, hors du package `girder_dicom_measure_flow` : un
processus lancé en `spawn` ré-importe le module de la fonction qu'on lui confie. Dans le
package, cela exécuterait son `__init__`, donc importerait tout Girder dans chaque worker.
Ici : pydicom seul.
"""

import io

import pydicom


def encode(data, transferSyntax, kwargs):
    """DICOM non compressé (bytes) → même DICOM encodé en `transferSyntax` (bytes)."""
    ds = pydicom.dcmread(io.BytesIO(data))
    ds.compress(transferSyntax, generate_instance_uid=False, **kwargs)
    out = io.BytesIO()
    ds.save_as(out, enforce_file_format=True)
    return out.getvalue()
