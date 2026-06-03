import os
import uuid
import sys
import base64
import sqlite3
import datetime
import contextlib
import logging
from fastapi import FastAPI, BackgroundTasks, HTTPException, Request, Depends, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.security.api_key import APIKeyHeader
from pydantic import BaseModel
import pipeline
import shutil
import time

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("lecture_notes_scribe")

@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: nothing needed
    yield
    # Shutdown: Clean up any active ffmpeg/yt-dlp subprocesses
    logger.info("Server shutting down, cleaning up active subprocesses...")
    with pipeline._process_lock:
        for proc in list(pipeline._active_subprocesses):
            try:
                proc.terminate()
            except Exception as e:
                logger.warning(f"Failed to terminate process {proc.pid}: {e}")
                
    time.sleep(1) # Brief wait for processes to die
    
    with pipeline._process_lock:
        for proc in list(pipeline._active_subprocesses):
            try:
                proc.kill()
            except Exception:
                pass

app = FastAPI(title="Lecture Notes Scribe API", lifespan=lifespan)

# Setup API Key Security
# NOTE: The same API_KEY must be configured identically on the client (popup.js) and server in production.
API_KEY = os.getenv("API_KEY", "REQUIRE_ENV_API_KEY")
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=True)

def get_api_key(api_key_header: str = Security(api_key_header)):
    if API_KEY == "REQUIRE_ENV_API_KEY":
        raise HTTPException(
            status_code=500,
            detail="API_KEY environment variable is not configured on the server."
        )
    if api_key_header == API_KEY:
        return api_key_header
    raise HTTPException(status_code=403, detail="Could not validate API KEY")

# Enable CORS so the Vercel frontend can make API calls to Render
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Ensure notes_media directory exists and mount it to serve images statically
os.makedirs("notes_media", exist_ok=True)
app.mount("/notes_media", StaticFiles(directory="notes_media"), name="notes_media")

