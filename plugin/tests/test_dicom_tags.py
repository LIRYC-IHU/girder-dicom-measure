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


# --- Objets DICOM sans pixels (PR, SR, DICOMDIR…) -------------------------------------------


def _part10(ds, sopClass, transferSyntax=None):
    """Sérialise `ds` au format fichier DICOM (préambule + méta) ; renvoie un flux relu."""
    import io

    from pydicom.dataset import FileMetaDataset
    from pydicom.uid import ExplicitVRLittleEndian, generate_uid

    ds.file_meta = FileMetaDataset()
    ds.file_meta.MediaStorageSOPClassUID = sopClass
    ds.file_meta.MediaStorageSOPInstanceUID = generate_uid()
    ds.file_meta.TransferSyntaxUID = transferSyntax or ExplicitVRLittleEndian
    ds.SOPClassUID = sopClass
    ds.SOPInstanceUID = ds.file_meta.MediaStorageSOPInstanceUID
    buf = io.BytesIO()
    ds.save_as(buf, enforce_file_format=True)
    buf.seek(0)
    return buf


def _image(**extra):
    ds = Dataset()
    ds.Modality = "XA"
    ds.Rows = ds.Columns = 4
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated = ds.BitsStored = 8
    ds.HighBit = 7
    ds.PixelRepresentation = 0
    ds.PixelData = bytes(16)
    for k, v in extra.items():
        setattr(ds, k, v)
    return ds


def _readHeader(fp):
    import pydicom

    return pydicom.dcmread(fp, defer_size=1024, stop_before_pixels=True)


@pytest.mark.parametrize("transferSyntax", ["1.2.840.10008.1.2", "1.2.840.10008.1.2.1"])
def test_at_pixel_data_true_for_an_image(transferSyntax):
    fp = _part10(_image(), "1.2.840.10008.5.1.4.1.1.12.1", transferSyntax)  # XA
    ds = _readHeader(fp)
    position = fp.tell()
    assert dicom_tags.at_pixel_data(fp, ds) is True
    assert fp.tell() == position  # flux restauré : la lecture suivante n'est pas décalée


def test_at_pixel_data_true_for_float_pixel_data():
    ds = _image()
    del ds.PixelData
    ds.BitsAllocated = 32
    ds.FloatPixelData = bytes(64)
    fp = _part10(ds, "1.2.840.10008.5.1.4.1.1.30")  # Parametric Map
    assert dicom_tags.at_pixel_data(fp, _readHeader(fp)) is True


def test_at_pixel_data_false_for_a_presentation_state():
    """Cas DEFINE-PFA (Rhéna) : 0001.dcm = Grayscale Softcopy Presentation State."""
    ds = Dataset()
    ds.Modality = "PR"
    ds.ContentLabel = "PS"
    ds.ImagerPixelSpacing = [0.2, 0.2]  # absent des PR réels ; ne fait pas une image
    fp = _part10(ds, "1.2.840.10008.5.1.4.1.1.11.1")
    assert dicom_tags.at_pixel_data(fp, _readHeader(fp)) is False


def test_at_pixel_data_false_for_a_structured_report():
    ds = Dataset()
    ds.Modality = "SR"
    ds.ValueType = "CONTAINER"
    fp = _part10(ds, "1.2.840.10008.5.1.4.1.1.88.22")
    assert dicom_tags.at_pixel_data(fp, _readHeader(fp)) is False


def test_at_pixel_data_ignores_nested_pixel_data():
    """Une icône (séquence imbriquée, comme dans un DICOMDIR) n'en fait pas une image."""
    from pydicom.sequence import Sequence

    ds = Dataset()
    ds.Modality = "KO"
    ds.IconImageSequence = Sequence([_image()])
    fp = _part10(ds, "1.2.840.10008.5.1.4.1.1.88.59")
    assert dicom_tags.at_pixel_data(fp, _readHeader(fp)) is False


def test_at_pixel_data_false_when_trailing_padding_follows_the_header():
    ds = Dataset()
    ds.Modality = "PR"
    ds.add_new(0xFFFCFFFC, "OB", bytes(8))  # Data Set Trailing Padding
    fp = _part10(ds, "1.2.840.10008.5.1.4.1.1.11.1")
    assert dicom_tags.at_pixel_data(fp, _readHeader(fp)) is False


@pytest.mark.parametrize("withPixels", [True, False])
def test_at_pixel_data_with_deflated_transfer_syntax(withPixels):
    """Dataset compressé (zlib) après la méta : la position du flux brut ne veut rien dire."""
    from pydicom.uid import DeflatedExplicitVRLittleEndian

    ds = _image() if withPixels else Dataset()
    if not withPixels:
        ds.Modality = "SR"
    sopClass = "1.2.840.10008.5.1.4.1.1.7" if withPixels else "1.2.840.10008.5.1.4.1.1.88.22"
    fp = _part10(ds, sopClass, DeflatedExplicitVRLittleEndian)
    header = _readHeader(fp)
    position = fp.tell()
    assert dicom_tags.at_pixel_data(fp, header) is withPixels
    assert fp.tell() == position


def test_at_pixel_data_with_a_private_transfer_syntax():
    """Syntaxe privée : pas d'exception (elle ferait ignorer le fichier), lecture petit-boutiste."""
    fp = _part10(_image(), "1.2.840.10008.5.1.4.1.1.12.1")
    ds = _readHeader(fp)
    ds.file_meta.TransferSyntaxUID = "1.2.3.4.5.6.7"
    assert dicom_tags.at_pixel_data(fp, ds) is True
