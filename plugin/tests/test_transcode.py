"""Tests du transcodage des pixels avant envoi (pydicom seul, sans Girder).

Les jeux d'essai sont SYNTHÉTIQUES (dégradé + motif) : `test_data/` n'est pas versionné, et
un bruit aléatoire ne se compresserait pas (le transcodeur renverrait « aucun gain »).
"""

import io
import os

import numpy as np
import pydicom
import pytest
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import (
    ExplicitVRLittleEndian,
    JPEG2000,
    JPEGLSLossless,
    RLELossless,
    generate_uid,
)

import transcode as T  # chargé via conftest (dossier du package sur sys.path)

SOP_UID = "1.2.826.0.1.3680043.8.498.11111111111111111111111111111111"


def _dataset(rows=64, columns=64, frames=1, bits=16, samples=1):
    """DICOM non compressé, au contenu compressible (dégradé + bandes)."""
    ds = Dataset()
    ds.file_meta = FileMetaDataset()
    ds.file_meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.7"
    ds.file_meta.MediaStorageSOPInstanceUID = SOP_UID
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.SOPClassUID = ds.file_meta.MediaStorageSOPClassUID
    ds.SOPInstanceUID = SOP_UID
    ds.SeriesInstanceUID = generate_uid()
    ds.StudyInstanceUID = generate_uid()
    ds.Modality = "XA"
    ds.Rows, ds.Columns = rows, columns
    ds.SamplesPerPixel = samples
    ds.PhotometricInterpretation = "MONOCHROME2" if samples == 1 else "RGB"
    if samples == 3:
        ds.PlanarConfiguration = 0
    ds.BitsAllocated = bits
    ds.BitsStored = bits
    ds.HighBit = bits - 1
    ds.PixelRepresentation = 0
    if frames > 1:
        ds.NumberOfFrames = frames

    dtype = np.uint8 if bits == 8 else np.uint16
    shape = [frames, rows, columns] if frames > 1 else [rows, columns]
    if samples == 3:
        shape.append(3)
    ramp = np.arange(columns, dtype=np.uint32) * (((1 << bits) - 1) // max(columns - 1, 1))
    arr = np.broadcast_to(ramp, tuple(shape[:-1]) + (columns,)) if samples == 1 else None
    if samples == 3:
        arr = np.broadcast_to(ramp.reshape(1, columns, 1), tuple(shape))
    ds.PixelData = np.ascontiguousarray(arr.astype(dtype)).tobytes()
    return ds


def _bytes(ds):
    buf = io.BytesIO()
    ds.save_as(buf, enforce_file_format=True)
    return buf.getvalue()


@pytest.fixture
def uncompressed():
    return _bytes(_dataset(frames=8))


def test_lossless_shrinks_and_preserves_pixels(uncompressed):
    result = T.transcode(uncompressed, T.MODE_LOSSLESS)
    assert result is not None
    assert len(result.data) < len(uncompressed)
    assert result.ratio > 1

    out = pydicom.dcmread(io.BytesIO(result.data))
    assert out.file_meta.TransferSyntaxUID == JPEGLSLossless
    np.testing.assert_array_equal(out.pixel_array, pydicom.dcmread(io.BytesIO(uncompressed)).pixel_array)


def test_sop_instance_uid_is_preserved(uncompressed):
    """Les mesures déjà enregistrées référencent l'image par son SOPInstanceUID."""
    for mode in (T.MODE_LOSSLESS, T.MODE_LOSSY):
        out = pydicom.dcmread(io.BytesIO(T.transcode(uncompressed, mode).data))
        assert out.SOPInstanceUID == SOP_UID
        assert out.file_meta.MediaStorageSOPInstanceUID == SOP_UID


def test_lossy_targets_the_requested_ratio(uncompressed):
    result = T.transcode(uncompressed, T.MODE_LOSSY, ratio=10)
    assert result is not None
    out = pydicom.dcmread(io.BytesIO(result.data))
    assert out.file_meta.TransferSyntaxUID == JPEG2000
    # Le ratio porte sur le flux de pixels ; l'en-tête (constant) est en plus.
    assert result.ratio > 5
    assert out.LossyImageCompression == "01"
    assert float(out.LossyImageCompressionRatio) == 10.0
    assert out.LossyImageCompressionMethod == "ISO_15444_1"


def test_lossy_is_smaller_than_lossless(uncompressed):
    assert len(T.transcode(uncompressed, T.MODE_LOSSY, 10).data) < len(
        T.transcode(uncompressed, T.MODE_LOSSLESS).data
    )


def test_color_falls_back_to_lossless(uncompressed):
    """Le mode avec perte est réservé au monochrome (pas de transformation de composantes)."""
    data = _bytes(_dataset(bits=8, samples=3))
    result = T.transcode(data, T.MODE_LOSSY, 10)
    assert result is not None
    assert pydicom.dcmread(io.BytesIO(result.data)).file_meta.TransferSyntaxUID == JPEGLSLossless


def test_already_compressed_is_left_alone(uncompressed):
    ds = pydicom.dcmread(io.BytesIO(uncompressed))
    ds.compress(RLELossless, generate_instance_uid=False)
    assert T.transcode(_bytes(ds), T.MODE_LOSSLESS) is None


def test_inspect_reports_why_a_file_is_skipped(uncompressed):
    """La raison remonte jusqu'à l'en-tête `X-Dmf-Transfer` : elle doit être exacte."""
    assert T.inspect(io.BytesIO(uncompressed)) is None  # transcodable

    ds = pydicom.dcmread(io.BytesIO(uncompressed))
    ds.compress(RLELossless, generate_instance_uid=False)
    assert T.inspect(io.BytesIO(_bytes(ds))) == T.SKIP_ALREADY_COMPRESSED

    assert T.inspect(io.BytesIO(b"pas du DICOM" * 100)) == T.SKIP_NOT_DICOM

    ds = _dataset()
    ds.BitsAllocated = 32  # hors du domaine des encodeurs utilisés
    assert T.inspect(io.BytesIO(_bytes(ds))) == T.SKIP_UNSUPPORTED


def test_mode_none_and_non_dicom_and_size_guard(uncompressed):
    assert T.transcode(uncompressed, T.MODE_NONE) is None
    assert T.transcode(b"pas du DICOM" * 100, T.MODE_LOSSLESS) is None
    assert T.transcode(uncompressed, T.MODE_LOSSLESS, maxBytes=16) is None


def test_dataset_without_pixels_is_left_alone():
    ds = _dataset()
    del ds.PixelData
    assert T.transcode(_bytes(ds), T.MODE_LOSSLESS) is None


def test_normalize_mode():
    assert T.normalizeMode("lossy") == T.MODE_LOSSY
    assert T.normalizeMode("  LOSSLESS ") == T.MODE_LOSSLESS
    assert T.normalizeMode("none") == T.MODE_NONE
    for bogus in (None, "", "jpeg", 42):
        assert T.normalizeMode(bogus) == T.MODE_LOSSLESS


def test_cache_key_depends_on_every_input():
    base = T.cacheKey("fid", "rev", "lossy", 10)
    assert base == T.cacheKey("fid", "rev", "lossy", 10.0)
    assert base != T.cacheKey("fid2", "rev", "lossy", 10)
    assert base != T.cacheKey("fid", "rev2", "lossy", 10)
    assert base != T.cacheKey("fid", "rev", "lossless", 10)
    assert base != T.cacheKey("fid", "rev", "lossy", 12)


def test_cache_roundtrip_and_negative_entry(tmp_path):
    cache = T.TranscodeCache(str(tmp_path))
    assert cache.lookup("abc") == (False, None)

    cache.put("abc", b"hello")
    known, path = cache.lookup("abc")
    assert known and open(path, "rb").read() == b"hello"

    cache.put("def", b"")  # « examiné, non transcodable »
    assert cache.lookup("def") == (True, None)


def test_cache_evicts_least_recently_used(tmp_path):
    cache = T.TranscodeCache(str(tmp_path), maxBytes=2500)
    for name, age in [("old", 10_000), ("mid", 5_000), ("new", 0)]:
        cache.put(name, b"x" * 1000)
        stamp = 1_700_000_000 - age
        os.utime(cache._path(name), (stamp, stamp))

    cache.put("last", b"x" * 1000)  # 4 × 1000 o > 2500 → éviction des plus anciens
    assert cache.lookup("old") == (False, None)
    assert cache.lookup("new")[1] is not None
    assert cache.lookup("last")[1] is not None
