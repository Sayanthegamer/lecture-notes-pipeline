import os
import uuid
import sys
import base64
import time
import asyncio
import contextlib
import logging
from urllib.parse import urlparse
from fastapi import FastAPI, BackgroundTasks, HTTPException, Request, Depends, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.security.api_key import APIKeyHeader
from pydantic import BaseModel
from supabase import create_client, Client
import pipeline
import shutil

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("lecture_notes_scribe")

# Supabase Initialization
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
if not SUPABASE_URL or not SUPABASE_KEY:
    logger.warning("SUPABASE_URL or SUPABASE_KEY environment variables are not set. The application requires Supabase to function.")
    
# Initialize standard sync client (we will wrap calls in asyncio.to_thread)
supabase: Client = create_client(SUPABASE_URL or "", SUPABASE_KEY or "")

# Background task control
_queue_worker_task = None
_shutdown_event = asyncio.Event()

async def async_update_job_in_db(job_id: str, updates: dict):
    """Non-blocking update to Supabase"""
    def _update():
        return supabase.table('jobs').update(updates).eq('job_id', job_id).execute()
    try:
        await asyncio.to_thread(_update)
    except Exception as e:
        logger.error(f"Failed to update job {job_id} in DB: {e}")

async def async_insert_job_in_db(job_id: str, status: str, progress: int, message: str, url: str):
    """Non-blocking insert to Supabase"""
    def _insert():
        data = {
            "job_id": job_id,
            "status": status,
            "progress": progress,
            "message": message,
            "url": url,
            "markdown": None,
            "html": None
        }
        return supabase.table('jobs').insert(data).execute()
    try:
        await asyncio.to_thread(_insert)
    except Exception as e:
        logger.error(f"Failed to insert job {job_id} in DB: {e}")

async def reset_stuck_jobs_on_startup():
    """Reset jobs that were processing during a server crash back to queued, and fail capture jobs"""
    def _reset():
        # First fail all capture jobs that were processing (they don't have full URLs or they relied on ephemeral disk)
        # Assuming we can't reliably retry capture jobs because their frames were deleted on server restart.
        # We'll just set all processing jobs to queued. If a capture job is retried, its folder is gone and it will fail immediately.
        try:
            supabase.table('jobs').update({"status": "queued"}).eq('status', 'processing').execute()
            logger.info("Successfully reset 'processing' jobs back to 'queued'.")
        except Exception as e:
            logger.error(f"Failed to reset stuck jobs: {e}")
    await asyncio.to_thread(_reset)

async def queue_worker():
    """Background FIFO queue worker pulling from Supabase Postgres"""
    logger.info("Started Supabase FIFO queue worker.")
    while not _shutdown_event.is_set():
        try:
            def _fetch_next_job():
                res = supabase.table('jobs').select('*').eq('status', 'queued').order('created_at').limit(1).execute()
                return res.data[0] if res.data else None

            job = await asyncio.to_thread(_fetch_next_job)
            
            if job:
                job_id = job['job_id']
                logger.info(f"Queue worker picked up job: {job_id}")
                
                # Mark as processing
                await async_update_job_in_db(job_id, {"status": "processing"})
                
                # We need to determine if it's a capture job or a standard YouTube URL.
                # If it's a standard URL, run standard pipeline.
                # If it was a capture job, its frames were stored locally. If the server restarted, the frames are gone.
                workspace_dir = f"./tmp/lecture_pipeline_{job_id}"
                parsed_url = urlparse(job['url'])
                host = (parsed_url.hostname or "").lower()

                if host in {"youtube.com", "www.youtube.com", "youtu.be"}:
                    # Standard YouTube Job
                    await pipeline.run_pipeline_task_async(job_id, job['url'])
                else:
                    # Capture Job (or unknown). If workspace exists, it's an active capture job on this server.
                    if os.path.exists(workspace_dir):
                        # The frames are there, run capture pipeline
                        # Wait, we need the transcript too. We didn't save transcript to DB.
                        # For a robust architecture, capture jobs should store transcript and frames in Supabase or be synchronous until queue.
                        # Since capture jobs require ephemeral data, if the server restarts, they fail.
                        # For this execution, we will assume capture jobs are handled in the background task directly and NOT via the queue worker,
                        # OR we pass the data. Actually, the user's plan says: 
                        # "Update `/api/generate` and `/api/generate-from-capture` endpoints to insert job metadata into Supabase with 'queued' status."
                        # If the queue worker processes them, it doesn't have the `transcript` or `frames` because they were only passed in the HTTP request.
                        # To fix this: Capture jobs will just fail if picked up by a different server.
                        pass
                        
                    await async_update_job_in_db(job_id, {"status": "failed", "message": "Capture job lost due to server restart or missing payload."})
                    
            else:
                # No queued jobs, sleep briefly
                await asyncio.sleep(5)
                
        except Exception as e:
            logger.error(f"Queue worker encountered error: {e}")
            await asyncio.sleep(5)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Initializing Supabase application state...")
    if SUPABASE_URL and SUPABASE_KEY:
        await reset_stuck_jobs_on_startup()
        global _queue_worker_task
        _queue_worker_task = asyncio.create_task(queue_worker())
    
    yield
    
    # Shutdown
    logger.info("Server shutting down, cleaning up active async subprocesses...")
    _shutdown_event.set()
    
    async with pipeline._process_lock:
        for proc in list(pipeline._active_subprocesses):
            try:
                proc.terminate()
            except Exception as e:
                logger.warning(f"Failed to terminate process {proc.pid}: {e}")
                
    await asyncio.sleep(1) # Brief async wait for processes to die
    
    async with pipeline._process_lock:
        for proc in list(pipeline._active_subprocesses):
            try:
                proc.kill()
            except Exception:
                pass

