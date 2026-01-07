import asyncio
import json
import os
import edge_tts
from render_engine import process_render

INPUT_JSON = "1.json"
INPUT_VIDEO = "1.mkv"
RESOLUTION = "native"  # 默认原始分辨率
USE_GPU = False  # 默认使用CPU
GPU_SURFACES = 64  # GPU编码缓冲区
GPU_LOOKAHEAD = 32  # 前瞻帧数

def add_comma_breaks(text):
    """在逗号处添加停顿（SSML风格，edge-tts会识别）"""
    # 中文逗号和英文逗号都加停顿
    return text.replace("，", "，ー").replace(",", ", ")

async def generate_audio_with_retry(text, output_file, max_retries=None):
    """生成音频，失败无限重试"""
    # 添加逗号断句处理
    processed_text = add_comma_breaks(text)
    attempt = 0
    while True:
        attempt += 1
        try:
            communicate = edge_tts.Communicate(processed_text, "zh-CN-YunxiNeural")
            await communicate.save(output_file)
            return True
        except Exception as e:
            print(f"[重试 {attempt}] 音频生成失败: {e}")
            await asyncio.sleep(1)  # 等1秒后重试
            if max_retries and attempt >= max_retries:
                return False

async def generate_all_audio_parallel(tasks):
    """并行生成所有音频"""
    return await asyncio.gather(*tasks, return_exceptions=True)

async def main():
    if not os.path.exists(INPUT_JSON):
        print(f"Error: {INPUT_JSON} not found.")
        return
    if not os.path.exists(INPUT_VIDEO):
        print(f"Error: {INPUT_VIDEO} not found.")
        return

    # 1. Read JSON
    with open(INPUT_JSON, 'r', encoding='utf-8') as f:
        data = json.load(f)

    # 2. Recursively flatten data if nested
    def flatten(items):
        result = []
        for item in items:
            if isinstance(item, list):
                result.extend(flatten(item))
            elif isinstance(item, dict):
                result.append(item)
        return result
    
    data = flatten(data)
    
    # Clear and recreate temp directories
    import shutil
    for temp_dir in ["temp_audio", "temp_render"]:
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)
        os.makedirs(temp_dir)

    # 3. 准备所有音频生成任务
    json_prefix = os.path.basename(INPUT_JSON).split('.')[0]
    audio_tasks = []
    task_info = []  # 记录每个任务对应的idx和文件名
    
    for idx, item in enumerate(data):
        voice_text = item.get("voiceover", "")
        fragments = item.get("fragments", [])
        
        if not fragments or not voice_text:
            continue
        
        audio_filename = os.path.abspath(f"temp_audio/tts_{json_prefix}_{idx}.mp3")
        audio_tasks.append(generate_audio_with_retry(voice_text, audio_filename))
        task_info.append((idx, audio_filename, fragments, voice_text))
    
    # 4. 并行生成所有音频
    print(f"[TTS] 并行生成 {len(audio_tasks)} 个音频...")
    await generate_all_audio_parallel(audio_tasks)
    print(f"[TTS] 音频生成完成！")
    
    # 5. 构建 script_data 和 audio_files
    script_data = []
    audio_files = {}
    
    for idx, audio_filename, fragments, voice_text in task_info:
        if os.path.exists(audio_filename):
            scene = {
                "fragments": fragments,
                "voiceover": voice_text
            }
            script_data.append(scene)
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
            resolution=RESOLUTION,
            gpu=USE_GPU,
            gpu_surfaces=GPU_SURFACES,
            gpu_lookahead=GPU_LOOKAHEAD
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
    parser.add_argument("--surfaces", type=int, default=64, help="GPU编码缓冲区 (8-64, 越高越吃显存越快)")
    parser.add_argument("--lookahead", type=int, default=32, help="前瞻帧数 (0-32, 越高越吃显存质量越好)")
    args = parser.parse_args()

    # Update globals or pass to main
    INPUT_JSON = args.json_file
    INPUT_VIDEO = args.video_file
    RESOLUTION = args.resolution
    USE_GPU = args.gpu
    GPU_SURFACES = args.surfaces
    GPU_LOOKAHEAD = args.lookahead
    
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
