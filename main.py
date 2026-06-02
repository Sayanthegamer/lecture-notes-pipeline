import os
import uuid
import sys
from fastapi import FastAPI, BackgroundTasks, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import pipeline

app = FastAPI(title="Lecture Notes Scribe API")

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

# In-memory dictionary to track job statuses
jobs = {}

class JobRequest(BaseModel):
    url: str

def run_pipeline_task(job_id: str, url: str, base_url: str):
    jobs[job_id] = {
        "status": "processing",
        "progress": 10,
        "message": "Downloading video from YouTube...",
        "markdown": None,
        "html": None,
    }
    
    try:
        # 1. Download YouTube video
        vid_file = pipeline.download_youtube_video(url)
        if not vid_file:
            raise Exception("YouTube video download failed.")
            
        jobs[job_id]["progress"] = 30
        jobs[job_id]["message"] = "Extracting keyframes and audio slices..."
        
        # Output filenames
        output_md = f"notes_{job_id}.md"
        output_html = f"notes_{job_id}.html"
        
        # 2. Run the main processing pipeline
        pipeline.run_pipeline(vid_file, output_md, threshold=0.10, cooldown_seconds=30)
        
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
        # Converts "./notes_media/frame_xxx.jpg" to "https://your-app.onrender.com/notes_media/frame_xxx.jpg"
        backend_media_url = f"{base_url}notes_media/"
        if md_text:
            md_text = md_text.replace("./notes_media/", backend_media_url)
        if html_text:
            html_text = html_text.replace("./notes_media/", backend_media_url)
            
        jobs[job_id] = {
            "status": "completed",
            "progress": 100,
            "message": "Notes successfully compiled!",
            "markdown": md_text,
            "html": html_text,
        }
        
        # Clean up temporary video file (images in notes_media persist to be served statically)
        if os.path.exists(vid_file):
            try: os.remove(vid_file)
            except: pass
        if os.path.exists(output_md):
            try: os.remove(output_md)
            except: pass
        if os.path.exists(output_html):
            try: os.remove(output_html)
            except: pass
            
    except Exception as e:
        jobs[job_id] = {
            "status": "failed",
            "progress": 100,
            "message": f"Error: {str(e)}",
            "markdown": None,
            "html": None,
        }

@app.post("/api/generate")
def generate_notes(request: JobRequest, background_tasks: BackgroundTasks, fastapi_req: Request):
    job_id = str(uuid.uuid4())
    # Capture the Render base URL dynamically
    base_url = str(fastapi_req.base_url)
    
    # Start the pipeline as a background thread task
    background_tasks.add_task(run_pipeline_task, job_id, request.url, base_url)
    
    return {"job_id": job_id}

@app.get("/api/status/{job_id}")
def get_status(job_id: str):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    return jobs[job_id]

# Bind to the PORT environment variable provided by Render
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
