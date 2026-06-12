import os
import re
import sys
import glob
import time
import asyncio
from PIL import Image
from google import genai
from google.genai import types
from dotenv import load_dotenv
import cv2
import logging
import shutil

# Process tracking for graceful shutdown
_active_subprocesses = []
_process_lock = asyncio.Lock()

async def run_subprocess_async(command, timeout=600, **kwargs):
    """
    Async wrapper for subprocess execution that tracks active processes for graceful termination.
    Handles 'check=True' behavior if provided.
    Includes a timeout to prevent hanging zombie processes.
    """
    check = kwargs.pop('check', False)
    
    # We remove stdout and stderr from kwargs to let them pipe to terminal or DEVNULL as needed.
    # asyncio.create_subprocess_exec expects individual string arguments, not a list.
    program = command[0]
    args = command[1:]
    
    async with _process_lock:
        proc = await asyncio.create_subprocess_exec(program, *args, **kwargs)
        _active_subprocesses.append(proc)
    
    try:
        returncode = await asyncio.wait_for(proc.wait(), timeout=timeout)
        if check and returncode != 0:
            raise Exception(f"Command '{' '.join(command)}' returned non-zero exit status {returncode}.")
        return proc
    except asyncio.TimeoutError:
        try:
            proc.terminate()
        except:
            pass
        raise Exception(f"Command '{' '.join(command)}' timed out after {timeout} seconds.")
    finally:
        async with _process_lock:
            if proc in _active_subprocesses:
                _active_subprocesses.remove(proc)

# Load environment variables
load_dotenv()
logger = logging.getLogger("lecture_notes_pipeline")

def timestamp_to_seconds(ts_str):
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
    return time.strftime('%H:%M:%S', time.gmtime(seconds))

async def extract_lecture_keyframes(video_path, output_dir, interval_seconds=45):
    print(f"--- Step 1: Extracting visual keyframes via FFmpeg (1 frame every {interval_seconds}s) ---")
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
        
    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS)
    frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT)
    if not fps or fps <= 0 or not frame_count:
        print("Error: Could not retrieve video duration for keyframe extraction.")
        cap.release()
        return 0
    duration_sec = frame_count / fps
    cap.release()

    extracted_count = 0
    time_sec = 0.0
    frame_index = 0

    while time_sec < duration_sec:
        timestamp_str = format_time(time_sec).replace(':', '_')
        output_file = os.path.join(output_dir, f"frame_{frame_index:03d}_time_{timestamp_str}.jpg")
        
        command = [
            "ffmpeg", "-y",
            "-nostdin",
            "-loglevel", "error",
            "-threads", "1",
            "-ss", str(time_sec),
            "-i", video_path,
            "-vframes", "1",
            "-vf", "scale=854:480:force_original_aspect_ratio=decrease,scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-q:v", "2",
            output_file
        ]
        
        try:
            await run_subprocess_async(command, check=True)
            extracted_count += 1
        except Exception as e:
            print(f"Failed to extract frame at {time_sec}s: {e}")
            
        time_sec += interval_seconds
        frame_index += 1

    print(f"Keyframe extraction complete. Total frames saved: {extracted_count}")
    return extracted_count

def find_subtitle_file(video_path):
    base_name = os.path.splitext(video_path)[0]
    for ext in ['.vtt', '.srt', '.en.vtt', '.en.srt']:
        sub_path = base_name + ext
        if os.path.exists(sub_path):
            return sub_path
            
    dir_name = os.path.dirname(video_path) or '.'
    subs = glob.glob(os.path.join(dir_name, "*.vtt")) + glob.glob(os.path.join(dir_name, "*.srt"))
    if subs:
        print(f"No direct name-matched subtitles. Found alternative subtitle: {subs[0]}")
        return subs[0]
        
    return None

def parse_subtitles_for_range(subtitle_path, start_sec, end_sec):
    with open(subtitle_path, 'r', encoding='utf-8', errors='ignore') as f:
        content = f.read()
        
    pattern = r'(\d{2}:\d{2}:\d{2})[.,]\d{3}\s+-->\s+\d{2}:\d{2}:\d{2}[.,]\d{3}\s*\n(.*?)(?=\n\d+\n|\n\d{2}:\d{2}:\d{2}|WEBVTT|\Z)'
    matches = re.findall(pattern, content, re.DOTALL)
    
    parsed_lines = []
    for timestamp, text in matches:
        sub_sec = timestamp_to_seconds(timestamp)
        if start_sec <= sub_sec <= end_sec:
            clean_text = re.sub(r'<[^>]+>', '', text).strip()
            clean_text = ' '.join(clean_text.split())
            if clean_text:
                parsed_lines.append(f"[{timestamp}] {clean_text}")
            
    return "\n".join(parsed_lines)

