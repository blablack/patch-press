import numpy as np

from ..model.audio import AudioBuffer


def classify_drum(buf: AudioBuffer) -> str:
    """
    Classifies a drum hit as kick/snare/hihat/other using spectral energy in the
    first 100 ms. Intentionally simple — good enough for rough kit labelling.
    """
    mono = buf.data.mean(axis=0)
    sr = buf.sample_rate

    n_frames = min(len(mono), sr // 10)
    segment = mono[:n_frames]
    if len(segment) < 64:
        return "other"

    spectrum = np.abs(np.fft.rfft(segment))
    freqs = np.fft.rfftfreq(len(segment), d=1.0 / sr)
    total = spectrum.sum() or 1.0

    def band(lo: float, hi: float) -> float:
        return float(spectrum[(freqs >= lo) & (freqs < hi)].sum()) / total

    low = band(20, 200)
    mid = band(200, 2000)
    high = band(2000, 20000)

    if low > 0.5:
        return "kick"
    if mid > 0.4 and low < 0.3:
        return "snare"
    if high > 0.5:
        return "hihat"
    return "other"
