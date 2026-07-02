import sys
import types
import unittest
from unittest.mock import MagicMock, patch, PropertyMock
import numpy as np

# Stub out rtlsdr before importing acquisition
rtlsdr_mod = types.ModuleType("rtlsdr")
RtlSdrMock = MagicMock()
rtlsdr_mod.RtlSdr = RtlSdrMock
sys.modules["rtlsdr"] = rtlsdr_mod

from src.acquisition import Acquisition, AcquisitionError, N_SAMPLES, CENTER_FREQ_HZ, TARGET_FREQ_HZ, SAMPLE_RATE, GAIN_DB, N_BINS, BIN_OFFSET, PPM_CORRECTION

CENTER_HZ = CENTER_FREQ_HZ  # 143.3e6
TARGET_HZ = TARGET_FREQ_HZ  # 143.05e6


def _pure_tone_iq(freq_offset_hz: float, n: int = N_SAMPLES) -> np.ndarray:
    """Generate complex IQ samples for a pure tone at freq_offset_hz from center."""
    t = np.arange(n) / SAMPLE_RATE
    return np.exp(2j * np.pi * freq_offset_hz * t).astype(np.complex64)


class TestAcquisitionInit(unittest.TestCase):
    def setUp(self):
        RtlSdrMock.reset_mock()
        RtlSdrMock.return_value = MagicMock()

    def test_open_device_sets_center_freq(self):
        acq = Acquisition()
        acq.open_device()
        sdr = RtlSdrMock.return_value
        self.assertEqual(sdr.center_freq, CENTER_FREQ_HZ)

    def test_open_device_sets_sample_rate(self):
        acq = Acquisition()
        acq.open_device()
        sdr = RtlSdrMock.return_value
        self.assertEqual(sdr.sample_rate, SAMPLE_RATE)

    def test_open_device_sets_gain(self):
        acq = Acquisition()
        acq.open_device()
        sdr = RtlSdrMock.return_value
        self.assertEqual(sdr.gain, GAIN_DB)

    def test_open_device_sets_ppm_correction(self):
        acq = Acquisition()
        acq.open_device()
        sdr = RtlSdrMock.return_value
        self.assertEqual(sdr.freq_correction, PPM_CORRECTION)

    def test_close_calls_close(self):
        acq = Acquisition()
        acq.open_device()
        acq.close()
        RtlSdrMock.return_value.close.assert_called_once()


class TestFFTExtraction(unittest.TestCase):
    def setUp(self):
        RtlSdrMock.reset_mock()
        self.sdr_instance = MagicMock()
        RtlSdrMock.return_value = self.sdr_instance

    def test_output_shape(self):
        target_offset = TARGET_HZ - CENTER_HZ  # -250000 Hz
        iq = _pure_tone_iq(target_offset)
        self.sdr_instance.read_samples.return_value = iq

        acq = Acquisition()
        acq.open_device()
        row = acq.read_row()

        self.assertEqual(row.shape, (N_BINS,))

    def test_output_dtype_float32(self):
        target_offset = TARGET_HZ - CENTER_HZ
        iq = _pure_tone_iq(target_offset)
        self.sdr_instance.read_samples.return_value = iq

        acq = Acquisition()
        acq.open_device()
        row = acq.read_row()

        self.assertEqual(row.dtype, np.float32)

    def test_peak_at_center_bin_for_target_tone(self):
        """Pure tone at 143.050 MHz should peak at center of the 40-bin window."""
        target_offset = TARGET_HZ - CENTER_HZ  # -250000 Hz
        iq = _pure_tone_iq(target_offset)
        self.sdr_instance.read_samples.return_value = iq

        acq = Acquisition()
        acq.open_device()
        row = acq.read_row()

        peak_bin = int(np.argmax(row))
        # Center bin of a 40-bin window is bin 20 (0-indexed)
        self.assertEqual(peak_bin, N_BINS // 2)

    def test_values_are_finite_and_in_range(self):
        """Hann-windowed FFT output should be finite floats in a plausible power range.

        With PSD normalisation (/ window_power) a unit-amplitude pure tone peaks at
        ~48 dBFS — higher than amplitude normalisation but still bounded.  The test
        verifies finiteness and a generous upper bound (200 dB) rather than a strict
        0-dBFS ceiling that only holds for amplitude normalisation.
        """
        target_offset = TARGET_HZ - CENTER_HZ
        iq = _pure_tone_iq(target_offset)
        self.sdr_instance.read_samples.return_value = iq

        acq = Acquisition()
        acq.open_device()
        row = acq.read_row()

        # All values must be finite (no NaN or Inf)
        self.assertTrue(np.all(np.isfinite(row)))
        # Sanity bounds — the PSD-normalised peak for a unit tone is ≈48 dBFS
        self.assertGreater(float(np.max(row)), -300.0)
        self.assertLess(float(np.max(row)), 200.0)

    def test_read_samples_called_with_correct_count(self):
        iq = np.zeros(N_SAMPLES, dtype=np.complex64)
        self.sdr_instance.read_samples.return_value = iq

        acq = Acquisition()
        acq.open_device()
        acq.read_row()

        self.sdr_instance.read_samples.assert_called_once_with(N_SAMPLES)


class TestErrorHandling(unittest.TestCase):
    def setUp(self):
        RtlSdrMock.reset_mock()
        self.sdr_instance = MagicMock()
        RtlSdrMock.return_value = self.sdr_instance

    def test_read_samples_exception_raises_acquisition_error(self):
        self.sdr_instance.read_samples.side_effect = Exception("libusb error")

        acq = Acquisition()
        acq.open_device()

        with self.assertRaises(AcquisitionError):
            acq.read_row()

    def test_open_device_exception_raises_acquisition_error(self):
        RtlSdrMock.side_effect = Exception("USB device not found")

        acq = Acquisition()
        with self.assertRaises(AcquisitionError):
            acq.open_device()
        RtlSdrMock.side_effect = None


class TestConstants(unittest.TestCase):
    def test_bin_offset_correct(self):
        """BIN_OFFSET should be index of 143.050 MHz in the FFT output."""
        expected = int(round((TARGET_HZ - CENTER_HZ) / (SAMPLE_RATE / N_SAMPLES)))
        # Wrap negative offset to positive index
        if expected < 0:
            expected = N_SAMPLES + expected
        self.assertEqual(BIN_OFFSET, expected)

    def test_n_bins_is_40(self):
        self.assertEqual(N_BINS, 40)


if __name__ == "__main__":
    unittest.main()