async def extract_audio_slice(video_path, audio_output_path, start_sec, duration_sec):
    if os.path.exists(audio_output_path):
        os.remove(audio_output_path)
        
    command = [
        "ffmpeg", "-y",
        "-nostdin",
        "-threads", "1",
        "-ss", str(start_sec),
        "-t", str(duration_sec),
        "-i", video_path,
        "-vn",
        "-acodec", "libmp3lame",
        "-ar", "16000",
        "-ac", "1",
        "-ab", "64k",
        audio_output_path
    ]
    try:
        await run_subprocess_async(command, check=True, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        return True
    except Exception as e:
        print(f"FFmpeg audio slice extraction failed: {e}")
        return False

async def download_youtube_video(url, job_id, workspace_dir, cookies_file=None):
    print("--- Step 0: Downloading video from YouTube ---")
    video_output_path = os.path.join(workspace_dir, f"lecture_video_{job_id}.mp4")

    common_args = [
        "--no-cache-dir",
        "-f", "bv*[vcodec^=avc][height<=720]+ba[ext=m4a] / bv*[ext=mp4][height<=720]+ba[ext=m4a] / b[ext=mp4] / best",
        "--merge-output-format", "mp4",
        "--remux-video", "mp4",
        "--remote-components", "ejs:github",
        "-o", os.path.join(workspace_dir, f"lecture_video_{job_id}.%(ext)s")
    ]

    if cookies_file and os.path.exists(cookies_file):
        print(f"Injecting session cookies from: {cookies_file}")
        common_args = ["--cookies", cookies_file] + common_args

    sub_args = [
        "--write-auto-subs", "--sub-lang", "en", "--convert-subs", "srt"
    ]

    cmd1 = ["yt-dlp", "--cookies-from-browser", "edge"] + common_args + sub_args + [url]
    cmd2 = ["yt-dlp", "--extractor-args", "youtube:player-client=ios,android"] + common_args + sub_args + [url]
    cmd3 = ["yt-dlp", "--extractor-args", "youtube:player-client=web_embedded,tv_embedded"] + common_args + sub_args + [url]
    cmd4 = ["yt-dlp", "--extractor-args", "youtube:player-client=ios,android"] + common_args + [url]
    cmd5 = ["yt-dlp", "--extractor-args", "youtube:player-client=web_embedded,tv_embedded"] + common_args + [url]
    cmd6 = ["yt-dlp"] + common_args + [url]

    commands = [cmd1, cmd2, cmd3, cmd4, cmd5, cmd6]
    descriptions = [
        "Subtitles + Local Edge Cookies (Windows)",
        "Subtitles + Mobile Client Spoofing (iOS/Android)",
        "Subtitles + TV/Embedded Client Spoofing",
        "Video Stream Only + Mobile Client Spoofing",
        "Video Stream Only + TV/Embedded Client Spoofing",
        "Video Stream Only (Standard Fallback)"
    ]

    success = False
    for i, cmd in enumerate(commands, 1):
        desc = descriptions[i-1]
        print(f"Trying download method {i}: {desc}...")
        try:
            await run_subprocess_async(cmd, check=True)
            success = True
            break
        except Exception:
            print(f"Method {i} failed.")
            
    if success:
        if os.path.exists(video_output_path):
            return video_output_path
        for f in glob.glob(os.path.join(workspace_dir, f"lecture_video_{job_id}.*")):
            if f.endswith(('.mp4', '.mkv', '.webm')):
                return f
    return None

async def run_pipeline_task_async(job_id: str, url: str):
    """
    Main background pipeline to process videos, upload images to Supabase, 
    and write final markdown/html into the Postgres DB.
    """
    import main  # Import to use the DB updating function and Supabase client
    
    workspace_dir = f"./tmp/lecture_pipeline_{job_id}"
    os.makedirs(workspace_dir, exist_ok=True)
    
    try:
        # Update status
        await main.async_update_job_in_db(job_id, {"progress": 10, "message": "Downloading video from YouTube..."})
        
        # Determine cookies path if exists
        cookies_file = os.path.join(workspace_dir, "cookies.txt")
        if not os.path.exists(cookies_file):
            cookies_file = None
            
        vid_file = await download_youtube_video(url, job_id, workspace_dir, cookies_file=cookies_file)
        
        if not vid_file:
            await main.async_update_job_in_db(job_id, {"progress": 25, "message": "Download blocked. Fetching transcript directly from YouTube..."})
            transcript_text = fetch_youtube_transcript_fallback(url)
            if not transcript_text:
                raise Exception("YouTube download failed and no transcript could be retrieved.")
            
            await main.async_update_job_in_db(job_id, {"progress": 50, "message": "Compiling textbook study notes from transcript..."})
            md_text, html_text = await run_pipeline_transcript_only_async(url, transcript_text, job_id)
        else:
            await main.async_update_job_in_db(job_id, {"progress": 30, "message": "Extracting keyframes and audio slices..."})
            md_text, html_text = await run_pipeline_multimodal_async(vid_file, job_id, workspace_dir, main.async_update_job_in_db, main.supabase)
            
        # Push final strings to Supabase and mark completed
        await main.async_update_job_in_db(job_id, {
            "status": "completed",
            "progress": 100,
            "message": "Notes successfully compiled!",
            "markdown": md_text,
            "html": html_text
        })

    except Exception as e:
        await main.async_update_job_in_db(job_id, {
            "status": "failed",
            "progress": 100,
            "message": f"Error: {str(e)}"
        })
    finally:
        # Final cleanup
        if os.path.exists(workspace_dir):
            try:
                shutil.rmtree(workspace_dir)
            except OSError as e:
                logger.warning(f"Failed to cleanup workspace {workspace_dir}: {e}")

async def run_pipeline_multimodal_async(video_path, job_id, workspace_dir, update_db_cb, supabase_client):
    start_time = time.time()
    
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise Exception("GEMINI_API_KEY env variable or .env file entry is missing.")
    client = genai.Client(api_key=api_key)

    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS)
    frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT)
    if not fps or fps <= 0 or not frame_count:
        cap.release()
        raise Exception("Could not retrieve video metadata (FPS / Frame Count).")
    video_duration_sec = frame_count / fps
    cap.release()

    temp_img_dir = os.path.join(workspace_dir, "keyframes")
    os.makedirs(temp_img_dir, exist_ok=True)
            
    cooldown_seconds = 30
    num_frames = await extract_lecture_keyframes(video_path, temp_img_dir, interval_seconds=cooldown_seconds)
    if num_frames == 0:
        raise Exception("Visual keyframe extraction yielded 0 frames. Aborting.")

    sub_file = find_subtitle_file(video_path)
    
    if sub_file:
        chunk_duration_sec = 10 * 60  
        rate_limit_delay = 5          
    else:
        chunk_duration_sec = 5 * 60   
        rate_limit_delay = 10         

    md_text = f"# Compiled Lecture Study Notes: {os.path.basename(video_path)}\n\n*Processed chronologically using Gemini API chunks.*\n\n---\n\n"

    chunk_start = 0.0
    chunk_index = 1
    previous_context = "" 
    
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

    import math
    total_chunks = max(1, math.ceil(video_duration_sec / chunk_duration_sec))

    while chunk_start < video_duration_sec:
        chunk_end = min(chunk_start + chunk_duration_sec, video_duration_sec)
        duration_to_extract = chunk_end - chunk_start
        
        percent = 30 + int(((chunk_index - 1) / total_chunks) * 60)
        await update_db_cb(job_id, {"progress": percent, "message": f"Compiling study notes: segment {chunk_index} of {total_chunks}..."})
        
        segment_images = []
        image_paths = sorted(glob.glob(os.path.join(temp_img_dir, "frame_*.jpg")))
        for img_path in image_paths:
            match = re.search(r'time_(\d{2})_(\d{2})_(\d{2})', img_path)
            if match:
                h, m, s = map(int, match.groups())
                img_time_sec = h * 3600 + m * 60 + s
                if chunk_start <= img_time_sec <= chunk_end:
                    segment_images.append(img_path)

        transcript_text = None
        audio_file_path = None
        uploaded_audio = None
        
        if sub_file:
            transcript_text = parse_subtitles_for_range(sub_file, chunk_start, chunk_end)
        else:
            audio_file_path = os.path.join(workspace_dir, f"temp_slice_{chunk_index}.mp3")
            success = await extract_audio_slice(video_path, audio_file_path, chunk_start, duration_to_extract)
            if not success:
                chunk_start = chunk_end
                chunk_index += 1
                continue

        contents = []
        
        if audio_file_path and os.path.exists(audio_file_path):
            def _upload_file():
                return client.files.upload(file=audio_file_path)
            uploaded_audio = await asyncio.to_thread(_upload_file)
            contents.append(uploaded_audio)
            
            while uploaded_audio.state.name == "PROCESSING":
                await asyncio.sleep(2)
                def _get_file():
                    return client.files.get(name=uploaded_audio.name)
                uploaded_audio = await asyncio.to_thread(_get_file)
            if uploaded_audio.state.name == "FAILED":
                raise Exception("Uploaded audio slice processing failed on Gemini server.")
        
        for img_path in segment_images:
            try:
                img = Image.open(img_path)
                contents.append(img)
            except Exception as e:
                print(f"Failed to load keyframe {img_path}: {e}")

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

        model_name = "gemini-3.1-flash-lite"
        try:
            def _generate():
                return client.models.generate_content(
                    model=model_name,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        system_instruction=system_instruction,
                        temperature=0.1
                    )
                )
            # Use the asyncio to thread fallback for genai if we don't use client.aio directly
            response = await asyncio.to_thread(_generate)
            
            cleaned_text = response.text if response.text else ""
            previous_context = cleaned_text[-2000:] if cleaned_text else ""
            
            # Find embedded images and upload them to Supabase Storage
            image_links = re.findall(r'!\[(.*?)]\(((?:.*?/)?(frame_\d{3}_time_.*?\.jpg))\)', cleaned_text)
            
            if image_links:
                for alt_text, full_path_in_link, filename in image_links:
                    src_path = os.path.join(temp_img_dir, filename)
                    if os.path.exists(src_path):
                        # Upload to Supabase Bucket `lecture_media`
                        unique_filename = f"{job_id}_{filename}"
                        with open(src_path, "rb") as f:
                            file_bytes = f.read()
                            
                        def _upload_sb():
                            # Remove if it exists to overwrite or just upload
                            try:
                                supabase_client.storage.from_("lecture_media").upload(
                                    file=file_bytes, 
                                    path=unique_filename,
                                    file_options={"content-type": "image/jpeg"}
                                )
                            except Exception as ex:
                                if "Duplicate" in str(ex):
                                    pass # ignore if already uploaded
                        await asyncio.to_thread(_upload_sb)
                        
                        # Get public URL
                        def _get_url():
                            return supabase_client.storage.from_("lecture_media").get_public_url(unique_filename)
                        public_url = await asyncio.to_thread(_get_url)
                        
                        cleaned_text = cleaned_text.replace(f"({full_path_in_link})", f"({public_url})")
            
            md_text += f"## Segment: {format_time(chunk_start)} - {format_time(chunk_end)}\n\n{cleaned_text}\n\n---\n\n"
            
        except Exception as e:
            print(f"API generation failed for segment {chunk_index}: {e}")
            
        finally:
            if audio_file_path and os.path.exists(audio_file_path):
                try:
                    os.remove(audio_file_path)
                except OSError:
                    pass
            if uploaded_audio:
                try:
                    def _del():
                        client.files.delete(name=uploaded_audio.name)
                    await asyncio.to_thread(_del)
                except Exception:
                    pass

        chunk_start = chunk_end
        chunk_index += 1
        
        if chunk_start < video_duration_sec:
            await asyncio.sleep(rate_limit_delay)

    await update_db_cb(job_id, {"progress": 95, "message": "Compiling HTML textbook preview..."})
    html_text = compile_markdown_to_html_string(md_text)
    return md_text, html_text

