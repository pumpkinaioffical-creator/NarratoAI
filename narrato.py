
import asyncio
import json
import os
import shutil
import subprocess
import argparse
import sys
import tempfile
import edge_tts
import requests
import base64
import time

# ==========================================
# 1. 核心渲染逻辑
# ==========================================

# 临时文件目录
TEMP_DIR = "temp_render"
# 注意：在 main 函数中会清理重建

# Cerebrium Configuration
CEREBRIUM_API_URL = "https://api.aws.us-east-1.cerebrium.ai/v4/p-6f9c5d84/index-tts-2-demo/predict"
CEREBRIUM_AUTH_TOKEN = "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJwcm9qZWN0SWQiOiJwLTZmOWM1ZDg0IiwibmFtZSI6IiIsImRlc2NyaXB0aW9uIjoiIiwiZXhwIjozMjUwMzY3OTk5OX0.3vV86l4uSAj0I9wUfxYD6W22Pd6ezkMWvDYB9jBtn-JZrPK3VHGr5NKNhXqjEO14hwQLwudr2KLWeM4jOJ-luBcvEzH2fGEr7qqGU2O_sxzG-hnWj7gHelumlrdtgmBnxe7MQbrDhpmsFG-ovVq9ActJY_ONodD9XCzEY3Y061zeJBihO0IHMW5AbGwRU0ybiC09Yb-Xk_jEvfQNthhB9zJcGrge26sqeaRcSf-C-YoD8yN0IlqBgg0s4XHVNaCrwcniYx5ZKL1JnPV8ccvdqyDRDdr_QqT47hUuFDuy5PPsp-5-HWyw_phcC3wPiyL784J7bz3s3UTsPrLdjcksjg"

def get_duration(file_path):
    """获取媒体文件时长(秒)"""
    cmd = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", file_path
    ]
    try:
        if os.name == 'nt':
            # Windows下避免弹出窗口
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, startupinfo=startupinfo)
        else:
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        return float(result.stdout.strip())
    except:
        return 0.0

def has_audio_stream(file_path):
    """检查文件是否包含音频流"""
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "a",
        "-show_entries", "stream=index", "-of", "csv=p=0", file_path
    ]
    try:
        if os.name == 'nt':
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, startupinfo=startupinfo)
        else:
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        # 如果有音频流，输出不为空
        return bool(result.stdout.strip())
    except:
        return False

def run_ffmpeg(cmd, verbose=False, cwd=None):
    """Run FFmpeg command with optional stderr output for debugging"""
    # Force utf-8 and relax decoding to prevent crash on Windows (GBK vs UTF-8 issues)

    startupinfo = None
    if os.name == 'nt':
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=cwd,
        encoding='utf-8',
        errors='replace',
        startupinfo=startupinfo
    )

    if result.returncode != 0:
        if verbose:
            print(f"[FFmpeg 错误] 命令: {' '.join(cmd[:5])}...")
            # Print last 800 chars of stderr
            stderr_tail = result.stderr[-800:] if len(result.stderr) > 800 else result.stderr
            print(stderr_tail)
        raise subprocess.CalledProcessError(result.returncode, cmd)
    return result

def get_atempo_filter(speed):
    filters = []
    s = speed
    # Handle slowdown
    while s < 0.5:
        filters.append("atempo=0.5")
        s /= 0.5
    # Handle speedup
    while s > 2.0:
        filters.append("atempo=2.0")
        s /= 2.0
    filters.append(f"atempo={s}")
    return ",".join(filters)

