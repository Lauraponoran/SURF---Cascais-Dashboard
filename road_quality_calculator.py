"""

Road Quality Calculator

Separate module for calculating road quality based on acceleration data.

"""



import numpy as np

from typing import Dict, List





class RoadQualityCalculator:

    """Calculator for road quality assessment based on acceleration data."""

    

    def __init__(self):

        """Initialize the road quality calculator."""

        pass

    

    def calculate_road_quality(self, acc_y_data: np.ndarray, window_size: int = 100, overlap: float = 0.5) -> Dict[str, np.ndarray]:

        """

        Calculate road quality based on acceleration data.

        Returns road quality scores from 1 (perfect) to 5 (terrible).

        

        Uses absolute percentile-based thresholds:

        1 = Top 5% (best roads)

        2 = Normal conditions (5-30%)

        3 = Worse 30-55%

        4 = Bad 55-75%

        5 = Worst 25%

        """

        n_samples = len(acc_y_data)

        step_size = int(window_size * (1 - overlap))

        

        road_quality_scores = []

        time_windows = []

        

        for i in range(0, n_samples - window_size, step_size):

            window_data = acc_y_data[i:i + window_size]

            time_windows.append(i + window_size // 2)  # Center of window

            

            # Method 1: RMS (Root Mean Square) - better indicator of overall vibration

            rms_score = np.sqrt(np.mean(window_data**2))

            

            # Method 2: Smoothness Index (inverse of roughness)

            smoothness_score = 1.0 / (1.0 + np.std(window_data))

            

            # Method 3: Peak-to-Peak analysis (detect sudden impacts)

            peak_to_peak = np.max(window_data) - np.min(window_data)

            

            # Method 4: Frequency content analysis (using FFT)

            fft_data = np.fft.fft(window_data)

            power_spectrum = np.abs(fft_data)**2

            high_freq_power = np.sum(power_spectrum[len(power_spectrum)//4:])

            freq_score = high_freq_power / len(window_data)

            

            # Method 5: Jerk analysis (rate of change) - comfort indicator

            jerk = np.diff(window_data)

            jerk_rms = np.sqrt(np.mean(jerk**2))

            

            # Weighted combination

            weights = {

                'rms': 0.35,

                'smoothness': 0.25,

                'peak_to_peak': 0.20,

                'frequency': 0.10,

                'jerk': 0.10

            }

            

            # Calculate individual normalized scores

            normalized_rms = min(rms_score / 4.0, 1.0)

            normalized_smoothness = 1.0 - smoothness_score

            normalized_peak_to_peak = min(peak_to_peak / 15.0, 1.0)

            normalized_freq = min(freq_score / 15000.0, 1.0)

            normalized_jerk = min(jerk_rms / 4.0, 1.0)

            

            combined_score = (

                weights['rms'] * normalized_rms +

                weights['smoothness'] * normalized_smoothness +

                weights['peak_to_peak'] * normalized_peak_to_peak +

                weights['frequency'] * normalized_freq +

                weights['jerk'] * normalized_jerk

            )

            

            # Convert to 1-5 scale using adjusted percentile-based thresholds

            # Balanced threshold for score 3 - middle ground between too high and too low

            if combined_score < 0.05:

                road_quality = 1  # Top 5% - Perfect race track

            elif combined_score < 0.30:

                road_quality = 2  # 5-30% - Normal conditions

            elif combined_score < 0.55:

                road_quality = 3  # 30-55% - Worse conditions (balanced threshold)

            elif combined_score < 0.75:

                road_quality = 4  # 55-75% - Bad conditions

            else:

                road_quality = 5  # Top 25% worst - Off-road/extreme

            

            road_quality_scores.append(road_quality)

        

        return {

            'road_quality': np.array(road_quality_scores),

            'time_windows': np.array(time_windows),

            'window_size': window_size

        }

    

    def _detect_peaks(self, data: np.ndarray, threshold: float = 0.5) -> List[int]:

        """Detect peaks in acceleration data."""

        peaks = []

        for i in range(1, len(data) - 1):

            if (data[i] > data[i-1] and data[i] > data[i+1] and 

                abs(data[i]) > threshold):

                peaks.append(i)

        return peaks





# Convenience function for direct usage

def calculate_road_quality(acc_y_data: np.ndarray, window_size: int = 100, overlap: float = 0.5) -> Dict[str, np.ndarray]:

    """

    Convenience function to calculate road quality.

    

    Args:

        acc_y_data: Y-axis acceleration data as numpy array

        window_size: Size of analysis window (default: 100 samples)

        overlap: Overlap between windows (default: 0.5 = 50%)

    

    Returns:

        Dictionary containing:

        - 'road_quality': Array of road quality scores (1-5)

        - 'time_windows': Array of time window centers

        - 'window_size': Window size used

    """

    calculator = RoadQualityCalculator()

    return calculator.calculate_road_quality(acc_y_data, window_size, overlap)