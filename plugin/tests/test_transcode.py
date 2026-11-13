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
from pydicom.multival import MultiValue
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


# --- Découpe frame par frame (affichage progressif) -------------------------


def test_frame_layout_locates_each_frame(uncompressed):
    layout = T.frameLayout(io.BytesIO(uncompressed))
    assert layout is not None
    assert layout.frames == 8
    assert layout.frameSize == 64 * 64 * 2  # 64×64, 16 bits, monochrome

    # L'offset annoncé doit vraiment tomber sur les pixels : on compare les octets extraits
    # à la frame correspondante du tableau source.
    source = pydicom.dcmread(io.BytesIO(uncompressed)).pixel_array
    for index in (0, 3, 7):
        raw = uncompressed[layout.offsetOf(index):layout.offsetOf(index) + layout.frameSize]
        np.testing.assert_array_equal(
            np.frombuffer(raw, dtype=np.uint16).reshape(64, 64), source[index]
        )
    # La dernière frame se termine exactement à la fin des pixels déclarés.
    assert layout.offsetOf(8) == layout.valueOffset + 8 * layout.frameSize


def test_declared_frames_is_readable_even_when_splitting_declines():
    """Régression : le nombre de frames DÉCLARÉ doit rester lisible dans tous les cas.

    C'est ce qui distingue « ce fichier est mono-frame » de « je ne sais pas » — la confusion
    entre les deux a fait servir des boucles entières comme une image unique en production.
    """
    assert T.declaredFrames(io.BytesIO(_bytes(_dataset(frames=8)))) == 8
    assert T.declaredFrames(io.BytesIO(_bytes(_dataset()))) == 1  # tag absent → 1

    # Déjà compressé : la découpe ne s'applique pas, mais le nombre déclaré reste lisible.
    ds = pydicom.dcmread(io.BytesIO(_bytes(_dataset(frames=8))))
    ds.compress(RLELossless, generate_instance_uid=False)
    assert T.frameLayout(io.BytesIO(_bytes(ds))) is None
    assert T.declaredFrames(io.BytesIO(_bytes(ds))) == 8

    assert T.declaredFrames(io.BytesIO(b"pas du DICOM" * 100)) is None


def test_frame_layout_declines_what_it_cannot_split():
    """Mono-frame, déjà compressé, non-DICOM : le fichier entier reste le bon grain."""
    assert T.frameLayout(io.BytesIO(_bytes(_dataset()))) is None  # une seule frame
    assert T.frameLayout(io.BytesIO(b"pas du DICOM" * 100)) is None

    ds = pydicom.dcmread(io.BytesIO(_bytes(_dataset(frames=4))))
    ds.compress(RLELossless, generate_instance_uid=False)
    assert T.frameLayout(io.BytesIO(_bytes(ds))) is None  # encapsulé


def test_built_frame_is_a_faithful_single_frame_dicom(uncompressed):
    layout = T.frameLayout(io.BytesIO(uncompressed))
    source = pydicom.dcmread(io.BytesIO(uncompressed)).pixel_array
    index = 5
    raw = uncompressed[layout.offsetOf(index):layout.offsetOf(index) + layout.frameSize]

    result = T.buildFrame(layout, raw, index, T.MODE_LOSSLESS)
    assert result is not None
    out = pydicom.dcmread(io.BytesIO(result.data))
    assert int(out.NumberOfFrames) == 1
    assert out.file_meta.TransferSyntaxUID == JPEGLSLossless
    # Sans perte → les pixels de la frame servie sont ceux de la source.
    np.testing.assert_array_equal(out.pixel_array, source[index])
    # Même SOPInstanceUID : les mesures référencent l'image par cet UID + le n° de frame.
    assert out.SOPInstanceUID == SOP_UID
    # Une frame seule pèse une fraction de la boucle entière.
    assert len(result.data) < len(uncompressed) / 4


def test_built_frame_can_be_lossy(uncompressed):
    layout = T.frameLayout(io.BytesIO(uncompressed))
    raw = uncompressed[layout.offsetOf(0):layout.offsetOf(0) + layout.frameSize]
    out = pydicom.dcmread(
        io.BytesIO(T.buildFrame(layout, raw, 0, T.MODE_LOSSY, 10).data)
    )
    assert out.file_meta.TransferSyntaxUID == JPEG2000
    assert out.LossyImageCompression == "01"