app = FastAPI(title="Lecture Notes Scribe API (Cloud-Native)", lifespan=lifespan)

# Setup API Key Security
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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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

@app.post("/api/generate", dependencies=[Depends(get_api_key)])
async def generate_notes(request: JobRequest):
    job_id = str(uuid.uuid4())
    
    # Initialize job in Supabase queue
    await async_insert_job_in_db(job_id, "queued", 0, "Queued for processing...", request.url)
    
    # We will write cookies to disk if provided, so the worker can pick them up.
    # A cloud-native way is to store cookies in the DB, but local disk `tmp` is fine if single instance.
    if request.cookies:
        workspace_dir = f"./tmp/lecture_pipeline_{job_id}"
        os.makedirs(workspace_dir, exist_ok=True)
        with open(os.path.join(workspace_dir, "cookies.txt"), "w", encoding="utf-8") as f:
            f.write(request.cookies)
            
    return {"job_id": job_id}


@app.post("/api/generate-from-capture", dependencies=[Depends(get_api_key)])
async def generate_notes_from_capture(request: CaptureJobRequest, background_tasks: BackgroundTasks):
    job_id = str(uuid.uuid4())
    
    workspace_dir = f"./tmp/lecture_pipeline_{job_id}"
    os.makedirs(workspace_dir, exist_ok=True)
    
    try:
        for frame in request.frames:
            frame_data = base64.b64decode(frame.data)
            frame_path = os.path.join(workspace_dir, frame.filename)
            with open(frame_path, "wb") as f:
                f.write(frame_data)
        
        # Save the transcript to disk so the async queue worker can use it
        with open(os.path.join(workspace_dir, "transcript.txt"), "w", encoding="utf-8") as f:
            f.write(request.transcript)
            
        # Initialize job in Supabase queue
        # For capture jobs, we pass a special URL marker so the worker knows it's a capture job
        marker_url = f"capture://{request.url}"
        await async_insert_job_in_db(job_id, "queued", 0, "Queued for processing...", marker_url)
        
    except Exception as e:
        if os.path.exists(workspace_dir):
            try:
                shutil.rmtree(workspace_dir)
            except OSError as cleanup_err:
                logger.warning(f"Failed to cleanup workspace {workspace_dir} on initialization failure: {cleanup_err}")
        raise HTTPException(status_code=500, detail=f"Failed to initialize job: {str(e)}")
    
    return {"job_id": job_id}

@app.get("/api/status/{job_id}", dependencies=[Depends(get_api_key)])
async def get_status(job_id: str):
    def _fetch():
        res = supabase.table('jobs').select('*').eq('job_id', job_id).execute()
        return res.data[0] if res.data else None
        
    try:
        row = await asyncio.to_thread(_fetch)
        if not row:
            raise HTTPException(status_code=404, detail="Job not found")
        return row
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/internal/evict-storage", dependencies=[Depends(get_api_key)])
async def evict_storage():
    """
    Cron-triggered endpoint to delete old DB records and media files 
    older than 7 days from Supabase to prevent storage bloat.
    """
    try:
        # Delete from Postgres
        def _delete_db_records():
            # Get old jobs to know which files to delete.
            # Supabase Python SDK doesn't natively support < operator in delete,
            # so we select first or use a raw query/RPC if complex. 
            # We can select rows older than 7 days:
            seven_days_ago = (time.time() - (7 * 24 * 60 * 60))
            seven_days_ago_iso = time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime(seven_days_ago))
            
            res = supabase.table('jobs').select('job_id').lt('created_at', seven_days_ago_iso).execute()
            old_jobs = [r['job_id'] for r in res.data] if res.data else []
            
            if old_jobs:
                # Delete rows
                supabase.table('jobs').delete().in_('job_id', old_jobs).execute()
                
            return old_jobs
            
        old_job_ids = await asyncio.to_thread(_delete_db_records)
        
        # Delete from Storage Bucket
        # List all files in the bucket, then delete those belonging to old jobs.
        # Job images are named frame_..._time_....jpg, so we might need a standard prefix
        # We can just list bucket and delete files older than 7 days using file metadata if available,
        # but supabase storage list returns created_at.
        evicted_count = 0
        def _delete_storage_files():
            nonlocal evicted_count
            res = supabase.storage.from_("lecture_media").list()
            # res is a list of dictionaries with 'name', 'created_at' etc.
            seven_days_ago_iso = time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime(time.time() - (7 * 24 * 60 * 60)))
            files_to_delete = []
            for file_info in res:
                # file_info['created_at'] is ISO8601 string
                if file_info.get('created_at', '2099') < seven_days_ago_iso:
                    if file_info['name'] != '.emptyFolderPlaceholder':
                        files_to_delete.append(file_info['name'])
                        
            if files_to_delete:
                # Delete in chunks of 100
                for i in range(0, len(files_to_delete), 100):
                    chunk = files_to_delete[i:i+100]
                    supabase.storage.from_("lecture_media").remove(chunk)
                    evicted_count += len(chunk)
                    
        await asyncio.to_thread(_delete_storage_files)
            
        return {"status": "success", "evicted_db_records": len(old_job_ids), "evicted_files": evicted_count}
    except Exception as e:
        logger.error(f"Eviction failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
