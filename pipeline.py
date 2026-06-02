import os
import re
import sys
import glob
import time
import subprocess
from PIL import Image
from google import genai
from google.genai import types
from dotenv import load_dotenv
import cv2

# Load environment variables
load_dotenv()

def timestamp_to_seconds(ts_str):
    """
    Converts a HH:MM:SS or MM:SS timestamp string to total seconds.
    """
    parts = ts_str.split(':')
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        elif len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
    except ValueError:
        pass
    return 0.0


def format_time(seconds):
    """
    Formats total seconds to a HH:MM:SS string.
    """
    return time.strftime('%H:%M:%S', time.gmtime(seconds))


def extract_lecture_keyframes(video_path, output_dir, interval_seconds=45):
    """
    Step 1: Extract keyframes from video using FFmpeg at a fixed time interval.
    This is extremely fast, avoids OpenCV seeking/reading bugs on large files, and is 100% stable.
    """
    print(f"--- Step 1: Extracting visual keyframes via FFmpeg (1 frame every {interval_seconds}s) ---")
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
        
    # FFmpeg command to extract frames at 1/interval_seconds fps, scaled to 1280px width
    command = [
        "ffmpeg", "-y",
        "-loglevel", "error",
        "-i", video_path,
        "-vf", f"fps=1/{interval_seconds},scale=1280:-1",
        "-q:v", "2",  # Quality factor (2 is very high quality, small file size)
        os.path.join(output_dir, "frame_%04d.jpg")
    ]
    try:
        # Show output so the user can see progress during extraction
        subprocess.run(command, check=True)
        
        # Rename the files to include timestamp for Gemini alignment
        extracted_files = sorted(glob.glob(os.path.join(output_dir, "frame_*.jpg")))
        for i, file_path in enumerate(extracted_files):
            # Frame index is 1-based from FFmpeg (frame_0001.jpg, frame_0002.jpg...)
            time_sec = i * interval_seconds
            timestamp_str = format_time(time_sec).replace(':', '_')
            new_path = os.path.join(output_dir, f"frame_{i:03d}_time_{timestamp_str}.jpg")
            os.rename(file_path, new_path)
            
        print(f"Keyframe extraction complete. Total frames saved: {len(extracted_files)}")
        return len(extracted_files)
    except Exception as e:
        print(f"FFmpeg frame extraction failed: {e}")
        return 0


def find_subtitle_file(video_path):
    """
    Checks for matching subtitle files (.vtt or .srt) in the directory.
    """
    base_name = os.path.splitext(video_path)[0]
    # Check exact matching base name with subtitle extensions
    for ext in ['.vtt', '.srt', '.en.vtt', '.en.srt']:
        sub_path = base_name + ext
        if os.path.exists(sub_path):
            return sub_path
            
    # Check any .vtt or .srt in the directory as a fallback
    dir_name = os.path.dirname(video_path) or '.'
    subs = glob.glob(os.path.join(dir_name, "*.vtt")) + glob.glob(os.path.join(dir_name, "*.srt"))
    if subs:
        print(f"No direct name-matched subtitles. Found alternative subtitle: {subs[0]}")
        return subs[0]
        
    return None


def parse_subtitles_for_range(subtitle_path, start_sec, end_sec):
    """
    Parses WebVTT (.vtt) or SubRip (.srt) subtitle files and extracts text within a time range.
    """
    with open(subtitle_path, 'r', encoding='utf-8', errors='ignore') as f:
        content = f.read()
        
    # Matches timestamp lines like: 00:01:23.456 --> 00:01:25.789
    pattern = r'(\d{2}:\d{2}:\d{2})[.,]\d{3}\s+-->\s+\d{2}:\d{2}:\d{2}[.,]\d{3}\s*\n(.*?)(?=\n\d+\n|\n\d{2}:\d{2}:\d{2}|WEBVTT|\Z)'
    matches = re.findall(pattern, content, re.DOTALL)
    
    parsed_lines = []
    for timestamp, text in matches:
        sub_sec = timestamp_to_seconds(timestamp)
        if start_sec <= sub_sec <= end_sec:
            # Clean HTML/styling tags and normalize spacing
            clean_text = re.sub(r'<[^>]+>', '', text).strip()
            clean_text = ' '.join(clean_text.split())
            if clean_text:
                parsed_lines.append(f"[{timestamp}] {clean_text}")
            
    return "\n".join(parsed_lines)


