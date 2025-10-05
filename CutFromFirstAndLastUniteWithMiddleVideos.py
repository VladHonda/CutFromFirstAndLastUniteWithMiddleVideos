import os
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm

# === Paths ===
# Poți seta TEMP_FOLDER ca variabilă de mediu pentru a specifica alt folder de intrare.
# Ex: set TEMP_FOLDER=D:/Videos/Hunt/TempSegments/KatanaFight
input_folder = os.getenv("TEMP_FOLDER", "temp")
output_folder = os.path.join(input_folder, "CombinedSegments")
os.makedirs(output_folder, exist_ok=True)

# === FFmpeg path ===
# Folosește variabila de mediu FFMPEG_PATH dacă e setată, altfel presupune că ffmpeg e în PATH
ffmpeg_path = os.getenv("FFMPEG_PATH", "ffmpeg")

# === Get sorted video list ===
videos = sorted([
    f for f in os.listdir(input_folder)
    if f.lower().endswith(('.mp4', '.mov', '.mkv', '.avi'))
])
if not videos:
    raise ValueError("No video files found in the input folder.")

print(f"📂 Found {len(videos)} video(s):")
for v in videos:
    print(f"   • {v}")


def fast_trim(video_path, output_path, start=None, end=None):
    """Use ffmpeg to trim or copy video without re-encoding (fast)."""
    cmd = [ffmpeg_path, "-y", "-hide_banner", "-loglevel", "error", "-i", video_path]
    if start is not None:
        cmd += ["-ss", str(start)]
    if end is not None:
        cmd += ["-to", str(end)]
    cmd += ["-c", "copy", output_path]
    subprocess.run(cmd, check=True)
    return output_path


# === Prepare paths ===
first_video = os.path.join(input_folder, videos[0])
last_video = os.path.join(input_folder, videos[-1])
trimmed_first_path = os.path.join(output_folder, f"trimmed_first_{videos[0]}")
trimmed_last_path = os.path.join(output_folder, f"trimmed_last_{videos[-1]}")

# === Run trimming concurrently ===
print("\n✂️  Fast trimming first and last videos...")
with ThreadPoolExecutor(max_workers=2) as executor:
    futures = {
        executor.submit(fast_trim, first_video, trimmed_first_path, start="00:03:12"): "First",
        executor.submit(fast_trim, last_video, trimmed_last_path, end="00:01:12"): "Last"
    }
    for _ in tqdm(as_completed(futures), total=len(futures), desc="Trimming progress", unit="video"):
        pass

# === Handle middle videos ===
middle_outputs = []
if len(videos) > 2:
    print("\n🎞️  Copying middle videos...")
    with ThreadPoolExecutor(max_workers=os.cpu_count()) as executor:
        futures = []
        for filename in videos[1:-1]:
            src = os.path.join(input_folder, filename)
            dst = os.path.join(output_folder, filename)
            middle_outputs.append(dst)
            if not os.path.exists(dst):
                futures.append(executor.submit(fast_trim, src, dst))
        for _ in tqdm(as_completed(futures), total=len(futures), desc="Copying middle clips", unit="video"):
            pass
else:
    print("\n⚠️  Only two videos found (no middle videos to copy).")

# === Combine all clips using FFmpeg concat (instant) ===
print("\n🎬 Combining all clips (no re-encode)...")

concat_list_path = os.path.join(output_folder, "concat_list.txt")
with open(concat_list_path, "w", encoding="utf-8") as f:
    # Add trimmed first
    f.write(f"file '{trimmed_first_path.replace('\\', '/')}'\n")
    # Add middle ones
    for path in middle_outputs:
        f.write(f"file '{path.replace('\\', '/')}'\n")
    # Add trimmed last
    f.write(f"file '{trimmed_last_path.replace('\\', '/')}'\n")

final_output = os.path.join(output_folder, "final_combined_video.mp4")

cmd_concat = [
    ffmpeg_path, "-y", "-hide_banner", "-loglevel", "error",
    "-f", "concat", "-safe", "0",
    "-i", concat_list_path,
    "-c", "copy", final_output
]

subprocess.run(cmd_concat, check=True)

print(f"\n✅ Final combined video saved to:\n{final_output}")
