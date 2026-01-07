import asyncio
import json
import os
import edge_tts
from render_engine import process_render

INPUT_JSON = "1.json"
INPUT_VIDEO = "1.mkv"
RESOLUTION = "native"  # 默认原始分辨率
USE_GPU = False  # 默认使用CPU

async def generate_audio(text, output_file, max_retries=float('inf')):
    """
    使用 edge_tts 生成音频，支持无限重试
    """
    retry_count = 0
    while True:
        try:
            # edge_tts 会自动根据标点符号进行断句，不需要额外SSML
            communicate = edge_tts.Communicate(text, "zh-CN-YunxiNeural")
            await communicate.save(output_file)
            return True
        except Exception as e:
            retry_count += 1
            print(f"[重试 {retry_count}] 音频生成失败: {e}")
            await asyncio.sleep(1)  # 等待1秒后重试

async def generate_audio_task(idx, text, output_file):
    """单个音频生成任务，用于并发"""
    await generate_audio(text, output_file)
    print(f"✓ 音频 {idx+1} 生成完成")
    return idx, output_file

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

    print("准备音频生成任务...")
    
    # Clear and recreate temp directories (no caching)
    import shutil
    for temp_dir in ["temp_audio", "temp_render"]:
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)
        os.makedirs(temp_dir)

    # 收集所有需要生成的音频任务
    audio_tasks = []
    scene_mapping = {}  # 用于追踪 idx -> scene 的映射
    
    json_prefix = os.path.basename(INPUT_JSON).split('.')[0]
    
    for idx, item in enumerate(data):
        voice_text = item.get("voiceover", "")
        fragments = item.get("fragments", [])
        
        if not fragments:
            print(f"跳过 {idx}: 没有视频片段")
            continue
        
        audio_filename = os.path.abspath(f"temp_audio/tts_{json_prefix}_{idx}.mp3")
        
        # 创建异步任务
        task = generate_audio_task(idx, voice_text, audio_filename)
        audio_tasks.append(task)
        
        # 保存场景信息
        scene_mapping[idx] = {
            "fragments": fragments,
            "voiceover": voice_text,
            "audio_file": audio_filename
        }
    
    print(f"开始并发生成 {len(audio_tasks)} 个音频...")
    
    # 并发执行所有音频生成任务
    results = await asyncio.gather(*audio_tasks)
    
    print(f"所有音频生成完成！")
    
    # 按顺序构建 script_data 和 audio_files
    for idx, audio_file in results:
        scene_info = scene_mapping[idx]
        scene = {
            "fragments": scene_info["fragments"],
            "voiceover": scene_info["voiceover"]
        }
        script_data.append(scene)
        audio_files[str(len(script_data)-1)] = audio_file

    print(f"准备了 {len(script_data)} 个场景。")
    
    # 3. Call process_render
    # Def: process_render(video_path, script_data, audio_files, verbose=False, resolution="native")
    print("Starting Rendering Process...")
    try:
        final_path = process_render(
            video_path=os.path.abspath(INPUT_VIDEO),
            script_data=script_data,
            audio_files=audio_files,
            verbose=True,
            resolution=RESOLUTION,
            gpu=USE_GPU
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
    parser.add_argument("--gpu", action="store_true", help="Use NVIDIA GPU (NVENC) for encoding")
    args = parser.parse_args()

    # Update globals or pass to main
    INPUT_JSON = args.json_file
    INPUT_VIDEO = args.video_file
    RESOLUTION = args.resolution
    USE_GPU = args.gpu
    
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
    print(f"GPU Accel:    {'NVIDIA NVENC' if USE_GPU else 'CPU (libx264)'}")

    asyncio.run(main())