def test_built_frame_trims_per_frame_attributes():
    """Une instance qui annonce 1 frame ne doit pas décrire les N de la source."""
    ds = _dataset(frames=4)
    ds.FrameIncrementPointer = 0x00181065
    ds.FrameTimeVector = [10.0, 20.0, 30.0, 40.0]
    data = _bytes(ds)
    layout = T.frameLayout(io.BytesIO(data))
    raw = data[layout.offsetOf(2):layout.offsetOf(2) + layout.frameSize]

    out = pydicom.dcmread(io.BytesIO(T.buildFrame(layout, raw, 2, T.MODE_LOSSLESS).data))
    # pydicom réduit une valeur multiple à un scalaire quand il n'en reste qu'une.
    kept = out.FrameTimeVector
    assert [float(v) for v in (kept if isinstance(kept, MultiValue) else [kept])] == [30.0]


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
    # Le fichier entier et chacune de ses frames sont des entrées distinctes.
    assert base != T.cacheKey("fid", "rev", "lossy", 10, frame=0)
    assert T.cacheKey("fid", "rev", "lossy", 10, frame=0) != T.cacheKey(
        "fid", "rev", "lossy", 10, frame=1
    )


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


# --- Empreinte des pixels ---------------------------------------------------------


def _digest(data, **kwargs):
    return T.pixelDataDigest(io.BytesIO(data), **kwargs)


def test_pixel_digest_is_sha256_of_pixel_value():
    import hashlib

    ds = _dataset(frames=3)
    assert _digest(_bytes(ds)) == hashlib.sha256(ds.PixelData).hexdigest()


def test_pixel_digest_ignores_header_changes():
    """Même image ré-exportée (UIDs régénérés, patient pseudonymisé) → même empreinte."""
    a, b = _dataset(frames=3), _dataset(frames=3)
    b.SOPInstanceUID = b.file_meta.MediaStorageSOPInstanceUID = generate_uid()
    b.SeriesInstanceUID = generate_uid()
    b.PatientID, b.PatientName = "8-5", "DEFINE-PFA^8-5"
    assert _digest(_bytes(a)) == _digest(_bytes(b))


def test_pixel_digest_same_for_implicit_and_explicit_vr():
    from pydicom.uid import ImplicitVRLittleEndian

    explicit = _dataset(frames=2)
    implicit = _dataset(frames=2)
    implicit.file_meta.TransferSyntaxUID = ImplicitVRLittleEndian
    buf = io.BytesIO()
    implicit.save_as(buf, enforce_file_format=True, implicit_vr=True, little_endian=True)
    assert _digest(buf.getvalue()) == _digest(_bytes(explicit))


