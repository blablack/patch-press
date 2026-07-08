import numpy as np

from ..model.audio import AudioBuffer

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def midi_to_hz(note: int) -> float:
    return 440.0 * (2.0 ** ((note - 69) / 12.0))


def hz_to_midi(freq: float) -> float:
    return 69 + 12 * np.log2(freq / 440.0)


def detect_pitch(
    buf: AudioBuffer,
    min_hz: float = 20.0,
    max_hz: float = 4000.0,
) -> float | None:
    """
    Autocorrelation-based fundamental frequency detection.
    Uses a 1-second window from the first quarter of the signal to stay in the sustain.
    Returns Hz or None if the signal is too short or flat.
    """
    mono = buf.data.mean(axis=0)
    sr = buf.sample_rate

    start = max(0, len(mono) // 8)
    end = min(len(mono), start + sr)
    if end - start < sr // 4:
        return None

    segment = mono[start:end]
    if segment.std() < 1e-4:  # near-silent — no reliable pitch
        return None
    n = len(segment)
    windowed = segment * np.hanning(n)
    fft = np.fft.rfft(windowed, n=2 * n)
    autocorr = np.fft.irfft(np.abs(fft) ** 2)[:n]

    min_lag = max(1, int(sr / max_hz))
    max_lag = min(n // 2, int(sr / min_hz))
    if min_lag >= max_lag:
        return None

    peak_lag = min_lag + int(np.argmax(autocorr[min_lag:max_lag]))
    # Reject weak peaks. autocorr[0] is the signal energy; requiring the peak's
    # relative height to clear a threshold avoids returning a "confident" Hz
    # value for noisy near-silent signals that just cleared the std gate above.
    # Matches the pattern used in loop.py's period detectors
    # (_detect_waveform_period gates on `search.max() < 0.3`).
    if autocorr[peak_lag] / (autocorr[0] + 1e-12) < 0.3:
        return None
    return float(sr / peak_lag) if peak_lag > 0 else None


def verify_pitch(
    buf: AudioBuffer,
    expected_note: int,
    tolerance_cents: float = 50.0,
) -> dict:
    detected_hz = detect_pitch(buf)
    expected_hz = midi_to_hz(expected_note)

    if detected_hz is None or detected_hz <= 0:
        return {"pitch_ok": False, "reason": "detection_failed"}

    cents_diff = 1200 * np.log2(detected_hz / expected_hz)
    # Fold to [-600, 600] to ignore octave ambiguity in the autocorrelation detector.
    # A sample at C4 detected as C3 gives 0 cents error, not -1200.
    cents_class = ((cents_diff + 600) % 1200) - 600
    ok = bool(abs(cents_class) <= tolerance_cents)
    return {
        "pitch_ok": ok,
        "detected_hz": round(detected_hz, 2),
        "expected_hz": round(expected_hz, 2),
        "detected_midi": round(hz_to_midi(detected_hz), 2),
        "cents_diff": round(float(cents_class), 1),
    }