def compile_markdown_to_html_string(md_content):
    """
    Renders the study notes Markdown string to a beautifully styled, self-contained HTML page.
    """
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
        .container {{ max-width: 900px; margin: 0 auto; padding: 40px 20px; }}
        h1, h2, h3, h4 {{ font-family: 'Outfit', sans-serif; color: #818cf8; margin-top: 1.8em; margin-bottom: 0.8em; }}
        h1 {{ border-bottom: 2px solid var(--border-color); padding-bottom: 10px; font-size: 2.2em; color: #a5b4fc; }}
        h2 {{ border-bottom: 1px solid var(--border-color); padding-bottom: 8px; font-size: 1.6em; }}
        a {{ color: var(--primary-color); text-decoration: none; }}
        a:hover {{ text-decoration: underline; }}
        img {{ max-width: 100%; height: auto; border-radius: 8px; border: 1px solid var(--border-color); margin: 20px 0; box-shadow: 0 4px 6px -1px rgb(0 0 0 / 0.1); }}
        code {{ background-color: #020617; padding: 2px 6px; border-radius: 4px; font-family: monospace; font-size: 0.9em; }}
        pre {{ background-color: #020617; padding: 15px; border-radius: 8px; overflow-x: auto; border: 1px solid var(--border-color); }}
        pre code {{ background-color: transparent; padding: 0; }}
        blockquote {{ border-left: 4px solid var(--primary-color); margin: 20px 0; padding: 10px 20px; background-color: var(--card-bg); border-radius: 0 8px 8px 0; color: #cbd5e1; }}
        /* GitHub-style alerts styling */
        .alert {{ border-left: 4px solid; border-radius: 0 6px 6px 0; padding: 12px 20px; margin: 20px 0; }}
        .alert-title {{ font-weight: 700; margin-bottom: 4px; text-transform: uppercase; font-size: 0.85em; letter-spacing: 0.05em; display: flex; align-items: center; gap: 6px; }}
        .alert-note {{ border-color: #3b82f6; background-color: rgba(59, 130, 246, 0.1); }}
        .alert-note .alert-title {{ color: #60a5fa; }}
        .alert-warning {{ border-color: #f59e0b; background-color: rgba(245, 158, 11, 0.1); }}
        .alert-warning .alert-title {{ color: #fbbf24; }}
        .alert-tip {{ border-color: #10b981; background-color: rgba(16, 185, 129, 0.1); }}
        .alert-tip .alert-title {{ color: #34d399; }}
        .alert-important {{ border-color: #8b5cf6; background-color: rgba(139, 92, 246, 0.1); }}
        .alert-important .alert-title {{ color: #a78bfa; }}
        .alert-caution {{ border-color: #ef4444; background-color: rgba(239, 68, 68, 0.1); }}
        .alert-caution .alert-title {{ color: #f87171; }}
        table {{ width: 100%; border-collapse: collapse; margin: 20px 0; }}
        th, td {{ padding: 12px; border: 1px solid var(--border-color); text-align: left; }}
        th {{ background-color: var(--card-bg); color: #818cf8; }}
        tr:nth-child(even) {{ background-color: rgba(30, 41, 59, 0.5); }}
        hr {{ border: 0; border-top: 1px solid var(--border-color); margin: 40px 0; }}
    </style>
</head>
<body>
    <div class="container">
        <div id="content" class="tex2jax_process"></div>
    </div>
    <script>
        const rawMarkdown = {repr(md_content)};
        const mathBlocks = [];
        let placeholderCount = 0;
        let tempMarkdown = rawMarkdown;
        
        tempMarkdown = tempMarkdown.replace(/\\$\\$([\\s\\S]+?)\\$\\$/g, (match) => {{
            const placeholder = `@@MATH_BLOCK_${{placeholderCount}}@@`;
            mathBlocks.push({{ placeholder, content: match }});
            placeholderCount++;
            return placeholder;
        }});
        
        tempMarkdown = tempMarkdown.replace(/\\$([^$\\n]+?)\\$/g, (match) => {{
            const placeholder = `@@MATH_BLOCK_${{placeholderCount}}@@`;
            mathBlocks.push({{ placeholder, content: match }});
            placeholderCount++;
            return placeholder;
        }});
        
        let renderedHtml = marked.parse(tempMarkdown);
        
        for (const block of mathBlocks) {{
            renderedHtml = renderedHtml.replace(block.placeholder, () => block.content);
        }}
        
        const contentDiv = document.getElementById('content');
        contentDiv.innerHTML = renderedHtml;
        
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
        
        function triggerMathJax() {{
            if (window.MathJax && typeof window.MathJax.typeset === 'function') {{
                window.MathJax.typeset();
            }}
        }}
        triggerMathJax();
        const mjScript = document.getElementById('MathJax-script');
        if (mjScript) {{ mjScript.addEventListener('load', triggerMathJax); }}
    </script>
</body>
</html>
"""
    return html_content

def extract_youtube_video_id(url):
    pattern = r'(?:v=|\/embed\/|\/1/|\/v\/|https:\/\/youtu\.be\/|shorts\/)([a-zA-Z0-9_-]{11})'
    match = re.search(pattern, url)
    if match:
        return match.group(1)
    return None

def fetch_youtube_transcript_fallback(url):
    video_id = extract_youtube_video_id(url)
    if not video_id: return None
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        api = YouTubeTranscriptApi()
        transcript_list = api.list(video_id)
        languages = ['en', 'hi', 'es', 'fr', 'de', 'it', 'ja', 'ko', 'zh', 'pt', 'ru']
        try:
            transcript = transcript_list.find_transcript(languages)
        except Exception:
            transcript = next(iter(transcript_list))
        raw_data = transcript.fetch()
        formatted_lines = []
        for entry in raw_data:
            formatted_lines.append(f"[{format_time(entry.start)}] {entry.text}")
        return "\n".join(formatted_lines)
    except Exception:
        pass
    try:
        import urllib.request
        api_url = f"https://youtube-transcript.ai/transcript/{video_id}.txt"
        req = urllib.request.Request(api_url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=15) as response:
            content = response.read().decode('utf-8')
            if "## Transcript" in content or len(content) > 100:
                return content
    except Exception:
        pass
    return None

def chunk_transcript(transcript_text, chunk_duration_sec=600):
    lines = transcript_text.split('\n')
    chunks = []
    current_chunk_lines = []
    ts_pattern = re.compile(r'\[((\d{1,2}:)?\d{1,2}:\d{2})\]')
    chunk_start_sec = 0.0
    for line in lines:
        match = ts_pattern.search(line)
        if match:
            ts_str = match.group(1)
            sec = timestamp_to_seconds(ts_str)
            while sec >= chunk_start_sec + chunk_duration_sec:
                if current_chunk_lines:
                    chunks.append({
                        "start_sec": chunk_start_sec,
                        "end_sec": chunk_start_sec + chunk_duration_sec,
                        "text": "\n".join(current_chunk_lines)
                    })
                    current_chunk_lines = []
                chunk_start_sec += chunk_duration_sec
            current_chunk_lines.append(line)
        else:
            if current_chunk_lines or not chunks:
                current_chunk_lines.append(line)
    if current_chunk_lines:
        chunks.append({
            "start_sec": chunk_start_sec,
            "end_sec": chunk_start_sec + chunk_duration_sec,
            "text": "\n".join(current_chunk_lines)
        })
    return chunks

async def run_pipeline_transcript_only_async(youtube_url, transcript_text, job_id, chunk_duration_sec=600, rate_limit_delay=5):
    logger.info(f"[{job_id}] Starting transcript-only pipeline for {youtube_url}")
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise Exception(f"[{job_id}] GEMINI_API_KEY env variable is missing.")
    client = genai.Client(api_key=api_key)
    
    chunks = chunk_transcript(transcript_text, chunk_duration_sec=chunk_duration_sec)
    if not chunks:
        raise Exception(f"[{job_id}] No transcript chunks could be generated.")
        
    md_text = f"# Compiled Lecture Study Notes: {youtube_url}\n\n*Processed from YouTube transcript fallback (no local media downloads) in chunks.*\n\n---\n\n"
        
    system_instruction = (
        "You are an elite academic instructor and expert scribe. Your goal is to produce "
        "fully independent, self-contained, textbook-quality classroom study notes based on the provided transcript segment. "
        "The notes must be so detailed and clear that a student can read them, learn the topic, understand every concept, "
        "and replicate every mathematical derivation from scratch.\n\n"
        "Requirements:\n"
        "1. **Independent Readability**: Do not write high-level summaries or references to the transcript itself. Write full narrative "
        "explanations. Define every setup, variable, constant, and physical term explicitly.\n"
        "2. **Exhaustive Mathematics**: Replicate *every single mathematical step* discussed or implied in the transcript. "
        "Do not skip steps. Show starting formulas, algebraic rearrangements, and final expressions.\n"
        "3. **Capture Spoken Nuances**: Include the instructor's verbal examples, analogies, explanations of *why* steps are performed, "
        "and warnings about common student mistakes. Use callout boxes (e.g. '> [!WARNING] Common Mistake: ...' or '> [!NOTE] Explanation: ...') for emphasis.\n"
        "4. **Format**: Render all math, equations, physical parameters, and chemical symbols in LaTeX ($...$ for inline, $$...$$ for blocks). "
        "Structure sections cleanly with Markdown headers, bullet points, numbered lists, and comparison tables."
    )
    
    previous_context = ""
    model_name = "gemini-3.1-flash-lite"
    
    for i, chunk in enumerate(chunks, 1):
        start_time_str = format_time(chunk["start_sec"])
        end_time_str = format_time(chunk["end_sec"])
        
        prompt = (
            f"You are writing complete, independent study notes for the lecture segment from {start_time_str} to {end_time_str}.\n"
            f"Below is the chronological transcript of this lecture segment:\n\n"
            f"{chunk['text']}\n\n"
            "Compile the complete, detailed study notes for this segment following the system instructions."
        )
        
        if previous_context:
            prompt += (
                f"\n\nHere is the tail end of the previous segment's notes for reference. "
                "Ensure smooth transitions, continuous flow, and consistent mathematical notation:\n"
                f"...\n{previous_context}\n"
            )
            
        try:
            logger.info(f"[{job_id}] Requesting content generation from Gemini for segment {i} of {len(chunks)}")
            def _generate():
                return client.models.generate_content(
                    model=model_name,
                    contents=[prompt],
                    config=types.GenerateContentConfig(
                        system_instruction=system_instruction,
                        temperature=0.1
                    )
                )
            response = await asyncio.to_thread(_generate)
            
            cleaned_text = response.text if response.text else ""
            previous_context = cleaned_text[-2000:] if cleaned_text else ""
            
            md_text += f"## Segment: {start_time_str} - {end_time_str}\n\n{cleaned_text}\n\n---\n\n"
            logger.info(f"[{job_id}] Successfully generated notes for segment {i}")
        except Exception as e:
            logger.error(f"[{job_id}] API generation failed for segment {i}: {e}")
            
        if i < len(chunks):
            await asyncio.sleep(rate_limit_delay)
            
    html_text = compile_markdown_to_html_string(md_text)
    return md_text, html_text

async def run_capture_pipeline_async(job_id: str, workspace_dir: str, transcript_text: str, youtube_url: str):
    """
    Background multimodal pipeline for processing pre-captured Chrome extension keyframe images and transcript.
    """
    import main  # Circular reference protection
    
    logger.info(f"[{job_id}] Starting capture-based multimodal pipeline for {youtube_url}")
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise Exception(f"[{job_id}] GEMINI_API_KEY env variable is missing.")
    client = genai.Client(api_key=api_key)
    
    # Chunk the transcript
    chunk_duration_sec = 10 * 60
    rate_limit_delay = 5
    chunks = chunk_transcript(transcript_text, chunk_duration_sec=chunk_duration_sec)
    if not chunks:
        raise Exception(f"[{job_id}] No transcript chunks could be generated.")
        
    # Gather pre-captured keyframes from workspace_dir
    frame_info = []
    image_paths = sorted(glob.glob(os.path.join(workspace_dir, "frame_*.jpg")))
    for img_path in image_paths:
        # Match time_HH_MM_SS or time_MM_SS
        match = re.search(r'time_(\d{2})_(\d{2})_(\d{2})', img_path)
        if match:
            h, m, s = map(int, match.groups())
            sec = h * 3600 + m * 60 + s
            frame_info.append({"path": img_path, "time_sec": sec, "filename": os.path.basename(img_path)})
        else:
            match = re.search(r'time_(\d{2})_(\d{2})', img_path)
            if match:
                m, s = map(int, match.groups())
                sec = m * 60 + s
                frame_info.append({"path": img_path, "time_sec": sec, "filename": os.path.basename(img_path)})
                
    md_text = f"# Compiled Lecture Study Notes: {youtube_url}\n\n*Processed from browser-captured keyframes + transcript via Chrome Extension.*\n\n---\n\n"
    
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
        "Place the embedded image immediately before the explanation or derivation of that diagram.\n"
        "5. **Format**: Render all math, equations, physical parameters, and chemical symbols in LaTeX ($...$ for inline, $$...$$ for blocks). "
        "Structure sections cleanly with Markdown headers, bullet points, numbered lists, and comparison tables."
    )
    
    previous_context = ""
    model_name = "gemini-3.1-flash-lite"
    total_chunks = len(chunks)
    
    for i, chunk in enumerate(chunks, 1):
        start_time_str = format_time(chunk["start_sec"])
        end_time_str = format_time(chunk["end_sec"])
        
        percent = 30 + int(((i - 1) / total_chunks) * 60)
        await main.async_update_job_in_db(job_id, {"progress": percent, "message": f"Compiling study notes from capture: segment {i} of {total_chunks}..."})
        
        # Filter keyframes for this segment
        segment_frames = [f for f in frame_info if chunk["start_sec"] <= f["time_sec"] <= chunk["end_sec"]]
        
        contents = []
        for frame in segment_frames:
            try:
                def _open_img():
                    return Image.open(frame["path"])
                img = await asyncio.to_thread(_open_img)
                contents.append(img)
            except Exception as e:
                logger.warning(f"[{job_id}] Failed to load keyframe {frame['path']}: {e}")
                
        prompt = (
            f"You are writing complete, independent study notes for the lecture segment from {start_time_str} to {end_time_str}.\n"
            f"You have been provided with visual keyframe images representing visual checkpoints in this range "
            f"(each filename indicates its exact timestamp). "
            f"And the matching text transcript below:\n\n{chunk['text']}\n\n"
            "Align the visual images with the spoken transcript chronologically to write complete, textbook-style notes."
        )
        
        if segment_frames:
            prompt += "\n\nThe following keyframe images are attached in your input contents in chronological order. If you choose to embed any of them, you MUST use their exact filename from this list:\n"
            for frame in segment_frames:
                prompt += f"- {frame['filename']}\n"
                
        if previous_context:
            prompt += (
                f"\n\nHere is the tail end of the previous segment's notes for reference. "
                "Ensure smooth transitions, continuous flow, and consistent mathematical notation:\n"
                f"...\n{previous_context}\n"
            )
            
        contents.append(prompt)
        
        try:
            logger.info(f"[{job_id}] Requesting content generation for capture segment {i} of {total_chunks}")
            def _generate():
                return client.models.generate_content(
                    model=model_name,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        system_instruction=system_instruction,
                        temperature=0.1
                    )
                )
            response = await asyncio.to_thread(_generate)
            cleaned_text = response.text if response.text else ""
            previous_context = cleaned_text[-2000:] if cleaned_text else ""
            
            # Upload embedded keyframe images to Supabase Storage and rewrite paths
            image_links = re.findall(r'!\[(.*?)]\(((?:.*?/)?(frame_\d{3}_time_.*?\.jpg))\)', cleaned_text)
            if image_links:
                for alt_text, full_path_in_link, filename in image_links:
                    src_path = os.path.join(workspace_dir, filename)
                    if os.path.exists(src_path):
                        unique_filename = f"{job_id}_{filename}"
                        with open(src_path, "rb") as f:
                            file_bytes = f.read()
                            
                        def _upload_sb():
                            try:
                                main.supabase.storage.from_("lecture_media").upload(
                                    file=file_bytes, 
                                    path=unique_filename,
                                    file_options={"content-type": "image/jpeg"}
                                )
                            except Exception as ex:
                                if "Duplicate" in str(ex):
                                    pass # ignore if already uploaded
                                else:
                                    logger.warning(f"[{job_id}] Failed to upload image {filename} to Supabase: {ex}")
                        await asyncio.to_thread(_upload_sb)
                        
                        def _get_url():
                            return main.supabase.storage.from_("lecture_media").get_public_url(unique_filename)
                        public_url = await asyncio.to_thread(_get_url)
                        
                        cleaned_text = cleaned_text.replace(f"({full_path_in_link})", f"({public_url})")
            
            md_text += f"## Segment: {start_time_str} - {end_time_str}\n\n{cleaned_text}\n\n---\n\n"
            logger.info(f"[{job_id}] Successfully generated notes for capture segment {i}")
        except Exception as e:
            logger.error(f"[{job_id}] API generation failed for capture segment {i}: {e}")
            
        if i < total_chunks:
            await asyncio.sleep(rate_limit_delay)
            
    html_text = compile_markdown_to_html_string(md_text)
    return md_text, html_text

if __name__ == "__main__":
    pass