def test_pixel_digest_differs_for_different_pixels():
    a, b = _dataset(frames=2), _dataset(frames=2)
    pixels = bytearray(b.PixelData)
    pixels[len(pixels) // 2] ^= 0xFF
    b.PixelData = bytes(pixels)
    assert _digest(_bytes(a)) != _digest(_bytes(b))


def test_pixel_digest_independent_of_chunk_size():
    data = _bytes(_dataset(frames=8))
    assert _digest(data, chunkSize=1000) == _digest(data)


def test_pixel_digest_encapsulated_matches_pydicom_and_is_stable():
    import hashlib

    ds = _dataset(frames=2)
    ds.compress(RLELossless)
    data = _bytes(ds)
    first = _digest(data)
    assert first == hashlib.sha256(pydicom.dcmread(io.BytesIO(data)).PixelData).hexdigest()
    assert _digest(data, chunkSize=100) == first
    ds.SOPInstanceUID = ds.file_meta.MediaStorageSOPInstanceUID = generate_uid()
    assert _digest(_bytes(ds)) == first


def test_pixel_digest_none_without_pixels_or_dicom():
    ds = _dataset()
    del ds.PixelData
    assert _digest(_bytes(ds)) is None       # SR, PR… : pas de pixels
    assert _digest(b"pas un DICOM") is None


# --- Concurrence : l'encodeur JPEG 2000 (openjpeg) n'est pas thread-safe -----------------
# En prod (pylibjpeg-openjpeg 2.6.0), 4 encodages J2K simultanés dans le processus Girder le
# faisaient planter en segfault. Les encodages J2K partent donc dans un pool de PROCESSUS
# (`dmf.j2k_workers`) ; à défaut de pool, ils sont sérialisés dans le processus courant.


@pytest.fixture
def j2kWorkers():
    """Règle la taille du pool pour un test, puis revient à 0 (module pur : pas de pool)."""
    yield T.configureJ2kWorkers
    T.configureJ2kWorkers(0)


def _crashingEncode(data, transferSyntax, kwargs):
    """Remplace `dmf_j2k_worker.encode` DANS le worker : simule un crash natif."""
    os._exit(139)


def _maxConcurrentCompress(monkeypatch, transferSyntax, threads=8):
    import threading
    import time

    state = {"now": 0, "max": 0}
    guard = threading.Lock()

    def fakeCompress(self, uid, **kwargs):
        with guard:
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
        time.sleep(0.02)  # laisse aux autres threads le temps d'entrer s'ils le peuvent
        with guard:
            state["now"] -= 1

    monkeypatch.setattr(Dataset, "compress", fakeCompress)
    start = threading.Barrier(threads)

    def run():
        start.wait()
        T._compress(Dataset(), transferSyntax)

    pool = [threading.Thread(target=run) for _ in range(threads)]
    for t in pool:
        t.start()
    for t in pool:
        t.join()
    return state["max"]


@pytest.mark.parametrize("uid", [JPEG2000, pydicom.uid.JPEG2000Lossless])
def test_in_process_j2k_encodes_are_serialized(monkeypatch, uid):
    # Sans pool (défaut du module pur, ou `dmf.j2k_workers = 0`) : un seul à la fois.
    assert _maxConcurrentCompress(monkeypatch, uid) == 1


@pytest.mark.parametrize("uid", [JPEGLSLossless, RLELossless])
def test_other_codecs_stay_parallel(monkeypatch, uid):
    assert _maxConcurrentCompress(monkeypatch, uid) > 1


def _requireJ2k():
    pytest.importorskip("openjpeg")
    pytest.importorskip("pylibjpeg")


@pytest.mark.parametrize("mode", [T.MODE_LOSSY, T.MODE_LOSSLESS])
def test_pool_output_is_identical_to_in_process(uncompressed, j2kWorkers, monkeypatch, mode):
    _requireJ2k()
    if mode == T.MODE_LOSSLESS:  # force le J2K sans perte (JPEG-LS passe avant sinon)
        monkeypatch.setattr(T, "_LOSSLESS_CHAIN", (pydicom.uid.JPEG2000Lossless,))
    local = T.transcode(uncompressed, mode=mode, ratio=10)
    j2kWorkers(2)
    pooled = T.transcode(uncompressed, mode=mode, ratio=10)
    assert T._pool is not None  # l'encodage est bien passé par le pool
    assert pooled.transferSyntax == local.transferSyntax
    assert pooled.data == local.data  # même encodeur, mêmes paramètres → mêmes octets
    out = pydicom.dcmread(io.BytesIO(pooled.data))
    assert out.SOPInstanceUID == SOP_UID
    if mode == T.MODE_LOSSY:
        assert out.LossyImageCompression == "01"


def test_built_frame_through_pool(uncompressed, j2kWorkers):
    _requireJ2k()
    j2kWorkers(2)
    layout = T.frameLayout(io.BytesIO(uncompressed))
    data = uncompressed[layout.offsetOf(3):layout.offsetOf(3) + layout.frameSize]
    out = T.buildFrame(layout, data, 3, mode=T.MODE_LOSSY, ratio=10)
    ds = pydicom.dcmread(io.BytesIO(out.data))
    assert ds.file_meta.TransferSyntaxUID == JPEG2000
    assert int(ds.NumberOfFrames) == 1 and ds.SOPInstanceUID == SOP_UID


def test_crashed_worker_falls_back_then_pool_recovers(uncompressed, j2kWorkers, monkeypatch,
                                                      caplog):
    _requireJ2k()
    j2kWorkers(2)
    # Le worker importe `_crashingEncode` par son nom (spawn) : il meurt à la première tâche.
    monkeypatch.setattr(T.dmf_j2k_worker, "encode", _crashingEncode)
    with caplog.at_level("ERROR", logger="girder.dicom_measure_flow"):
        out = T.transcode(uncompressed, mode=T.MODE_LOSSY, ratio=10)
    assert out is not None and out.transferSyntax == str(JPEG2000)  # repli local réussi
    assert "s'est arrêté brutalement" in caplog.text
    assert T._pool is None  # pool cassé abandonné…

    monkeypatch.undo()
    assert T.transcode(uncompressed, mode=T.MODE_LOSSY, ratio=10) is not None
    assert T._pool is not None  # … et recréé à l'encodage suivant


def test_pool_is_disabled_after_repeated_failures(uncompressed, j2kWorkers, monkeypatch,
                                                   caplog):
    _requireJ2k()
    j2kWorkers(2)
    monkeypatch.setattr(T.dmf_j2k_worker, "encode", _crashingEncode)
    with caplog.at_level("ERROR", logger="girder.dicom_measure_flow"):
        for _ in range(T.POOL_MAX_FAILURES + 2):
            out = T.transcode(uncompressed, mode=T.MODE_LOSSY, ratio=10)
            assert out is not None and out.transferSyntax == str(JPEG2000)
    # Exactement POOL_MAX_FAILURES pools tentés, puis plus aucun : encodage local.
    assert caplog.text.count("s'est arrêté brutalement") == T.POOL_MAX_FAILURES
    assert "pool DÉSACTIVÉ" in caplog.text
    assert T._getPool() is None

    monkeypatch.undo()
    j2kWorkers(3)  # un changement de réglage réarme le pool
    assert T.transcode(uncompressed, mode=T.MODE_LOSSY, ratio=10) is not None
    assert T._pool is not None


def test_configure_j2k_workers_is_tolerant(j2kWorkers):
    j2kWorkers("pas un nombre")
    assert T._j2kWorkers == T.defaultJ2kWorkers()
    j2kWorkers(-3)
    assert T._j2kWorkers == 0
    assert 1 <= T.defaultJ2kWorkers() <= 4


@pytest.mark.parametrize("workers", [0, 3])
def test_real_parallel_j2k_encoding_does_not_crash(workers):
    """Encodages J2K RÉELS depuis 8 threads, dans un sous-processus : sans protection, le
    segfault tuerait pytest lui-même au lieu de faire échouer ce test. Couvre le repli
    sérialisé (0 worker) ET le pool de processus."""
    import subprocess
    import sys
    import textwrap

    _requireJ2k()
    script = textwrap.dedent(
        """
        import sys, threading
        workers = int(sys.argv[1])
        sys.path[:0] = sys.argv[2:]
        import test_transcode as TT
        import transcode as T
        T.configureJ2kWorkers(workers)
        data = TT._bytes(TT._dataset(rows=256, columns=256))
        errors = []
        def run():
            try:
                for _ in range(20):
                    out = T.transcode(data, mode=T.MODE_LOSSY, ratio=10)
                    assert out is not None and out.transferSyntax == str(T.JPEG2000), out
            except BaseException as exc:
                errors.append(repr(exc))
        pool = [threading.Thread(target=run) for _ in range(8)]
        [t.start() for t in pool]
        [t.join() for t in pool]
        assert (T._pool is not None) == (workers > 0)
        print(errors[:1])
        sys.exit(1 if errors else 0)
        """
    )
    here = os.path.dirname(__file__)
    root = os.path.join(here, "..")
    pkg = os.path.join(root, "girder_dicom_measure_flow")
    proc = subprocess.run(
        [sys.executable, "-c", script, str(workers), here, root, pkg],
        capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, (proc.returncode, proc.stdout[-500:], proc.stderr[-2000:])
