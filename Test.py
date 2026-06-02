import cv2
import os
import time
from PIL import Image

def extract_lecture_keyframes(video_path, output_dir, threshold=0.10, cooldown_seconds=30):
    """
    Parses a video file and saves frames only when a meaningful visual 
    shift occurs (e.g., slide changes or board erasures) using edge detection
    and a temporal cooldown.
    """
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
        
    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS)
    if not fps or fps <= 0:
        fps = 30.0  # Fallback FPS if reading metadata fails
        
    hop_frames = int(fps * 2) # Sample/check every 2 seconds
    
    success, last_frame = cap.read()
    if not success:
        print("Error: Cannot read video.")
        return

    # Convert to grayscale, blur, and extract edges
    last_gray = cv2.cvtColor(last_frame, cv2.COLOR_BGR2GRAY)
    last_gray = cv2.GaussianBlur(last_gray, (9, 9), 0)
    last_edges = cv2.Canny(last_gray, 50, 150)
    
    saved_count = 0
    cv2.imwrite(os.path.join(output_dir, f"frame_{saved_count:03d}.jpg"), last_frame)
    saved_count += 1
    last_saved_time_sec = 0.0

    while cap.isOpened():
        frame_id = cap.get(cv2.CAP_PROP_POS_FRAMES)
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_id + hop_frames)
        
        success, frame = cap.read()
        if not success:
            break
            
        current_time_sec = frame_id / fps
        
        # Cooldown check: Skip comparison if we recently saved a frame
        if current_time_sec - last_saved_time_sec < cooldown_seconds:
            continue

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (9, 9), 0)
        edges = cv2.Canny(gray, 50, 150)
        
        # Calculate structural change in edges
        edge_delta = cv2.absdiff(last_edges, edges)
        non_zero_ratio = cv2.countNonZero(edge_delta) / float(gray.shape[0] * gray.shape[1])
        
        if non_zero_ratio > threshold:
            timestamp = time.strftime('%H:%M:%S', time.gmtime(current_time_sec))
            filename = os.path.join(output_dir, f"frame_{saved_count:03d}_time_{timestamp.replace(':', '_')}.jpg")
            cv2.imwrite(filename, frame)
            
            last_edges = edges
            last_saved_time_sec = current_time_sec
            saved_count += 1
            print(f"Significant board/slide change detected at {timestamp}. Saved keyframe.")

    cap.release()
    print(f"Extraction complete. Total keyframes saved: {saved_count}")

if __name__ == "__main__":
    # Test with a brief 5-10 minute clip first
    extract_lecture_keyframes("sample_lecture.mp4", "./extracted_notes_frames")