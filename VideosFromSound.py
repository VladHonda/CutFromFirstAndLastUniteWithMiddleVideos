"""
Hunt: Showdown Highlight Extractor (Work In Progress)
----------------------------------------------------
Automatically detects intense audio/action segments in Twitch VODs
and downloads them as highlight clips.

⚠️ Work in progress — not fully functional yet.
Use at your own risk.

Dependencies:
    pip install yt-dlp moviepy==1.0.3 librosa numpy<2 tqdm
"""

import os
import subprocess
import numpy as np
import librosa
from moviepy.editor import VideoFileClip, concatenate_videoclips
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm

# === CONFIGURATION ===
CHANNEL_URL = os.getenv("TWITCH_CHANNEL_URL", "https://www.twitch.tv/exampleChannel")
OUTPUT_FOLDER = os.getenv("OUTPUT_FOLDER", "output")
TEMP_FOLDER = os.getenv("TEMP_FOLDER", "temp")
COMBINED_OUTPUT = os.getenv("COMBINED_OUTPUT", os.path.join(OUTPUT_FOLDER, "combined_highlights.mp4"))
FFMPEG_PATH = os.getenv("FFMPEG_PATH", "ffmpeg")  # assume ffmpeg is in PATH

AUDIO_FPS = 48000
THRESHOLD_PERCENTILE = 99.9
PRE_SECONDS = 5
POST_SECONDS = 5
MIN_GAP_BETWEEN_EVENTS = 10
SEGMENT_DURATION = 300  # seconds
MAX_WORKERS = 12  # Optimized for 6C/12T CPUs

os.makedirs(OUTPUT_FOLDER, exist_ok=True)
os.makedirs(TEMP_FOLDER, exist_ok=True)


# === UTILITIES ===
def log(msg, icon="ℹ️"):
    print(f"{icon} {msg}")


def generate_unique_filename(base_name, extension=".mp4"):
    from datetime import datetime
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{base_name}_{timestamp}{extension}"


def save_video(clip, path):
    """Safely save video using NVENC or fallback to libx264."""
    try:
        clip.write_videofile(path, codec="h264_nvenc", audio_codec="aac", threads=4, logger=None)
    except Exception:
        log("NVENC not available, using libx264.", "⚠️")
        clip.write_videofile(path, codec="libx264", audio_codec="aac", threads=4, logger=None)


# === AUDIO DETECTION ===
def detect_loud_sections(local_file):
    """Detect intense action periods (e.g., gunshots) in a video file."""
    log(f"Analyzing audio: {local_file}", "🎧")
    try:
        y, sr = librosa.load(local_file, sr=AUDIO_FPS)
    except Exception as e:
        log(f"Error loading audio from {local_file}: {e}", "❌")
        return []

    # Onset detection
    onset_env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=1024)
    onset_frames = librosa.onset.onset_detect(onset_envelope=onset_env, sr=sr, units="frames")
    onset_times_all = librosa.frames_to_time(onset_frames, sr=sr)

    # Filter onsets by strength and frequency (detect sharp high-frequency bursts)
    strength_threshold = np.percentile(onset_env, 95)
    spectral_centroid = librosa.feature.spectral_centroid(y=y, sr=sr, hop_length=1024)[0]
    centroid_threshold = 4000  # Hz
    filtered_onsets = [
        t for t, f in zip(onset_times_all, onset_frames)
        if onset_env[f] > strength_threshold and spectral_centroid[f] > centroid_threshold
    ]

    # Detect clusters of onsets (action peaks)
    window_size = 15.0
    step_size = 5.0
    min_onsets = 6
    events = []
    max_time = filtered_onsets[-1] if filtered_onsets else 0

    for window_start in np.arange(0, max_time - window_size + step_size, step_size):
        window_end = window_start + window_size
        onsets_in_window = [t for t in filtered_onsets if window_start <= t < window_end]
        if len(onsets_in_window) >= min_onsets:
            center_time = (window_start + window_end) / 2
            events.append(center_time)

    # Filter events by minimum time gap
    filtered_events = []
    for t in sorted(events):
        if not filtered_events or t - filtered_events[-1] > MIN_GAP_BETWEEN_EVENTS:
            filtered_events.append(t)

    segments = [(max(0, t - PRE_SECONDS), t + POST_SECONDS) for t in filtered_events]
    log(f"Detected {len(segments)} action periods.", "🔊")
    return segments


