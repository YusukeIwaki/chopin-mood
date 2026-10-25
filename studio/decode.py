"""Source decode preserving fidelity (no unconditional pcm_s16le).

WAV input keeps its native subtype/channels via soundfile; compressed
input (mp3/aac/ogg/video audio) decodes to 32-bit float WAV. Both the
raw attachment hash and the decoded WAV hash are recorded; downstream
steps compare both against the plan context before rendering.
"""
from __future__ import annotations

import hashlib
import json
import struct
import subprocess
import tempfile
import os
from pathlib import Path


def sha256_file(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def probe(path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format",
         "-of", "json", str(path)], capture_output=True, text=True,
        timeout=120, check=True).stdout
    return json.loads(out)


def canonicalize_wav(path) -> None:
    """Strip volatile WAV metadata without touching a single sample byte.

    The v2 contract uses a minimal RIFF fmt/fact/data container. Decoder
    versions can still produce different PCM; the resulting hash mismatch is
    intentional and must be resolved by using the same decoder, never waived.
    """
    path = Path(path)
    with path.open("rb") as source:
        header = source.read(12)
        if len(header) != 12 or header[:4] != b"RIFF" or header[8:] != b"WAVE":
            raise ValueError("canonical decode requires little-endian RIFF WAV")
        fmt, data_offset, data_size = None, None, None
        while chunk := source.read(8):
            if len(chunk) != 8:
                raise ValueError("truncated WAV chunk header")
            kind, size = struct.unpack("<4sI", chunk)
            if kind == b"fmt ":
                if size > 128:
                    raise ValueError("unsupported WAV fmt chunk")
                fmt = source.read(size)
                if len(fmt) != size:
                    raise ValueError("truncated WAV fmt chunk")
            elif kind == b"data":
                if data_offset is not None:
                    raise ValueError("multiple WAV data chunks")
                data_offset, data_size = source.tell(), size
                source.seek(size, 1)
            else:
                source.seek(size, 1)
            if size % 2:
                source.seek(1, 1)
        if fmt is None or len(fmt) < 16 or data_offset is None:
            raise ValueError("missing WAV audio chunks")
        tag, channels, sr, rate, alignment, bits = struct.unpack("<HHIIHH", fmt[:16])
        if tag == 65534 and len(fmt) >= 40:
            tag = struct.unpack("<H", fmt[24:26])[0]
        if (tag not in (1, 3) or bits not in (8, 16, 24, 32, 64)
                or not alignment or alignment != channels * bits // 8
                or rate != sr * alignment or data_size % alignment):
            raise ValueError("unsupported canonical WAV PCM format")
        if data_offset + data_size > path.stat().st_size:
            raise ValueError("truncated WAV samples")
        canonical_fmt = struct.pack("<HHIIHH", tag, channels, sr, rate, alignment, bits)
        chunks = b"fmt " + struct.pack("<I", 16) + canonical_fmt
        if tag == 3:
            chunks += b"fact" + struct.pack("<II", 4, data_size // alignment)
        riff_size = 4 + len(chunks) + 8 + data_size + data_size % 2
        fd, temporary = tempfile.mkstemp(prefix=".canonical-", suffix=".wav", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(b"RIFF" + struct.pack("<I", riff_size) + b"WAVE" + chunks)
                output.write(b"data" + struct.pack("<I", data_size))
                source.seek(data_offset)
                remaining = data_size
                while remaining:
                    chunk = source.read(min(remaining, 1024 * 1024))
                    if not chunk:
                        raise ValueError("truncated WAV samples")
                    output.write(chunk)
                    remaining -= len(chunk)
                if data_size % 2:
                    output.write(b"\x00")
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def decode_lossless(src, dst, *, canonical_wav=False) -> dict:
    """Decode to WAV without truncating bit depth. Returns meta."""
    import soundfile as sf
    src, dst = Path(src), Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        info = sf.info(str(src))
    except sf.LibsndfileError:
        # libsndfile does not support AAC/M4A/MP4; ffmpeg is the decoder
        # for those containers, not just an alternative for MP3.
        info = None
    if info is not None and info.format == "WAV":
        data, sr = sf.read(str(src), dtype="float64", always_2d=True)
        # Re-write at native subtype: read raw per subtype to stay exact
        # for integer PCM; float64 round-trip is exact for PCM_24/32 too
        # when written back at the same subtype via int32 path.
        if info.subtype in ("PCM_16",):
            raw, _ = sf.read(str(src), dtype="int16", always_2d=True)
            sf.write(str(dst), raw, sr, subtype="PCM_16")
        elif info.subtype in ("PCM_24", "PCM_32"):
            raw, _ = sf.read(str(src), dtype="int32", always_2d=True)
            sf.write(str(dst), raw, sr, subtype=info.subtype)
        else:
            sf.write(str(dst), data, sr, subtype=info.subtype)
        mode = f"wav_native_{info.subtype.lower()}"
        if canonical_wav:
            canonicalize_wav(dst)
        out_info = sf.info(str(dst))
        return {"mode": mode, "subtype": out_info.subtype,
                "channels": out_info.channels, "sample_rate": out_info.samplerate,
                "source_subtype": info.subtype, "source_channels": info.channels}
    # Compressed media: high-precision float decode via ffmpeg.
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(src),
                    "-map", "0:a:0", "-c:a", "pcm_f32le", str(dst)],
                   check=True, timeout=600)
    if canonical_wav:
        canonicalize_wav(dst)
    out_info = sf.info(str(dst))
    return {"mode": "compressed_pcm_f32le", "subtype": out_info.subtype,
            "channels": out_info.channels, "sample_rate": out_info.samplerate,
            "source_subtype": info.subtype if info else "compressed",
            "source_channels": info.channels if info else out_info.channels}