def process_render(video_path, script_data, audio_files, verbose=False, resolution="native", cut_method="pad"):
    """
    核心渲染逻辑
    """
    output_filename = "final_output.mp4"
    final_path = os.path.join(TEMP_DIR, output_filename)

    segment_files = []
    srt_entries = []
    current_time_cursor = 0.0
    report_log = []

    # 确保 video_path 是绝对路径
    video_source_path = os.path.abspath(video_path)

    # 1. 处理每个片段
    for idx, scene in enumerate(script_data):
        fragments = scene.get('fragments', [])
        if not fragments:
            start_str = scene.get('time_start', '00:00')
            end_str = scene.get('time_end', '00:05')
            fragments = [{'start': start_str, 'end': end_str, 'speed': 1.0}]

        def parse_time(t_str):
            t_str = str(t_str)
            p = list(map(float, t_str.split(':')))
            if len(p) == 1:  return p[0]
            elif len(p) == 2: return p[0]*60 + p[1]
            elif len(p) == 3: return p[0]*3600 + p[1]*60 + p[2]
            else: raise ValueError(f"无效的时间格式: {t_str}")

        audio_path = audio_files.get(str(idx))

        seg_video_name = f"seg_v_{idx}.mp4"
        seg_audio_name = f"seg_a_{idx}.wav"
        seg_out_name = f"clip_{idx}.mp4"

        p_seg_v = os.path.join(TEMP_DIR, seg_video_name)
        p_seg_a = os.path.join(TEMP_DIR, seg_audio_name)
        p_seg_out = os.path.join(TEMP_DIR, seg_out_name)

        source_video_duration = get_duration(video_source_path)

        frag_files = []

        for frag_idx, frag in enumerate(fragments):
            frag_start = parse_time(frag.get('start', '00:00'))
            frag_end = parse_time(frag.get('end', '00:05'))
            frag_speed = float(frag.get('speed', 1.0))

            if frag_start >= source_video_duration: frag_start = max(0, source_video_duration - 2)
            if frag_end > source_video_duration: frag_end = source_video_duration

            frag_dur = frag_end - frag_start
            if frag_dur <= 0: frag_dur = 1

            frag_file = os.path.join(TEMP_DIR, f"frag_{idx}_{frag_idx}.mp4")

            # 构建视频滤镜
            scale_filter = ""
            if resolution and resolution != "native":
                res_map = {
                    "360p": "scale=640:360",
                    "480p": "scale=854:480",
                    "720p": "scale=1280:720",
                    "1080p": "scale=1920:1080",
                }
                if resolution in res_map:
                    scale_filter = res_map[resolution] + ","
                elif "x" in resolution:
                    scale_filter = f"scale={resolution.replace('x', ':')},"

            vf_filters = []
            if scale_filter: vf_filters.append(scale_filter.rstrip(','))

            if frag_speed != 1.0:
                vf_filters.append(f"setpts={1/frag_speed}*PTS")

            vf_chain = ",".join(vf_filters) if vf_filters else None

            # 构建音频滤镜 (保留原声并变速)
            af_chain = None
            if frag_speed != 1.0:
                af_chain = get_atempo_filter(frag_speed)

            cmd_frag = [
                "ffmpeg", "-y", "-ss", str(frag_start), "-t", str(frag_dur),
                "-i", video_source_path
            ]
            if vf_chain: cmd_frag.extend(["-vf", vf_chain])
            if af_chain: cmd_frag.extend(["-af", af_chain])

            cmd_frag.extend([
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-c:a", "aac",
                frag_file
            ])

            try:
                run_ffmpeg(cmd_frag, verbose=verbose)
                frag_files.append(frag_file)
            except Exception as e:
                if verbose: print(f"[警告] 片段 {idx+1} 子片段 {frag_idx+1} 切割失败: {e}")

        if not frag_files:
            if verbose: print(f"[跳过] 片段 {idx+1}: 无有效子片段")
            continue

        # 拼接子片段
        if len(frag_files) == 1:
            shutil.copy(frag_files[0], p_seg_v)
        else:
            concat_list = os.path.join(TEMP_DIR, f"concat_{idx}.txt")
            with open(concat_list, 'w', encoding='utf-8') as f:
                for ff in frag_files:
                    f.write(f"file '{os.path.abspath(ff)}'\n")

            cmd_concat = [
                "ffmpeg", "-y", "-f", "concat", "-safe", "0",
                "-i", concat_list,
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-an",
                p_seg_v
            ]
            run_ffmpeg(cmd_concat, verbose=verbose)

        video_dur = get_duration(p_seg_v)

        # 处理TTS音频
        run_ffmpeg(["ffmpeg", "-y", "-i", audio_path, p_seg_a], verbose=verbose)
        audio_dur = get_duration(p_seg_a)

        # 自动延长检查
        if audio_dur > video_dur + 0.1:
            diff = audio_dur - video_dur
            vo_text = scene.get('voiceover', '').strip()
            vo_snippet = (vo_text[:30] + '..') if len(vo_text) > 30 else vo_text

            last_frag = fragments[-1]
            last_frag_end = parse_time(last_frag.get('end', '00:05'))
            extend_start = last_frag_end
            extend_dur = diff + 0.5

            if extend_start + extend_dur > source_video_duration:
                extend_dur = source_video_duration - extend_start
                if extend_dur <= 0:
                    extend_start = 0
                    extend_dur = diff + 0.5
                    if extend_dur > source_video_duration:
                        extend_dur = source_video_duration

            if extend_dur > 0:
                print(f"[自动延长] 片段 {idx+1}: 从 {extend_start:.1f}s 延长 {extend_dur:.1f}s")
                extend_file = os.path.join(TEMP_DIR, f"extend_{idx}.mp4")

                scale_filter = None
                if resolution and resolution != "native":
                    if resolution in {"360p", "480p", "720p", "1080p"}:
                         # simple dict
                         res_map = {"360p": "scale=640:360", "480p": "scale=854:480", "1080p":"scale=1920:1080"}
                         scale_filter = res_map.get(resolution, "scale=1280:720")
                    elif "x" in resolution:
                        scale_filter = f"scale={resolution.replace('x', ':')}"

                cmd_extend = [
                    "ffmpeg", "-y", "-ss", str(extend_start), "-t", str(extend_dur),
                    "-i", video_source_path
                ]
                if scale_filter: cmd_extend.extend(["-vf", scale_filter])
                cmd_extend.extend([
                    "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                    "-an",
                    extend_file
                ])

                try:
                    run_ffmpeg(cmd_extend, verbose=verbose)

                    concat_extend = os.path.join(TEMP_DIR, f"concat_ext_{idx}.txt")
                    with open(concat_extend, 'w', encoding='utf-8') as f:
                        f.write(f"file '{os.path.abspath(p_seg_v)}'\n")
                        f.write(f"file '{os.path.abspath(extend_file)}'\n")

                    p_seg_v_extended = os.path.join(TEMP_DIR, f"seg_v_{idx}_ext.mp4")
                    cmd_concat_ext = [
                        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
                        "-i", concat_extend,
                        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                        "-an",
                        p_seg_v_extended
                    ]
                    run_ffmpeg(cmd_concat_ext, verbose=verbose)
                    shutil.move(p_seg_v_extended, p_seg_v)
                    video_dur = get_duration(p_seg_v)

                except Exception as e:
                    print(f"[警告] 自动延长失败: {e}")

            report_log.append(f"片段 {idx+1} [内容: {vo_snippet}]: 已自动延长视频 {diff:.2f}s")

        # 音视频混合
        cmd_merge = []
        video_dur = get_duration(p_seg_v)

        if cut_method == "cut" and video_dur > audio_dur + 0.1:
            cmd_merge = [
                "ffmpeg", "-y", "-i", p_seg_v, "-i", p_seg_a,
                "-map", "0:v", "-map", "1:a",
                "-c:v", "copy", "-c:a", "aac",
                "-shortest",
                p_seg_out
            ]
        elif video_dur > audio_dur + 0.1:
            has_original_audio = has_audio_stream(p_seg_v)
            if has_original_audio:
                audio_filter = f"[0:a]volume=0:enable='between(t,0,{audio_dur})'[bg];[1:a][bg]amix=inputs=2:duration=longest:dropout_transition=0[aout]"
                cmd_merge = [
                    "ffmpeg", "-y", "-i", p_seg_v, "-i", p_seg_a,
                    "-filter_complex", audio_filter,
                    "-map", "0:v", "-map", "[aout]",
                    "-c:v", "copy", "-c:a", "aac",
                    "-t", str(video_dur),
                    p_seg_out
                ]
            else:
                cmd_merge = [
                    "ffmpeg", "-y", "-i", p_seg_v, "-i", p_seg_a,
                    "-filter_complex", f"[1:a]apad=whole_dur={video_dur}[aout]",
                    "-map", "0:v", "-map", "[aout]",
                    "-c:v", "copy", "-c:a", "aac",
                    "-t", str(video_dur),
                    p_seg_out
                ]
        else:
             cmd_merge = [
                "ffmpeg", "-y", "-i", p_seg_v, "-i", p_seg_a,
                "-map", "0:v", "-map", "1:a",
                "-c:v", "copy", "-c:a", "aac",
                "-shortest",
                p_seg_out
            ]

        try:
             run_ffmpeg(cmd_merge, verbose=verbose)
             segment_files.append(p_seg_out)
        except Exception as e:
             if verbose: print(f"[警告] 音频混合失败，尝试仅使用TTS音频: {e}")
             fallback_cmd = [
                "ffmpeg", "-y", "-i", p_seg_v, "-i", p_seg_a,
                "-filter_complex", f"[1:a]apad=whole_dur={video_dur}[aout]",
                "-map", "0:v", "-map", "[aout]",
                "-c:v", "copy", "-c:a", "aac",
                "-t", str(video_dur),
                p_seg_out
             ]
             run_ffmpeg(fallback_cmd, verbose=verbose)
             segment_files.append(p_seg_out)

        # 字幕处理
        video_dur = get_duration(p_seg_out)
        def fmt_srt_time(seconds):
            m, s = divmod(seconds, 60)
            h, m = divmod(m, 60)
            ms = int((s - int(s)) * 1000)
            return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{ms:03d}"

        srt_start = fmt_srt_time(current_time_cursor)
        srt_end = fmt_srt_time(current_time_cursor + video_dur)
        srt_entries.append(f"{idx+1}\n{srt_start} --> {srt_end}\n{scene.get('voiceover', '')}\n")
        current_time_cursor += video_dur

    # 2. 合并所有片段
    print("Merging segments...")
    list_path = os.path.join(TEMP_DIR, "filelist.txt")
    with open(list_path, "w", encoding="utf-8") as f:
        for seg in segment_files:
            f.write(f"file '{os.path.basename(seg)}'\n")

    merged_tmp = os.path.join(TEMP_DIR, "merged_tmp.mp4")
    cmd_concat = [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", "filelist.txt",
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        "merged_tmp.mp4"
    ]
    run_ffmpeg(cmd_concat, verbose=verbose, cwd=TEMP_DIR)

    # 导出 SRT
    srt_path = os.path.join(TEMP_DIR, "subs.srt")
    with open(srt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(srt_entries))

    shutil.copy(merged_tmp, final_path)

    # 如果输出路径不是绝对路径，且我们在临时目录外，直接copy过去
    # 这里 final_path 指向 TEMP_DIR/final_output.mp4
    # 外部调用者会期望一个明确的输出文件。

    if report_log:
         with open("report.txt", "w", encoding="utf-8") as f:
            f.write("\n".join(report_log))

    return final_path, srt_path


