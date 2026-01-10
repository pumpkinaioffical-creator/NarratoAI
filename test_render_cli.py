import asyncio
import json
import os
import edge_tts
from render_engine import process_render

INPUT_JSON = "1.json"
INPUT_VIDEO = "1.mkv"
RESOLUTION = "native"
CUT_METHOD = "pad"

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

    # 1. Read input json
    with open(INPUT_JSON, 'r', encoding='utf-8') as f:
        data = json.load(f)

    # 2. Adapt to schema
    # Recursively flatten data if nested
    def flatten(items):
        result = []
        for item in items:
            if isinstance(item, list):
                result.extend(flatten(item))
            elif isinstance(item, dict):
                result.append(item)
        return result
    
    data = flatten(data)
    
    script_data = []
    audio_files = {}

    print("Generating audio and preparing script data...")
    
    # Clear and recreate temp directories
    import shutil
    for temp_dir in ["temp_audio", "temp_render"]:
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)
        os.makedirs(temp_dir)

    for idx, item in enumerate(data):
        voice_text = item.get("voiceover", "")
        fragments = item.get("fragments", [])
        
        if not fragments:
            print(f"Skipping item {idx}: No fragments.")
            continue
        
        json_prefix = os.path.basename(INPUT_JSON).split('.')[0]
        audio_filename = os.path.abspath(f"temp_audio/tts_{json_prefix}_{idx}.mp3")
        if not os.path.exists(audio_filename):
            try:
                await generate_audio(voice_text, audio_filename)
                print(f"Generated audio for scene {idx}")
            except Exception as e:
                print(f"Failed to generate audio for scene {idx}: {e}")
                continue
        
        scene = {
            "fragments": fragments,
            "voiceover": voice_text
        }
        script_data.append(scene)
        audio_files[str(len(script_data)-1)] = audio_filename

    print(f"Prepared {len(script_data)} scenes.")
    
    # 3. Call process_render
    print("Starting Rendering Process...")
    try:
        final_path = process_render(
            video_path=os.path.abspath(INPUT_VIDEO),
            script_data=script_data,
            audio_files=audio_files,
            verbose=True,
            resolution=RESOLUTION,
            cut_method=CUT_METHOD
        )
        print(f"Render Success! Output: {final_path}")
    except Exception as e:
        print(f"Render Failed: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Test Video Rendering")
    parser.add_argument("json_file", nargs="?", default="1.json", help="Input JSON script file")
    parser.add_argument("video_file", nargs="?", default="1.mkv", help="Input video source file")
    parser.add_argument("-r", "--resolution", default="native", help="Resolution: native, 360p, 480p, 720p, 1080p, or WxH")
    parser.add_argument("--cut", action="store_true", help="If set, cut video to match audio length (instead of padding audio)")
    
    args = parser.parse_args()

    INPUT_JSON = args.json_file
    INPUT_VIDEO = args.video_file
    RESOLUTION = args.resolution
    CUT_METHOD = "cut" if args.cut else "pad"
    
    if not os.path.exists(INPUT_JSON):
        print(f"Error: JSON file '{INPUT_JSON}' not found.")
        exit(1)
    if not os.path.exists(INPUT_VIDEO):
        print(f"Error: Video file '{INPUT_VIDEO}' not found.")
        exit(1)
    
    print(f"Using Script: {INPUT_JSON}")
    print(f"Using Video:  {INPUT_VIDEO}")
    print(f"Resolution:   {RESOLUTION}")
    print(f"Cut Method:   {CUT_METHOD}")

    asyncio.run(main())