def extract_audio_slice(video_path, audio_output_path, start_sec, duration_sec):
    """
    Extracts a specific audio slice from the video file using FFmpeg and compresses it.
    """
    if os.path.exists(audio_output_path):
        os.remove(audio_output_path)
        
    command = [
        "ffmpeg", "-y",
        "-ss", str(start_sec),             # Seek start position
        "-t", str(duration_sec),           # Duration to extract
        "-i", video_path,
        "-vn",                             # Remove video track
        "-acodec", "libmp3lame",
        "-ar", "16000",                    # Downsample to 16kHz
        "-ac", "1",                        # Downsample to mono channel
        "-ab", "64k",                      # Compresses to ~64kbps to save tokens/upload size
        audio_output_path
    ]
    try:
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception as e:
        print(f"FFmpeg audio slice extraction failed: {e}")
        return False


def download_youtube_video(url):
    """
    Downloads a video from YouTube using yt-dlp with a multi-step fallback mechanism
    to capture subtitles and bypass 429 rate limit blocks.
    """
    print(f"--- Step 0: Downloading video from YouTube ---")
    video_output_path = "lecture_video.mp4"
    
    # Cleanup any old downloaded files matching 'lecture_video.*'
    for f in glob.glob("lecture_video.*"):
        try: os.remove(f)
        except: pass

    # Method 1: Subtitles + Edge Browser Cookies (Best bypass for 429)
    cmd1 = [
        "yt-dlp", "--cookies-from-browser", "edge",
        "-f", "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]",
        "--merge-output-format", "mp4",
        "--remux-video", "mp4",
        "--write-auto-subs", "--sub-lang", "en", "--convert-subs", "srt",
        "-o", "lecture_video.%(ext)s", url
    ]
    
    # Method 2: Subtitles only (standard request)
    cmd2 = [
        "yt-dlp",
        "-f", "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]",
        "--merge-output-format", "mp4",
        "--remux-video", "mp4",
        "--write-auto-subs", "--sub-lang", "en", "--convert-subs", "srt",
        "-o", "lecture_video.%(ext)s", url
    ]

    # Method 3: Video stream only (no subtitle request - bypasses 429 easily)
    cmd3 = [
        "yt-dlp",
        "-f", "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]",
        "--merge-output-format", "mp4",
        "--remux-video", "mp4",
        "-o", "lecture_video.%(ext)s", url
    ]
    
    success = False
    for i, cmd in enumerate([cmd1, cmd2, cmd3], 1):
        desc = ["Subtitles + Edge Cookies", "Subtitles only (standard)", "Video stream only (fallback)"][i-1]
        print(f"Trying download method {i}: {desc}...")
        try:
            # Let it show console progress
            subprocess.run(cmd, check=True)
            success = True
            break
        except subprocess.CalledProcessError:
            print(f"Method {i} failed.")
            
    if success:
        # Check standard output path
        if os.path.exists(video_output_path):
            return video_output_path
        # Return whatever video format it merged to (mkv, webm, mp4)
        for f in glob.glob("lecture_video.*"):
            if f.endswith(('.mp4', '.mkv', '.webm')):
                return f
    return None