# ==========================================
# 2. 命令行接口
# ==========================================

async def generate_audio(text, output_file):
    communicate = edge_tts.Communicate(text, "zh-CN-YunxiNeural")
    await communicate.save(output_file)

async def generate_audio_cerebrium(text, output_file, reference_audio_path):
    print(f"Reading reference audio file: {reference_audio_path}")
    if not os.path.exists(reference_audio_path):
        raise FileNotFoundError(f"Reference audio file not found: {reference_audio_path}")

    with open(reference_audio_path, "rb") as f:
        audio_data = f.read()
        audio_b64 = base64.b64encode(audio_data).decode("utf-8")

    payload = {
        "text": text,
        "prompt_audio": audio_b64,
        "emo_control_method": 0, # Same as speaker
        "do_sample": True,
        "temperature": 0.8
    }

    headers = {
        "Authorization": f"Bearer {CEREBRIUM_AUTH_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream"  # Request streaming response
    }

    print(f"Sending streaming request to {CEREBRIUM_API_URL}...")

    # Since requests is synchronous, we run it directly.
    # If concurrency was needed, we would use run_in_executor, but for CLI this is fine.

    try:
        with requests.post(CEREBRIUM_API_URL, json=payload, headers=headers, stream=True, timeout=600) as response:
            if response.status_code == 200:
                audio_b64_result = None

                # Process streaming response
                for line in response.iter_lines():
                    if line:
                        try:
                            # Decode the line
                            line_str = line.decode('utf-8')

                            # Handle SSE format (data: {...})
                            if line_str.startswith("data: "):
                                line_str = line_str[6:]

                            data = json.loads(line_str)
                            status = data.get("status")
                            progress = data.get("progress", 0)

                            if status != "processing" or progress % 10 == 0:
                                print(f"Status: {status}, Progress: {progress}%", end="\r")

                            if status == "success":
                                chunk = data.get("audio_chunk")
                                if chunk:
                                    if audio_b64_result is None:
                                        audio_b64_result = ""
                                    audio_b64_result += chunk

                            elif status == "error":
                                raise Exception(f"Cerebrium Error: {data.get('message')}")

                        except json.JSONDecodeError:
                            pass # Skip invalid JSON lines

                print("") # New line after progress

                # Save audio if we got it
                if audio_b64_result:
                    with open(output_file, "wb") as f:
                        f.write(base64.b64decode(audio_b64_result))
                    print(f"Audio saved to {output_file}")
                else:
                    raise Exception("No audio data received from Cerebrium!")

            else:
                raise Exception(f"Request failed with status code {response.status_code}: {response.text[:200]}")

    except requests.exceptions.Timeout:
        raise Exception("Request timed out after 600 seconds")

