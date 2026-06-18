"""Tests des fonctions pures d'extraction/tri DICOM (pydicom seul, sans Girder)."""

import datetime

import pytest
from pydicom.dataset import Dataset
from pydicom.valuerep import PersonName

import dicom_tags  # chargé via conftest (dossier du package sur sys.path)


def test_coerce_value_base_types():
    assert dicom_tags.coerce_value(3) == 3
    assert dicom_tags.coerce_value(2.5) == 2.5
    assert dicom_tags.coerce_value("CT") == "CT"
    assert dicom_tags.coerce_value(b"abc") == b"abc"
    today = datetime.date(2024, 1, 2)
    assert dicom_tags.coerce_value(today) == today


def test_coerce_value_person_name():
    assert dicom_tags.coerce_value(PersonName("DOE^JANE")) == "DOE^JANE"


def test_coerce_value_multivalue():
    ds = Dataset()
    ds.PixelSpacing = [0.5, 0.5]  # VR=DS, MultiValue
    assert dicom_tags.coerce_value(ds.PixelSpacing) == [0.5, 0.5]


def test_coerce_value_rejects_binary_with_null():
    with pytest.raises(ValueError):
        dicom_tags.coerce_value(b"\x00\x01")


def test_coerce_value_rejects_unknown():
    with pytest.raises(ValueError):
        dicom_tags.coerce_value(object())


def test_coerce_metadata_keywords_private_and_group_length():
    ds = Dataset()
    ds.PatientName = "DOE^JANE"
    ds.Modality = "CT"
    ds.Rows = 512
    ds.add_new(0x00090010, "LO", "SIEMENS")  # tag privé → clé = str(tag)
    ds.add_new(0x00080000, "UL", 0)  # group length (element 0) → ignoré

    meta = dicom_tags.coerce_metadata(ds)
    assert meta["PatientName"] == "DOE^JANE"
    assert meta["Modality"] == "CT"
    assert meta["Rows"] == 512
    assert "(0009,0010)" in meta  # tag privé conservé sous sa forme numérique
    assert all("0008,0000" not in k for k in meta)  # group length exclu


def test_sortable_none_last():
    items = [3, None, 1, None, 2]
    assert sorted(items, key=dicom_tags.sortable) == [1, 2, 3, None, None]


def test_sort_key_orders_by_series_then_instance():
    files = [
        {"name": "c.dcm", "dicom": {"SeriesNumber": 1, "InstanceNumber": 11}},
        {"name": "a.dcm", "dicom": {"SeriesNumber": 1, "InstanceNumber": 3}},
        {"name": "b.dcm", "dicom": {"SeriesNumber": 1, "InstanceNumber": 7}},
        {"name": "z.dcm", "dicom": {"SeriesNumber": 2, "InstanceNumber": 1}},
    ]
    ordered = [f["name"] for f in sorted(files, key=dicom_tags.sort_key)]
    assert ordered == ["a.dcm", "b.dcm", "c.dcm", "z.dcm"]


def test_sort_key_handles_missing_fields():
    files = [
        {"name": "b.dcm", "dicom": {}},
        {"name": "a.dcm", "dicom": {"InstanceNumber": 1}},
    ]
    ordered = [f["name"] for f in sorted(files, key=dicom_tags.sort_key)]
    assert ordered == ["a.dcm", "b.dcm"]  # valeur avant None