def run_pipeline(video_path, output_notes_path="lecture_notes.md", threshold=0.10, cooldown_seconds=30):
    start_time = time.time()
    
    # Check Gemini API Key
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("Error: GEMINI_API_KEY env variable or .env file entry is missing.")
        return
    client = genai.Client(api_key=api_key)

    # 1. Get video metadata
    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS)
    frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT)
    if not fps or fps <= 0 or not frame_count:
        print("Error: Could not retrieve video metadata (FPS / Frame Count).")
        cap.release()
        return
    video_duration_sec = frame_count / fps
    cap.release()
    
    print(f"Video detected: {video_path}")
    print(f"Duration: {format_time(video_duration_sec)} ({video_duration_sec:.1f} seconds)")

    # 2. Extract Keyframes (all at once to run scanning loop only once)
    temp_img_dir = "./temp_keyframes"
    if os.path.exists(temp_img_dir):
        for f in glob.glob(os.path.join(temp_img_dir, "*")):
            os.remove(f)
            
    num_frames = extract_lecture_keyframes(video_path, temp_img_dir, interval_seconds=cooldown_seconds)
    if num_frames == 0:
        print("Error: Visual keyframe extraction yielded 0 frames. Aborting.")
        return

    # 3. Determine Chunk Sizing and Cooldown delays (Adaptive to respect 250k TPM rate limit)
    sub_file = find_subtitle_file(video_path)
    
    if sub_file:
        print(f"--- Step 2: Subtitle file found: {sub_file} ---")
        chunk_duration_sec = 10 * 60  # 10-minute chunks for text to ensure comprehensive coverage
        rate_limit_delay = 5          # 5-second wait to allow small sliding windows to reset
    else:
        print(f"--- Step 2: No subtitle file found. Using Audio fallback ---")
        chunk_duration_sec = 5 * 60   # 5-minute chunks for audio to prevent attention decay and ensure 100% complete coverage
        rate_limit_delay = 10         # 10-second cooldown between calls is safe for 5-minute segments under 250k TPM limit

    # Clear output file first
    with open(output_notes_path, "w", encoding="utf-8") as f:
        f.write(f"# Compiled Lecture Study Notes: {os.path.basename(video_path)}\n\n")
        f.write(f"*Processed chronologically using Gemini API chunks.*\n\n---\n\n")

    # 4. Processing Loop
    chunk_start = 0.0
    chunk_index = 1
    previous_context = "" # Rolling context to keep track of the end of the previous notes segment
    
    # Shared prompt instruction template for self-contained classroom notes
    system_instruction = (
        "You are an elite academic instructor and expert scribe. Your goal is to produce "
        "fully independent, self-contained, textbook-quality classroom study notes based on the provided inputs. "
        "The notes must be so detailed and clear that a student can read them, learn the topic, understand every concept, "
        "and replicate every mathematical derivation from scratch without watching the video.\n\n"
        "Requirements:\n"
        "1. **Independent Readability**: Do not write high-level summaries or references to the video itself. Write full narrative "
        "explanations. Define every physical setup, coordinate system, variable, constant, and physical term explicitly.\n"
        "2. **Exhaustive Mathematics**: Replicate *every single mathematical step* shown on the board or discussed in the audio. "
        "Do not skip steps. Show starting formulas, algebraic rearrangements, boundary conditions for integrals, substitutions, and final expressions.\n"
        "3. **Capture Spoken Nuances**: Include the instructor's verbal examples, physical analogies, explanations of *why* steps are performed, "
        "and warnings about common student mistakes. Use callout boxes (e.g. '> [!WARNING] Common Mistake: ...' or '> [!NOTE] Explanation: ...') for emphasis.\n"
        "4. **Whiteboard Illustrations & Diagrams**: Do not use ASCII art. Instead, you MUST embed the actual "
        "whiteboard image/slide displaying the drawing or diagram. To do this, identify the keyframe image in your inputs "
        "that shows the diagram most clearly, and embed it using its exact filename. Format it exactly as: "
        "`![Description of Diagram](filename.jpg)` (e.g. `![Electric field lines in sphere](frame_023_time_00_11_30.jpg)`). "
        "Place the embedded image immediately before the explanation or derivation of that diagram. "
        "**Unobstructed View rule**: If the instructor is standing in front of or blocking a diagram or writing in one frame, "
        "look at the surrounding or subsequent frames to find the moment where the instructor has moved away and "
        "the board/diagram is fully completed and completely unobstructed. Always select and embed the filename of this "
        "clearest, cleanest version.\n"
        "5. **Format**: Render all math, equations, physical parameters, and chemical symbols in LaTeX ($...$ for inline, $$...$$ for blocks). "
        "Structure sections cleanly with Markdown headers, bullet points, numbered lists, and comparison tables."
    )

    while chunk_start < video_duration_sec:
        chunk_end = min(chunk_start + chunk_duration_sec, video_duration_sec)
        duration_to_extract = chunk_end - chunk_start
        
        print(f"\n==================================================")
        print(f"Processing segment {chunk_index}: {format_time(chunk_start)} to {format_time(chunk_end)}")
        print(f"==================================================")

        # A. Filter image keyframes belonging to this segment time range
        segment_images = []
        image_paths = sorted(glob.glob(os.path.join(temp_img_dir, "frame_*.jpg")))
        for img_path in image_paths:
            match = re.search(r'time_(\d{2})_(\d{2})_(\d{2})', img_path)
            if match:
                h, m, s = map(int, match.groups())
                img_time_sec = h * 3600 + m * 60 + s
                if chunk_start <= img_time_sec <= chunk_end:
                    segment_images.append(img_path)

        # B. Get Transcript (subtitles or sliced audio fallback)
        transcript_text = None
        audio_file_path = None
        uploaded_audio = None
        
        if sub_file:
            transcript_text = parse_subtitles_for_range(sub_file, chunk_start, chunk_end)
        else:
            audio_file_path = f"./temp_slice_{chunk_index}.mp3"
            print(f"Extracting audio segment ({format_time(chunk_start)} to {format_time(chunk_end)})...")
            if not extract_audio_slice(video_path, audio_file_path, chunk_start, duration_to_extract):
                print(f"Skipping segment {chunk_index} due to FFmpeg failure.")
                chunk_start = chunk_end
                chunk_index += 1
                continue

        # C. Assemble multimodal inputs
        contents = []
        
        # Add audio file if fallback
        if audio_file_path and os.path.exists(audio_file_path):
            print("Uploading audio track slice to Gemini File API...")
            uploaded_audio = client.files.upload(file=audio_file_path)
            contents.append(uploaded_audio)
            
            # Wait for file to become active
            while uploaded_audio.state.name == "PROCESSING":
                time.sleep(2)
                uploaded_audio = client.files.get(name=uploaded_audio.name)
            if uploaded_audio.state.name == "FAILED":
                raise Exception("Uploaded audio slice processing failed on Gemini server.")
        
        # Add keyframe images
        print(f"Attaching {len(segment_images)} segment keyframes...")
        for img_path in segment_images:
            try:
                img = Image.open(img_path)
                contents.append(img)
            except Exception as e:
                print(f"Failed to load keyframe {img_path}: {e}")

        # Add prompt instructions
        prompt = (
            f"You are writing complete, independent study notes for the lecture segment from {format_time(chunk_start)} to {format_time(chunk_end)}.\n"
            f"You have been provided with visual keyframe images representing visual checkpoints in this range (each filename indicates its exact timestamp). "
        )
        if transcript_text:
            prompt += (
                f"And the matching text transcript below:\n\n{transcript_text}\n\n"
                "Align the visual images with the spoken transcript chronologically to write complete, textbook-style notes."
            )
        else:
            prompt += (
                "And the raw audio track. Listen to the audio slice, cross-reference it with the board captures, "
                "and compile the complete, detailed study notes."
            )
            
        # List the attached image filenames in the prompt so the model knows their exact names
        prompt += "\n\nThe following keyframe images are attached in your input contents in chronological order. If you choose to embed any of them, you MUST use their exact filename from this list:\n"
        for img_path in segment_images:
            prompt += f"- {os.path.basename(img_path)}\n"
            
        if previous_context:
            prompt += (
                f"\n\nHere is the tail end of the previous segment's notes for reference. "
                "Ensure smooth transitions, continuous flow, and consistent mathematical notation:\n"
                f"...\n{previous_context}\n"
            )
            
        contents.append(prompt)

        # D. Call Gemini API
        model_name = "gemini-3.1-flash-lite"
        print(f"Contacting Gemini ({model_name}) for segment notes...")
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    temperature=0.1
                )
            )
            
            # Save a slice of the generated text to carry over as context to the next chunk
            previous_context = response.text[-2000:] if response.text else ""
            
            # Copy embedded whiteboard diagram images to a permanent directory and rewrite paths
            cleaned_text = response.text if response.text else ""
            # Matches standard markdown image links like ![Alt Text](frame_023_time_00_11_30.jpg) or ![Alt](notes_media/frame_023_time_00_11_30.jpg)
            image_links = re.findall(r'!\[(.*?)]\(((?:.*?/)?(frame_\d{3}_time_.*?\.jpg))\)', cleaned_text)
            
            if image_links:
                import shutil
                media_dir = "./notes_media"
                if not os.path.exists(media_dir):
                    os.makedirs(media_dir)
                for alt_text, full_path_in_link, filename in image_links:
                    src_path = os.path.join(temp_img_dir, filename)
                    dst_path = os.path.join(media_dir, filename)
                    if os.path.exists(src_path):
                        if not os.path.exists(dst_path):
                            shutil.copy(src_path, dst_path)
                            print(f" Copied diagram image to: {dst_path}")
                        relative_link = f"./notes_media/{filename}"
                        cleaned_text = cleaned_text.replace(f"({full_path_in_link})", f"({relative_link})")
            
            # Write to output notes file
            with open(output_notes_path, "a", encoding="utf-8") as f:
                f.write(f"## Segment: {format_time(chunk_start)} - {format_time(chunk_end)}\n\n")
                f.write(cleaned_text)
                f.write("\n\n---\n\n")
                
            print(f"Notes for segment {chunk_index} written successfully.")
            
        except Exception as e:
            print(f"API generation failed for segment {chunk_index}: {e}")
            
        finally:
            # Cleanup temp files for this chunk
            if audio_file_path and os.path.exists(audio_file_path):
                try: os.remove(audio_file_path)
                except: pass
            if uploaded_audio:
                try: client.files.delete(name=uploaded_audio.name)
                except: pass

        # Move to next chunk
        chunk_start = chunk_end
        chunk_index += 1
        
        # E. Rate-limit cooldown delay to prevent hitting 250k TPM limit
        if chunk_start < video_duration_sec:
            print(f"Sleeping for {rate_limit_delay} seconds to stay under token rate limits...")
            time.sleep(rate_limit_delay)

    # 5. Final Workspace Cleanup
    print("\n--- Pipeline execution complete. Cleaning up workspace ---")
    if os.path.exists(temp_img_dir):
        for f in glob.glob(os.path.join(temp_img_dir, "*")):
            try: os.remove(f)
            except: pass
        try: os.rmdir(temp_img_dir)
        except: pass
        
    elapsed = time.time() - start_time
    print(f"All chunks compiled! Output file: {output_notes_path}")
    print(f"Total processing completed in {elapsed:.1f} seconds.")
    
    # Auto-compile HTML companion notes
    html_notes_path = os.path.splitext(output_notes_path)[0] + ".html"
    try:
        compile_markdown_to_html(output_notes_path, html_notes_path)
    except Exception as e:
        print(f"Warning: Failed to compile HTML textbook preview: {e}")


