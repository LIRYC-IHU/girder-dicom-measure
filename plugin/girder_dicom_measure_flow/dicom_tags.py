"""Fonctions PURES de manipulation de tags DICOM (aucune dépendance Girder/Mongo).

Isolées ici pour être testables unitairement avec pydicom seul (cf. tests/test_dicom_tags.py).
Utilisées par `dicom_metadata.py` (extraction + tri à l'upload / au backfill).
"""

import datetime

import pydicom
import pydicom.multival
import pydicom.sequence
import pydicom.valuerep

# Types temporels : pydicom renvoie des chaînes pour les VR de date DICOM, mais on les
# accepte tels quels par sécurité (on ne peut pas les reconstruire via leur constructeur).
_PASSTHROUGH_TYPES = (datetime.datetime, datetime.date, datetime.time)
# Types « scalaires » pydicom (IS, DS, str spéciaux…) → on normalise vers le type Python de base.
_NORMALIZED_TYPES = (int, float, bytes, str)


def coerce_value(value):
    """Convertit une valeur pydicom en type JSON-stockable, ou lève ValueError."""
    if isinstance(value, bytes):
        if b"\x00" in value:
            raise ValueError("Binary data with null")
        try:
            value.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError("Binary data that cannot be stored as utf-8")

    if isinstance(value, _PASSTHROUGH_TYPES):
        return value

    for base in _NORMALIZED_TYPES:
        if isinstance(value, base):
            return base(value)

    if isinstance(value, pydicom.valuerep.PersonName):
        return str(value)

    if isinstance(value, pydicom.multival.MultiValue):
        if isinstance(value, pydicom.sequence.Sequence):
            raise ValueError("Cannot coerce a Sequence")
        return list(map(coerce_value, value))

    raise ValueError("Unknown type", type(value))


def coerce_metadata(dataset):
    """Dictionnaire {keyword: valeur} des tags exploitables d'un Dataset pydicom."""
    metadata = {}
    for tag in dataset.keys():
        try:
            data_element = dataset[tag]
        except OSError:
            continue
        if data_element.tag.element == 0:  # group length
            continue

        tag_key = (
            data_element.keyword
            if data_element.keyword and not data_element.tag.is_private
            else str(data_element.tag)
        )
        try:
            metadata[tag_key] = coerce_value(data_element.value)
        except ValueError:
            continue
    return metadata


def sortable(value):
    """Clé de tri tolérante aux None (qui finissent en dernier)."""
    return (value is None, value if value is not None else 0)


def sort_key(file_entry):
    """Clé de tri des fichiers d'un item : (SeriesNumber, InstanceNumber, SliceLocation, name)."""
    meta = file_entry.get("dicom") or {}
    return (
        sortable(meta.get("SeriesNumber")),
        sortable(meta.get("InstanceNumber")),
        sortable(meta.get("SliceLocation")),
        file_entry.get("name") or "",
    )