# Setup SQLite Database
@contextlib.contextmanager
def get_db_connection():
    conn = sqlite3.connect("jobs.db", timeout=30.0, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

def init_db():
    with get_db_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                status TEXT,
                progress INTEGER,
                message TEXT,
                markdown TEXT,
                html TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()

init_db()

class JobRequest(BaseModel):
    url: str
    cookies: str | None = None

class CaptureFrame(BaseModel):
    timestamp: str
    filename: str
    data: str  # base64 JPEG data

class CaptureJobRequest(BaseModel):
    url: str
    transcript: str
    frames: list[CaptureFrame] = []

def update_job_in_db(
    job_id: str,
    status: str | None = None,
    progress: int | None = None,
    message: str | None = None,
    markdown: str | None = None,
    html: str | None = None
):
    with get_db_connection() as conn:
        conn.execute(
            """
            UPDATE jobs
            SET status = COALESCE(?, status),
                progress = COALESCE(?, progress),
                message = COALESCE(?, message),
                markdown = COALESCE(?, markdown),
                html = COALESCE(?, html)
            WHERE job_id = ?
            """,
            (status, progress, message, markdown, html, job_id)
        )
        conn.commit()

def run_pipeline_task(job_id: str, url: str, base_url: str, cookies_text: str = None):
    def update_progress(progress_val, message_text):
        update_job_in_db(job_id, progress=progress_val, message=message_text)

    # Scoped Workspace
    workspace_dir = f"./tmp/lecture_pipeline_{job_id}"
    os.makedirs(workspace_dir, exist_ok=True)
    
    cookies_file = os.path.join(workspace_dir, "cookies.txt") if cookies_text else None
    
    try:
        # Write cookies if provided
        if cookies_file and cookies_text:
            with open(cookies_file, "w", encoding="utf-8") as f:
                f.write(cookies_text)
                
        # Output filenames
        output_md = os.path.join(workspace_dir, f"notes_{job_id}.md")
        output_html = os.path.join(workspace_dir, f"notes_{job_id}.html")

        # 1. Download YouTube video
        vid_file = pipeline.download_youtube_video(url, job_id, workspace_dir, cookies_file=cookies_file)
        
        if vid_file:
            update_progress(30, "Extracting keyframes and audio slices...")
            # 2. Run the main processing pipeline (Multimodal)
            pipeline.run_pipeline(vid_file, job_id, workspace_dir, output_md, threshold=0.10, cooldown_seconds=30, progress_callback=update_progress)
        else:
            update_progress(25, "Download blocked. Fetching transcript directly from YouTube...")
            
            # Fallback: Retrieve transcript text only
            transcript_text = pipeline.fetch_youtube_transcript_fallback(url)
            if not transcript_text:
                raise Exception("YouTube download failed and no transcript could be retrieved.")
                
            update_progress(50, "Compiling textbook study notes from transcript...")
            
            # 2. Run the transcript-only processing pipeline
            pipeline.run_pipeline_transcript_only(url, transcript_text, output_md, progress_callback=update_progress)
            
        # 3. Read generated output files
        if os.path.exists(output_md):
            with open(output_md, "r", encoding="utf-8") as f:
                md_text = f.read()
        else:
            raise Exception("Notes generation failed (Markdown not found).")
            
        if os.path.exists(output_html):
            with open(output_html, "r", encoding="utf-8") as f:
                html_text = f.read()
        else:
            html_text = ""
            
        # 4. Rewrite relative image paths to point to Render's absolute static url
        backend_media_url = f"{base_url}notes_media/"
        if md_text:
            md_text = md_text.replace("./notes_media/", backend_media_url)
        if html_text:
            html_text = html_text.replace("./notes_media/", backend_media_url)
            
        update_job_in_db(job_id, status="completed", progress=100, message="Notes successfully compiled!", markdown=md_text, html=html_text)
            
    except Exception as e:
        update_job_in_db(job_id, status="failed", progress=100, message=f"Error: {str(e)}")
    finally:
        # Final cleanup: Delete entire workspace to prevent race conditions and disk bloat.
        if os.path.exists(workspace_dir):
            try:
                shutil.rmtree(workspace_dir)
            except OSError as e:
                logger.warning(f"Failed to cleanup workspace {workspace_dir}: {e}")

def run_capture_pipeline_task(job_id: str, url: str, base_url: str, transcript: str, workspace_dir: str):
    def update_progress(progress_val, message_text):
        update_job_in_db(job_id, progress=progress_val, message=message_text)

    try:
        output_md = os.path.join(workspace_dir, f"notes_{job_id}.md")
        output_html = os.path.join(workspace_dir, f"notes_{job_id}.html")
        
        update_progress(30, "Compiling multimodal study notes with Gemini...")
        
        # Run the capture-based multimodal pipeline
        success = pipeline.run_pipeline_from_capture(url, transcript, workspace_dir, output_md, progress_callback=update_progress)
        
        if not success:
            raise Exception("Multimodal pipeline processing failed.")
        
        # Read generated output files
        if os.path.exists(output_md):
            with open(output_md, "r", encoding="utf-8") as f:
                md_text = f.read()
        else:
            raise Exception("Notes generation failed (Markdown not found).")
            
        if os.path.exists(output_html):
            with open(output_html, "r", encoding="utf-8") as f:
                html_text = f.read()
        else:
            html_text = ""
            
        # Rewrite relative image paths to point to Render's absolute static url
        backend_media_url = f"{base_url}notes_media/"
        if md_text:
            md_text = md_text.replace("./notes_media/", backend_media_url)
        if html_text:
            html_text = html_text.replace("./notes_media/", backend_media_url)
            
        update_job_in_db(job_id, status="completed", progress=100, message="Notes successfully compiled from browser capture!", markdown=md_text, html=html_text)
            
    except Exception as e:
        update_job_in_db(job_id, status="failed", progress=100, message=f"Error: {str(e)}")
    finally:
        # Clean up the temporary workspace
        if os.path.exists(workspace_dir):
            try:
                shutil.rmtree(workspace_dir)
            except OSError as e:
                logger.warning(f"Failed to cleanup workspace {workspace_dir}: {e}")


@app.post("/api/generate", dependencies=[Depends(get_api_key)])
def generate_notes(request: JobRequest, background_tasks: BackgroundTasks, fastapi_req: Request):
    job_id = str(uuid.uuid4())
    base_url = str(fastapi_req.base_url)
    
    # Initialize job in DB
    with get_db_connection() as conn:
        conn.execute(
            "INSERT INTO jobs (job_id, status, progress, message) VALUES (?, ?, ?, ?)",
            (job_id, "processing", 10, "Downloading video from YouTube...")
        )
        conn.commit()
    
    background_tasks.add_task(run_pipeline_task, job_id, request.url, base_url, request.cookies)
    
    return {"job_id": job_id}


@app.post("/api/generate-from-capture", dependencies=[Depends(get_api_key)])
def generate_notes_from_capture(request: CaptureJobRequest, background_tasks: BackgroundTasks, fastapi_req: Request):
    job_id = str(uuid.uuid4())
    base_url = str(fastapi_req.base_url)
    
    workspace_dir = f"./tmp/lecture_pipeline_{job_id}"
    os.makedirs(workspace_dir, exist_ok=True)
    
    try:
        for frame in request.frames:
            frame_data = base64.b64decode(frame.data)
            frame_path = os.path.join(workspace_dir, frame.filename)
            with open(frame_path, "wb") as f:
                f.write(frame_data)
        
        frame_count = len([f for f in os.listdir(workspace_dir) if f.endswith('.jpg')])
        logger.info(f"[Capture] Received {frame_count} keyframes + transcript for {request.url}")
        
        # Initialize job in DB
        with get_db_connection() as conn:
            conn.execute(
                "INSERT INTO jobs (job_id, status, progress, message) VALUES (?, ?, ?, ?)",
                (job_id, "processing", 20, "Processing captured keyframes and transcript...")
            )
            conn.commit()
    except Exception as e:
        if os.path.exists(workspace_dir):
            try:
                shutil.rmtree(workspace_dir)
            except OSError as cleanup_err:
                logger.warning(f"Failed to cleanup workspace {workspace_dir} on initialization failure: {cleanup_err}")
        raise HTTPException(status_code=500, detail=f"Failed to initialize job: {str(e)}")
    
    background_tasks.add_task(
        run_capture_pipeline_task, job_id, request.url, base_url, request.transcript, workspace_dir
    )
    
    return {"job_id": job_id}

@app.get("/api/status/{job_id}", dependencies=[Depends(get_api_key)])
def get_status(job_id: str):
    with get_db_connection() as conn:
        cursor = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Job not found")
        return dict(row)

@app.post("/api/internal/evict-storage", dependencies=[Depends(get_api_key)])
def evict_storage():
    """
    Cron-triggered endpoint to delete old DB records and media files 
    older than 7 days to prevent storage bloat.
    """
    try:
        with get_db_connection() as conn:
            conn.execute("DELETE FROM jobs WHERE created_at < datetime('now', '-7 days')")
            conn.commit()
            
        evicted_count = 0
        media_dir = "notes_media"
        now = time.time()
        seven_days_ago = now - (7 * 24 * 60 * 60)
        
        if os.path.exists(media_dir):
            for filename in os.listdir(media_dir):
                filepath = os.path.join(media_dir, filename)
                if os.path.isfile(filepath):
                    if os.path.getmtime(filepath) < seven_days_ago:
                        try:
                            os.remove(filepath)
                            evicted_count += 1
                        except OSError as e:
                            logger.warning(f"Failed to evict {filepath}: {e}")
                            
        return {"status": "success", "evicted_files": evicted_count}
    except Exception as e:
        logger.error(f"Eviction failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
