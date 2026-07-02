import numpy as np

# Hardware constants
CENTER_FREQ_HZ = 143_300_000   # 143.3 MHz — PLL-friendly offset
TARGET_FREQ_HZ = 143_050_000   # 143.050 MHz — GRAVES carrier
SAMPLE_RATE = 1_024_000        # 1.024 MSPS
GAIN_DB = 40.2                 # RTL-SDR Blog V3 gain setting
PPM_CORRECTION = 0             # Set to measured crystal offset; 0 uses TCXO reference if available
N_SAMPLES = 102_400            # 100 ms accumulation window (100 ms × 1.024 MSPS)
N_BINS = 40                    # ±200 Hz around target at 10 Hz/bin

# Bin index of TARGET_FREQ_HZ in the FFT output (before fftshift)
# bin_width = SAMPLE_RATE / N_SAMPLES = 10 Hz/bin
# offset = TARGET_FREQ_HZ - CENTER_FREQ_HZ = -250000 Hz → -25000 bins from DC
# wrap negative index: N_SAMPLES + (-25000) = 77400
_BIN_WIDTH_HZ = SAMPLE_RATE / N_SAMPLES          # 10.0 Hz
_TARGET_OFFSET_HZ = TARGET_FREQ_HZ - CENTER_FREQ_HZ  # -250000 Hz
_TARGET_BIN_RAW = int(round(_TARGET_OFFSET_HZ / _BIN_WIDTH_HZ))  # -25000
BIN_OFFSET = (_TARGET_BIN_RAW % N_SAMPLES)       # 77400 (Python % always non-negative)

# Slice: N_BINS//2 bins below target, N_BINS//2 bins above (centre at BIN_OFFSET)
_HALF = N_BINS // 2  # 20
_BIN_START = BIN_OFFSET - _HALF   # 77380
_BIN_END = _BIN_START + N_BINS    # 77420


class AcquisitionError(RuntimeError):
    pass


class Acquisition:
    def __init__(self):
        self._sdr = None

    def open_device(self):
        try:
            from rtlsdr import RtlSdr
            sdr = RtlSdr()
            sdr.center_freq = CENTER_FREQ_HZ
            sdr.sample_rate = SAMPLE_RATE
            sdr.gain = GAIN_DB
            sdr.freq_correction = PPM_CORRECTION
            self._sdr = sdr
        except Exception as exc:
            raise AcquisitionError(f"Failed to open RTL-SDR: {exc}") from exc

    def read_row(self) -> np.ndarray:
        """Read one 100 ms IQ block and return 40-bin float32 power row in dBFS."""
        try:
            samples = self._sdr.read_samples(N_SAMPLES)
        except Exception as exc:
            raise AcquisitionError(f"read_samples failed: {exc}") from exc

        samples = np.asarray(samples, dtype=np.complex64)
        # Apply Hann window to reduce sidelobe leakage from −13 dB (rectangular) to −31 dB
        window_func = np.hanning(N_SAMPLES)
        window_power = np.sum(window_func ** 2)
        spectrum = np.fft.fft(samples * window_func, n=N_SAMPLES)

        # Extract the 40 bins around TARGET_FREQ_HZ (wrapping handled by modular slice)
        if _BIN_END <= N_SAMPLES:
            window = spectrum[_BIN_START:_BIN_END]
        else:
            # Wrap around (shouldn't happen with current constants, but be safe)
            window = np.concatenate([spectrum[_BIN_START:], spectrum[:_BIN_END - N_SAMPLES]])

        power = (np.abs(window) ** 2) / window_power
        power_db = 10.0 * np.log10(power + 1e-30)
        return power_db.astype(np.float32)

    def close(self):
        if self._sdr is not None:
            self._sdr.close()
            self._sdr = None


if __name__ == "__main__":
    acq = Acquisition()
    print(f"Opening RTL-SDR at {CENTER_FREQ_HZ / 1e6:.3f} MHz, gain {GAIN_DB} dB …")
    acq.open_device()
    try:
        for i in range(10):
            row = acq.read_row()
            vals = " ".join(f"{v:7.2f}" for v in row)
            print(f"row {i:02d}: [{vals}]")
    finally:
        acq.close()
