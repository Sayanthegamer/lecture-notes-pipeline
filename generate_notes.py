import os
import glob
from google import genai
from google.genai import types
from PIL import Image
from dotenv import load_dotenv

# Load API key from a .env file if present
load_dotenv()

# Initialize the Gemini Client
# It will automatically look for the GEMINI_API_KEY environment variable.
# Alternatively, you can pass it directly: client = genai.Client(api_key="YOUR_KEY")
api_key = os.getenv("GEMINI_API_KEY")
if not api_key:
    print("Warning: GEMINI_API_KEY not found in environment or .env file.")
    print("Please set GEMINI_API_KEY or edit this script to pass the API key directly.")
    
client = genai.Client(api_key=api_key)

def compile_notes(image_dir="./extracted_notes_frames", output_file="lecture_notes.md", max_images=10):
    """
    Reads extracted keyframe images, sends them to Gemini,
    and saves the generated Markdown study notes.
    """
    # Find all JPEG frames in the output directory and sort them chronologically
    image_paths = sorted(glob.glob(os.path.join(image_dir, "frame_*.jpg")))
    
    if not image_paths:
        print(f"No keyframe images found in {image_dir}.")
        return

    print(f"Found {len(image_paths)} total keyframes.")
    
    # For testing, we default to a subset of frames to conserve tokens/rate limits.
    # You can change max_images or slice differently to process the entire lecture.
    test_images = image_paths[:max_images]
    print(f"Sending first {len(test_images)} frames to Gemini for compilation...")

    # Load PIL Images for Gemini SDK
    contents = []
    for path in test_images:
        try:
            img = Image.open(path)
            contents.append(img)
            print(f" Loaded: {os.path.basename(path)}")
        except Exception as e:
            print(f"Error loading {path}: {e}")

    if not contents:
        print("No valid images could be loaded.")
        return

    # Add the prompt instructions
    system_instruction = (
        "You are an elite academic instructor. Analyze these sequential lecture board captures. "
        "Convert the handwritten board work, slides, and diagrams into pristine, well-structured study notes in Markdown. "
        "Render all mathematical equations and chemical formulas using LaTeX notation ($...$ for inline, $$...$$ for block formulas). "
        "Ignore the instructor's physical body or any people; focus entirely on the academic content. "
        "Synthesize the visual progression chronologically into clean, readable textbook-style chapters with clear headings."
    )
    
    prompt = (
        "Here are the sequential keyframes captured from a lecture video. "
        "Please compile them into comprehensive study notes matching the instructions."
    )
    
    contents.append(prompt)

    try:
        print("Contacting Gemini API (using gemini-3.1-flash-lite)...")
        response = client.models.generate_content(
            model='gemini-3.1-flash-lite',
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                temperature=0.2,
            )
        )
        
        # Save the result to a markdown file
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(response.text)
            
        print(f"\nSuccess! Notes successfully compiled and saved to: {output_file}")
        
    except Exception as e:
        print(f"API Error: {e}")

if __name__ == "__main__":
    # Compile notes using the first 10 frames as a test.
    # Increase max_images to process more of the video.
    compile_notes(image_dir="./extracted_notes_frames", output_file="lecture_notes.md", max_images=10)