# === DOWNLOAD SEGMENTS ===
def download_segment(vod_url, start, end, output_path):
    """Download a specific time segment from a Twitch VOD using yt-dlp."""
    cmd = [
        "yt-dlp",
        "--ffmpeg-location", FFMPEG_PATH,
        "-f", "best",
        "--download-sections", f"*{start}-{end}",
        vod_url,
        "-o", output_path
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    log(f"Segment downloaded: {output_path}", "✅")
    return output_path


# === PROCESS VOD ===
def process_vod(vod_url, vod_index):
    log(f"Processing VOD: {vod_url}", "🎬")

    # Get VOD duration
    result = subprocess.run(
        ["yt-dlp", "--ffmpeg-location", FFMPEG_PATH, "--get-duration", vod_url],
        capture_output=True, text=True
    )
    duration_str = result.stdout.strip()
    parts = list(map(int, duration_str.split(":")))
    if len(parts) == 3:
        h, m, s = parts
    elif len(parts) == 2:
        h, m, s = 0, parts[0], parts[1]
    else:
        h, m, s = 0, 0, parts[0]
    vod_duration = h * 3600 + m * 60 + s

    # Split into chunks
    vod_segments = [(i, min(i + SEGMENT_DURATION, vod_duration)) for i in range(0, vod_duration, SEGMENT_DURATION)]
    highlight_paths = []

    def process_segment_wrapper(start, end, seg_idx):
        temp_file = os.path.join(TEMP_FOLDER, f"vod{vod_index}_seg{seg_idx}.mp4")
        if not os.path.exists(temp_file):
            download_segment(vod_url, start, end, temp_file)

        try:
            events = detect_loud_sections(temp_file)
        except Exception as e:
            log(f"Skipping segment {seg_idx} due to error: {e}", "⚠️")
            return []

        segment_clips = []
        for i, (s, e) in enumerate(events):
            highlight_file = os.path.join(OUTPUT_FOLDER, f"vod{vod_index}_seg{seg_idx}_{i}.mp4")
            if not os.path.exists(highlight_file):
                try:
                    download_segment(vod_url, int(start + s), int(start + e), highlight_file)
                    segment_clips.append(highlight_file)
                except Exception as e:
                    log(f"Error downloading highlight ({s}-{e}): {e}", "❌")
        return segment_clips

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(process_segment_wrapper, s, e, idx): idx
            for idx, (s, e) in enumerate(vod_segments)
        }
        for f in tqdm(as_completed(futures), total=len(futures), desc="Processing segments"):
            try:
                highlight_paths.extend(f.result())
            except Exception as e:
                idx = futures[f]
                log(f"Error in segment {idx}: {e}", "❌")

    return highlight_paths


# === MAIN ===
def main():
    log("Starting Hunt: Showdown highlight extractor...", "🎬")

    vod_urls = [
        "https://www.twitch.tv/videos/000000000",  # Example placeholder VOD
    ]

    all_highlights = []

    for idx, vod_url in enumerate(vod_urls):
        try:
            highlights = process_vod(vod_url, idx)
            all_highlights.extend(highlights)
        except Exception as e:
            log(f"Error processing VOD {vod_url}: {e}", "❌")

    if all_highlights:
        log("Combining all highlights...", "🔗")
        clips = [VideoFileClip(p) for p in all_highlights]
        combined = concatenate_videoclips(clips, method="compose")
        save_video(combined, COMBINED_OUTPUT)
        for c in clips:
            c.close()
        log(f"✅ Done! Combined highlights saved to:\n{COMBINED_OUTPUT}", "🏁")
    else:
        log("No highlights were generated.", "⚠️")


if __name__ == "__main__":
    main()