def compile_markdown_to_html(md_path, html_path):
    """
    Renders the study notes Markdown file to a beautifully styled, self-contained HTML page
    with responsive fonts, clean margins, and CDNs for Markdown (marked.js) and LaTeX (MathJax).
    """
    print("--- Step 4: Compiling Markdown to HTML Textbook format ---")
    if not os.path.exists(md_path):
        print(f"Error: Markdown file {md_path} not found for HTML generation.")
        return

    with open(md_path, "r", encoding="utf-8") as f:
        md_content = f.read()

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Lecture Study Notes</title>
    <!-- Google Fonts -->
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=Outfit:wght@400;500;600;700&display=swap" rel="stylesheet">
    
    <!-- Marked.js for Markdown parsing -->
    <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
    
    <!-- MathJax for LaTeX equations -->
    <script>
        window.MathJax = {{
            tex: {{
                inlineMath: [['$', '$'], ['\\\\(', '\\\\)']],
                displayMath: [['$$', '$$'], ['\\\\[', '\\\\]']],
                processEscapes: true
            }},
            options: {{
                ignoreHtmlClass: 'tex2jax_ignore',
                processHtmlClass: 'tex2jax_process'
            }}
        }};
    </script>
    <script id="MathJax-script" async src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js"></script>
    
    <style>
        :root {{
            --bg-color: #0f172a;
            --text-color: #f8fafc;
            --primary-color: #6366f1;
            --border-color: #334155;
            --card-bg: #1e293b;
        }}
        
        body {{
            font-family: 'Inter', sans-serif;
            background-color: var(--bg-color);
            color: var(--text-color);
            line-height: 1.6;
            margin: 0;
            padding: 0;
        }}
        
        .container {{
            max-width: 900px;
            margin: 0 auto;
            padding: 40px 20px;
        }}
        
        h1, h2, h3, h4 {{
            font-family: 'Outfit', sans-serif;
            color: #818cf8;
            margin-top: 1.8em;
            margin-bottom: 0.8em;
        }}
        
        h1 {{
            border-bottom: 2px solid var(--border-color);
            padding-bottom: 10px;
            font-size: 2.2em;
            color: #a5b4fc;
        }}
        
        h2 {{
            border-bottom: 1px solid var(--border-color);
            padding-bottom: 8px;
            font-size: 1.6em;
        }}
        
        a {{
            color: var(--primary-color);
            text-decoration: none;
        }}
        
        a:hover {{
            text-decoration: underline;
        }}
        
        img {{
            max-width: 100%;
            height: auto;
            border-radius: 8px;
            border: 1px solid var(--border-color);
            margin: 20px 0;
            box-shadow: 0 4px 6px -1px rgb(0 0 0 / 0.1), 0 2px 4px -2px rgb(0 0 0 / 0.1);
        }}
        
        code {{
            background-color: #020617;
            padding: 2px 6px;
            border-radius: 4px;
            font-family: monospace;
            font-size: 0.9em;
        }}
        
        pre {{
            background-color: #020617;
            padding: 15px;
            border-radius: 8px;
            overflow-x: auto;
            border: 1px solid var(--border-color);
        }}
        
        pre code {{
            background-color: transparent;
            padding: 0;
        }}
        
        blockquote {{
            border-left: 4px solid var(--primary-color);
            margin: 20px 0;
            padding: 10px 20px;
            background-color: var(--card-bg);
            border-radius: 0 8px 8px 0;
            color: #cbd5e1;
        }}
        
        /* GitHub-style alerts styling */
        .alert {{
            border-left: 4px solid;
            border-radius: 0 6px 6px 0;
            padding: 12px 20px;
            margin: 20px 0;
        }}
        .alert-title {{
            font-weight: 700;
            margin-bottom: 4px;
            text-transform: uppercase;
            font-size: 0.85em;
            letter-spacing: 0.05em;
            display: flex;
            align-items: center;
            gap: 6px;
        }}
        .alert-note {{
            border-color: #3b82f6;
            background-color: rgba(59, 130, 246, 0.1);
        }}
        .alert-note .alert-title {{ color: #60a5fa; }}
        
        .alert-warning {{
            border-color: #f59e0b;
            background-color: rgba(245, 158, 11, 0.1);
        }}
        .alert-warning .alert-title {{ color: #fbbf24; }}
        
        .alert-tip {{
            border-color: #10b981;
            background-color: rgba(16, 185, 129, 0.1);
        }}
        .alert-tip .alert-title {{ color: #34d399; }}
        
        .alert-important {{
            border-color: #8b5cf6;
            background-color: rgba(139, 92, 246, 0.1);
        }}
        .alert-important .alert-title {{ color: #a78bfa; }}
        
        .alert-caution {{
            border-color: #ef4444;
            background-color: rgba(239, 68, 68, 0.1);
        }}
        .alert-caution .alert-title {{ color: #f87171; }}
        
        table {{
            width: 100%;
            border-collapse: collapse;
            margin: 20px 0;
        }}
        
        th, td {{
            padding: 12px;
            border: 1px solid var(--border-color);
            text-align: left;
        }}
        
        th {{
            background-color: var(--card-bg);
            color: #818cf8;
        }}
        
        tr:nth-child(even) {{
            background-color: rgba(30, 41, 59, 0.5);
        }}

        hr {{
            border: 0;
            border-top: 1px solid var(--border-color);
            margin: 40px 0;
        }}
        
        /* Print optimization */
        @media print {{
            :root {{
                --bg-color: #ffffff;
                --text-color: #000000;
                --primary-color: #000000;
                --border-color: #cccccc;
                --card-bg: #f5f5f5;
            }}
            body {{
                font-size: 12pt;
                background-color: #ffffff;
                color: #000000;
            }}
            h1, h2, h3, h4 {{
                color: #000000;
                page-break-after: avoid;
            }}
            pre, blockquote, tr {{
                page-break-inside: avoid;
            }}
            img {{
                max-width: 100%;
                page-break-inside: avoid;
            }}
            .container {{
                max-width: 100%;
                padding: 0;
            }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <div id="content" class="tex2jax_process"></div>
    </div>
    
    <script>
        const rawMarkdown = {repr(md_content)};
        
        // 1. Extract math blocks to protect them from marked.js parsing
        const mathBlocks = [];
        let placeholderCount = 0;
        let tempMarkdown = rawMarkdown;
        
        // Block math: $$ ... $$
        tempMarkdown = tempMarkdown.replace(/\\$\\$([\\s\\S]+?)\\$\\$/g, (match) => {{
            const placeholder = `@@MATH_BLOCK_${{placeholderCount}}@@`;
            mathBlocks.push({{ placeholder, content: match }});
            placeholderCount++;
            return placeholder;
        }});
        
        // Inline math: $ ... $ (ensuring we don't match double dollar placeholders)
        tempMarkdown = tempMarkdown.replace(/\\$([^$\\n]+?)\\$/g, (match) => {{
            const placeholder = `@@MATH_BLOCK_${{placeholderCount}}@@`;
            mathBlocks.push({{ placeholder, content: match }});
            placeholderCount++;
            return placeholder;
        }});
        
        // 2. Parse Markdown to HTML
        let renderedHtml = marked.parse(tempMarkdown);
        
        // 3. Restore math blocks
        for (const block of mathBlocks) {{
            renderedHtml = renderedHtml.replace(block.placeholder, () => block.content);
        }}
        
        // 4. Update the DOM
        const contentDiv = document.getElementById('content');
        contentDiv.innerHTML = renderedHtml;
        
        // 5. Post-process blockquotes for GitHub-style alerts
        contentDiv.querySelectorAll('blockquote').forEach(bq => {{
            const p = bq.querySelector('p');
            if (p) {{
                const match = p.innerHTML.match(/^\\[!(NOTE|WARNING|TIP|IMPORTANT|CAUTION)\\]/i);
                if (match) {{
                    const type = match[1].toUpperCase();
                    p.innerHTML = p.innerHTML.replace(/^\\[!(NOTE|WARNING|TIP|IMPORTANT|CAUTION)\\]\\s*/i, '');
                    bq.classList.add('alert', `alert-${{type.toLowerCase()}}`);
                    
                    const title = document.createElement('div');
                    title.className = 'alert-title';
                    title.innerText = type;
                    bq.insertBefore(title, p);
                }}
            }}
        }});
        
        // 6. Trigger MathJax to typeset the dynamic content
        function triggerMathJax() {{
            if (window.MathJax && typeof window.MathJax.typeset === 'function') {{
                window.MathJax.typeset();
            }}
        }}
        
        triggerMathJax();
        
        // Also listen to script load in case MathJax is still loading
        const mjScript = document.getElementById('MathJax-script');
        if (mjScript) {{
            mjScript.addEventListener('load', triggerMathJax);
        }}
    </script>
</body>
</html>
"""
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_content)
    print(f"HTML textbook successfully generated at: {html_path}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python pipeline.py <video_file_path_or_youtube_url> [output_notes_path] [threshold] [cooldown_seconds]")
        print("Example: python pipeline.py https://youtu.be/StwUNDxdw2c lecture_notes.md 0.10 30")
        sys.exit(1)
        
    vid_file = sys.argv[1]
    out_notes = sys.argv[2] if len(sys.argv) > 2 else "lecture_notes.md"
    thresh = float(sys.argv[3]) if len(sys.argv) > 3 else 0.10
    cooldown = int(sys.argv[4]) if len(sys.argv) > 4 else 30
    
    # Auto-detect if input is a YouTube URL
    if vid_file.startswith(('http://', 'https://', 'www.', 'youtu.be')):
        downloaded_path = download_youtube_video(vid_file)
        if downloaded_path:
            vid_file = downloaded_path
        else:
            print("Error: YouTube video download failed. Aborting.")
            sys.exit(1)
            
    run_pipeline(vid_file, out_notes, thresh, cooldown)