async def main_cli():
    parser = argparse.ArgumentParser(description="NarratoAI Video Renderer")
    parser.add_argument("json_file", nargs="?", default="1.json", help="Input JSON script file")
    parser.add_argument("video_file", nargs="?", default=None, help="Input video source file (optional, auto-detected if None)")
    parser.add_argument("-r", "--resolution", default="native", help="Resolution: native, 360p, 480p, 720p, 1080p, or WxH")
    parser.add_argument("--cut", action="store_true", help="If set, cut video to match audio length (instead of padding audio)")

    # New arguments
    parser.add_argument("--cerebrium", action="store_true", help="Use Cerebrium API for voice cloning")
    parser.add_argument("--input_audio", help="Reference audio file path for voice cloning (required if --cerebrium is set)")

    args = parser.parse_args()

    input_json = args.json_file
    input_video = args.video_file
    resolution = args.resolution
    cut_method = "cut" if args.cut else "pad"

    # Validation for Cerebrium
    if args.cerebrium and not args.input_audio:
        print("Error: --input_audio is required when using --cerebrium")
        sys.exit(1)

    if args.input_audio and not os.path.exists(args.input_audio):
        print(f"Error: Reference audio file '{args.input_audio}' not found.")
        sys.exit(1)

    if not os.path.exists(input_json):
        print(f"Error: JSON file '{input_json}' not found.")
        sys.exit(1)

    # Auto detect video if not provided (e.g. 1.json -> 1.mp4 or 1.mkv)
    if not input_video:
        base = os.path.splitext(input_json)[0]
        for ext in ['.mp4', '.mkv', '.mov', '.avi']:
            if os.path.exists(base + ext):
                input_video = base + ext
                break

    if not input_video or not os.path.exists(input_video):
        print(f"Error: Video file not found. Please specify or ensure video matches json name.")
        sys.exit(1)

    print(f"Using Script: {input_json}")
    print(f"Using Video:  {input_video}")
    print(f"Resolution:   {resolution}")
    print(f"Cut Method:   {cut_method}")
    if args.cerebrium:
        print(f"TTS Engine:   Cerebrium (Cloning from {args.input_audio})")
    else:
        print(f"TTS Engine:   Edge-TTS")

    # Read and flatten data
    with open(input_json, 'r', encoding='utf-8') as f:
        data = json.load(f)

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

    if os.path.exists(TEMP_DIR):
        shutil.rmtree(TEMP_DIR)
    os.makedirs(TEMP_DIR, exist_ok=True)

    # 也是在这里创建 temp_audio
    TEMP_AUDIO_DIR = "temp_audio"
    if not os.path.exists(TEMP_AUDIO_DIR):
        os.makedirs(TEMP_AUDIO_DIR)

    for idx, item in enumerate(data):
        voice_text = item.get("voiceover", "")
        fragments = item.get("fragments", [])

        if not fragments:
            print(f"Skipping item {idx}: No fragments.")
            continue

        json_prefix = os.path.basename(input_json).split('.')[0]
        audio_filename = os.path.abspath(f"{TEMP_AUDIO_DIR}/tts_{json_prefix}_{idx}.mp3")

        # In Cerebrium mode, we might want to force regeneration if it's a different engine,
        # but for simplicity, we assume if the file exists it's good, or user cleans temp_audio.
        # However, to be safe, if using Cerebrium, maybe we should suffix the filename?
        # For now, we rely on user managing temp_audio or just overwrite check.

        if not os.path.exists(audio_filename):
            try:
                if args.cerebrium:
                    await generate_audio_cerebrium(voice_text, audio_filename, args.input_audio)
                else:
                    await generate_audio(voice_text, audio_filename)
                print(f"Generated audio for scene {idx}")
            except Exception as e:
                print(f"Failed to generate audio for scene {idx}: {e}")
                continue

        scene = {"fragments": fragments, "voiceover": voice_text}
        script_data.append(scene)
        audio_files[str(len(script_data)-1)] = audio_filename

    print(f"Prepared {len(script_data)} scenes. Starting Render...")

    try:
        final_mp4, final_srt = process_render(
            video_path=input_video,
            script_data=script_data,
            audio_files=audio_files,
            verbose=True,
            resolution=resolution,
            cut_method=cut_method
        )

        # Move output to current directory
        base_name = os.path.splitext(input_json)[0]
        output_mp4 = f"{base_name}_output.mp4"
        output_srt = f"{base_name}_output.srt"

        shutil.copy(final_mp4, output_mp4)
        shutil.copy(final_srt, output_srt)

        print(f"\nRender Success!")
        print(f"Video: {os.path.abspath(output_mp4)}")
        print(f"Subs : {os.path.abspath(output_srt)}")

    except Exception as e:
        print(f"Render Failed: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main_cli())
