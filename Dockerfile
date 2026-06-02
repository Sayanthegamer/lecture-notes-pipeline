# Use an official Python runtime as a parent image
FROM python:3.11-slim

# Install system dependencies (FFmpeg is required for frame extraction and audio slicing)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file into the container
COPY requirements.txt .

# Install Python packages
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code
COPY pipeline.py .
COPY main.py .
COPY frontend ./frontend

# Expose port 10000 (Render uses the $PORT env variable, which we will bind to in main.py)
EXPOSE 10000

# Start FastAPI server
CMD ["python", "main.py"]
