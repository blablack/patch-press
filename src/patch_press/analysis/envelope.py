import numpy as np

from ..model.audio import AudioBuffer


def _to_db(v: float) -> float:
    return 20 * np.log10(v) if v > 0 else -float("inf")


def analyze_envelope(buf: AudioBuffer) -> dict:
    mono = np.abs(buf.data.mean(axis=0))
    sr = buf.sample_rate
    n = len(mono)

    if n == 0:
        return {"peak_db": -float("inf"), "rms_db": -float("inf"), "attack_s": None}

    peak = float(mono.max())
    rms = float(np.sqrt(np.mean(mono**2)))
    attack_idx = int(np.argmax(mono >= 0.9 * peak))

    return {
        "peak_db": round(_to_db(peak), 2),
        "rms_db": round(_to_db(rms), 2),
        "attack_s": round(attack_idx / sr, 4),
    }
