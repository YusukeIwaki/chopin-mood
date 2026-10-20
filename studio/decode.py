"""Source decode preserving fidelity (no unconditional pcm_s16le).

WAV input keeps its native subtype/channels via soundfile; compressed
input (mp3/aac/ogg/video audio) decodes to 32-bit float WAV. Both the
raw attachment hash and the decoded WAV hash are recorded; downstream
steps compare both against the plan context before rendering.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path


def sha256_file(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def probe(path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format",
         "-of", "json", str(path)], capture_output=True, text=True,
        timeout=120, check=True).stdout
    return json.loads(out)


def decode_lossless(src, dst) -> dict:
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
        out_info = sf.info(str(dst))
        return {"mode": mode, "subtype": out_info.subtype,
                "channels": out_info.channels, "sample_rate": out_info.samplerate,
                "source_subtype": info.subtype, "source_channels": info.channels}
    # Compressed media: high-precision float decode via ffmpeg.
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(src),
                    "-map", "0:a:0", "-c:a", "pcm_f32le", str(dst)],
                   check=True, timeout=600)
    out_info = sf.info(str(dst))
    return {"mode": "compressed_pcm_f32le", "subtype": out_info.subtype,
            "channels": out_info.channels, "sample_rate": out_info.samplerate,
            "source_subtype": info.subtype if info else "compressed",
            "source_channels": info.channels if info else out_info.channels}
