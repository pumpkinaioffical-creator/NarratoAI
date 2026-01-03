import asyncio
import json
import os
import edge_tts
from render_engine import process_render

INPUT_JSON = "1.json"
INPUT_VIDEO = "1.mkv"
RESOLUTION = "native"  # 默认原始分辨率

async def generate_audio(text, output_file):
    communicate = edge_tts.Communicate(text, "zh-CN-YunxiNeural")
    await communicate.save(output_file)

async def main():
    if not os.path.exists(INPUT_JSON):
        print(f"Error: {INPUT_JSON} not found.")
        return
    if not os.path.exists(INPUT_VIDEO):
        print(f"Error: {INPUT_VIDEO} not found.")
        return

    # 1. Read 1.json
    with open(INPUT_JSON, 'r', encoding='utf-8') as f:
        data = json.load(f)

    # 2. Adapt to app.py schema
    # Recursively flatten data if nested (user may have deeply nested arrays)
    def flatten(items):
        result = []
        for item in items:
            if isinstance(item, list):
                result.extend(flatten(item))
            elif isinstance(item, dict):
                result.append(item)
        return result
    
    data = flatten(data)

    # app.py expects: { 'time_start': 'MM:SS', 'time_end': 'MM:SS', 'voiceover': '...' }
    # 1.json has: { 'voiceover': '...', 'fragments': [ { 'start': 'MM:SS', 'end': 'MM:SS' } ] }
    
    script_data = []
    audio_files = {}

    print("Generating audio and preparing script data...")
    
    # Create temp dir for audio if not exists
    if not os.path.exists("temp_audio"):
        os.makedirs("temp_audio")

    for idx, item in enumerate(data):
        voice_text = item.get("voiceover", "")
        fragments = item.get("fragments", [])
        
        if not fragments:
            print(f"Skipping item {idx}: No fragments.")
            continue
            
        # Pass all fragments to app.py for multi-clip support
        scene = {
            "fragments": fragments,  # Pass all fragments
            "voiceover": voice_text
        }
        script_data.append(scene)
        
        
        # Generator Audio
        # Use prefix based on JSON filename to avoid collision between 1.json and 2.json
        json_prefix = os.path.basename(INPUT_JSON).split('.')[0]
        audio_filename = os.path.abspath(f"temp_audio/tts_{json_prefix}_{idx}.mp3")
        if not os.path.exists(audio_filename):
            try:
                await generate_audio(voice_text, audio_filename)
                print(f"Generated audio for scene {idx}")
            except Exception as e:
                print(f"Failed to generate audio for scene {idx}: {e}")
                continue
        
        # process_render expects audio_files keys to be str(idx) matching script_data
        # But wait, logic in app.py:
        # for idx, scene in enumerate(script_data):
        #     audio_path = audio_files.get(str(idx))
        # So we must match the NEW script_data index.
        audio_files[str(len(script_data)-1)] = audio_filename

    print(f"Prepared {len(script_data)} scenes.")
    
    # 3. Call process_render
    # Def: process_render(video_path, script_data, audio_files, verbose=False, resolution="native")
    print("Starting Rendering Process...")
    try:
        final_path = process_render(
            video_path=os.path.abspath(INPUT_VIDEO),
            script_data=script_data,
            audio_files=audio_files,
            verbose=True,
            resolution=RESOLUTION
        )
        print(f"Render Success! Output: {final_path}")
    except Exception as e:
        print(f"Render Failed: {e}")
        import traceback
        traceback.print_exc()

    # ... (code inside main) is largely fine, but need to update initial variables
    
    # 3. Call process_render
    # ...

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Test Video Rendering")
    parser.add_argument("json_file", nargs="?", default="1.json", help="Input JSON script file")
    parser.add_argument("video_file", nargs="?", default="1.mkv", help="Input video source file")
    parser.add_argument("-r", "--resolution", default="native", help="Resolution: native, 360p, 480p, 720p, 1080p, or WxH")
    args = parser.parse_args()

    # Update globals or pass to main
    INPUT_JSON = args.json_file
    INPUT_VIDEO = args.video_file
    RESOLUTION = args.resolution
    
    # Check strict existence here or let main handle it
    if not os.path.exists(INPUT_JSON):
        print(f"Error: JSON file '{INPUT_JSON}' not found.")
        exit(1)
    if not os.path.exists(INPUT_VIDEO):
        print(f"Error: Video file '{INPUT_VIDEO}' not found.")
        exit(1)

    # Need to pass these to main or update global scope if main uses globals. 
    # The current main uses global INPUT_JSON/INPUT_VIDEO. 
    # Let's refactor main to accept args or just rely on the global scope update (module level).
    # Since INPUT_JSON is global in the script, updating it *before* calling main() works if main uses input_json or refer to global.
    # Looking at original code: INPUT_JSON is defined at module level. main() uses it.
    
    print(f"Using Script: {INPUT_JSON}")
    print(f"Using Video:  {INPUT_VIDEO}")
    print(f"Resolution:   {RESOLUTION}")

    asyncio.run(main())
