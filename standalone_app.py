import edge_tts
import json
import os
import shutil
import subprocess
import asyncio
import sys
import time
import platform
import urllib.request

# ================= 配置区域 =================
VIDEO_FILE = "1.mp4"
SCRIPT_FILE = "1.json"
OUTPUT_VIDEO = "final_output.mp4"
OUTPUT_SRT = "final_output.srt"
TTS_VOICE = "zh-CN-YunxiNeural"

# 🔧 核心参数
TARGET_FPS = 30
CPU_THREADS = "8"

# 📐 分辨率设置 (0 = 原画)
TARGET_WIDTH = 0
TARGET_HEIGHT = 0
# ===========================================

# ----------------- 工具函数 -----------------

def get_video_resolution(f):
    """获取视频宽高"""
    try:
        cmd_w = ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width", "-of", "default=noprint_wrappers=1:nokey=1", f]
        w = int(subprocess.check_output(cmd_w).decode().strip())
        cmd_h = ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=height", "-of", "default=noprint_wrappers=1:nokey=1", f]
        h = int(subprocess.check_output(cmd_h).decode().strip())
        return w, h
    except:
        return 1920, 1080

# 全局变量
USE_GPU = False

def check_nvenc():
    try:
        res = subprocess.run(["ffmpeg", "-encoders"], capture_output=True, text=True, encoding='utf-8', errors='ignore')
        return "h264_nvenc" in res.stdout
    except: return False

def get_encoder_options():
    """
    参考项目中的优化参数配置
    增加码率控制和GOP设置以防止卡顿
    """
    common_opts = [
        "-r", str(TARGET_FPS),
        "-g", str(TARGET_FPS * 2),  # GOP size = 2秒，防止关键帧间隔过大
        "-b:v", "5M",               # 目标码率 5Mbps
        "-maxrate", "8M",           # 最大码率 8Mbps
        "-bufsize", "10M",          # 缓冲区大小
        "-pix_fmt", "yuv420p",      # 兼容性最好的像素格式
        "-movflags", "+faststart",  # 优化Web播放
        "-threads", CPU_THREADS
    ]

    if USE_GPU:
        # NVENC 推荐参数
        return ["-c:v", "h264_nvenc", "-preset", "medium", "-cq", "23", "-profile:v", "main"] + common_opts
    else:
        # libx264 推荐参数
        return ["-c:v", "libx264", "-preset", "medium", "-profile:v", "high"] + common_opts

def run_cmd(cmd, tag="FFmpeg", quiet=True):
    try:
        cmd = [str(c) for c in cmd]
        if "ffmpeg" in cmd[0]:
            cmd.extend(["-max_muxing_queue_size", "4096"]) # 增加队列大小防止溢出

        # 打印调试命令（可选）
        # print(f"执行: {' '.join(cmd)}")

        result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='ignore')
        if result.returncode != 0:
            print(f"\n❌ [{tag}] 错误:")
            print(f"命令: {' '.join(cmd)}")
            print(f"报错: {result.stderr[-800:]}")
            return False
        return True
    except Exception as e:
        print(f"\n❌ [{tag}] 异常: {e}")
        return False

def get_duration(f):
    try:
        cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", f]
        o = subprocess.check_output(cmd, stderr=subprocess.STDOUT)
        return float(o.decode().strip())
    except: return 0.0

def fmt_time(s):
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{int((s%1)*1000):03d}"

def pt(t):
    try:
        t = str(t).strip()
        parts = list(map(float, t.split(':')))
        if len(parts) == 3: return parts[0]*3600 + parts[1]*60 + parts[2]
        if len(parts) == 2: return parts[0]*60 + parts[1]
        return float(t)
    except: return -1.0

def extract_all_blocks(data):
    blocks = []
    def recursive_find(obj):
        if isinstance(obj, dict):
            if "voiceover" in obj: blocks.append(obj)
            for k, v in obj.items():
                if isinstance(v, (dict, list)) and k != "fragments": recursive_find(v)
        elif isinstance(obj, list):
            for item in obj: recursive_find(item)
    recursive_find(data)
    return blocks

async def main():
    global USE_GPU
    start_time = time.time()

    # 1. 环境检测
    USE_GPU = check_nvenc()

    if not os.path.exists(VIDEO_FILE): return print(f"❌ 缺视频: {VIDEO_FILE}")
    if not os.path.exists(SCRIPT_FILE): return print(f"❌ 缺脚本: {SCRIPT_FILE}")

    source_w, source_h = get_video_resolution(VIDEO_FILE)
    final_w = TARGET_WIDTH if TARGET_WIDTH > 0 else source_w
    final_h = TARGET_HEIGHT if TARGET_HEIGHT > 0 else source_h
    is_resizing = (final_w != source_w) or (final_h != source_h)

    engine_name = "🚀 CUDA (NVIDIA GPU)" if USE_GPU else "🐌 CPU (libx264)"
    print("="*60)
    print(f"🚀 启动 V26 修复版 (NarratoAI Optimized)")
    print(f"📺 视频源: {source_w}x{source_h}")
    print(f"📏 目标: {final_w}x{final_h} {'(需缩放)' if is_resizing else '(原画)'}")
    print(f"🎮 引擎: {engine_name}")
    print("="*60)

    if os.path.exists("temp"): shutil.rmtree("temp")
    os.makedirs("temp", exist_ok=True)

    video_total_duration = get_duration(VIDEO_FILE)

    try:
        with open(SCRIPT_FILE, 'r', encoding='utf-8') as f: data = json.load(f)
    except Exception as e: return print(f"❌ JSON错误: {e}")

    blocks = extract_all_blocks(data)
    segment_files = []
    srt_lines = []
    current_srt_time = 0.0
    encode_params = get_encoder_options()

    print(f"\n🎬 共有 {len(blocks)} 个片段待处理...\n")

    for idx, block in enumerate(blocks):
        text = block.get('voiceover', '...')
        text_preview = text[:20] + "..." if len(text) > 20 else text

        print(f"🔹 [片段 {idx+1}/{len(blocks)}] 正在处理...")
        print(f"   📖 解说内容: \"{text_preview}\"")

        # 1. 生成 TTS (强制统一采样率)
        f_tts = f"temp/t_{idx}.mp3"
        f_wav = f"temp/a_{idx}.wav"
        try:
            await edge_tts.Communicate(text, TTS_VOICE).save(f_tts)
            # 强制转为 44100Hz 单声道，避免合并时出错
            run_cmd(["ffmpeg", "-y", "-i", f_tts, "-ar", "44100", "-ac", "1", f_wav], "TTS")
            audio_dur = get_duration(f_wav)
        except:
            run_cmd(["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", "1", f_wav], "静音")
            audio_dur = 1.0

        # 2. 解析视频片段
        fragments = block.get('fragments', [])
        valid_frag_infos = []
        for frag in fragments:
            s = pt(frag.get('start', 0))
            e = pt(frag.get('end', 0))
            spd = float(frag.get('speed', 1.0))
            if s >= video_total_duration or e <= s: continue
            if e > video_total_duration: e = video_total_duration
            valid_frag_infos.append({"start": s, "end": e, "raw_dur": e-s, "speed": spd})

        if not valid_frag_infos:
            print("   ⚠️  [警告] 该片段没有有效的视频对应关系，跳过！")
            continue

        # 3. 核心：时长校验与补充逻辑
        current_video_dur = sum(f['raw_dur'] / f['speed'] for f in valid_frag_infos)

        if current_video_dur < audio_dur:
            diff = audio_dur - current_video_dur
            last = valid_frag_infos[-1]
            need_raw = diff * last['speed']
            avail_raw = video_total_duration - last['end']

            if avail_raw > 0.05:
                add_raw = min(need_raw, avail_raw)
                last['end'] += add_raw
                last['raw_dur'] += add_raw
                print(f"      ↳ 🔄 自动延长片段: +{add_raw/last['speed']:.2f}s")

        # 4. 视频切割与处理 (第一步编码，标准化中间素材)
        frag_temp_files = []
        real_video_dur_sum = 0.0

        for f_i, info in enumerate(valid_frag_infos):
            f_seg = f"temp/v_{idx}_{f_i}.mp4"

            # 使用 setpts 调整速度，fps 滤镜确保帧率统一
            filter_parts = [f"[0:v]setpts={1.0/info['speed']}*PTS"]
            if is_resizing: filter_parts.append(f"scale={final_w}:{final_h}")
            filter_parts.append(f"fps={TARGET_FPS}[v]")
            vf = ",".join(filter_parts)

            # 中间文件使用高码率或复制参数，这里我们应用标准参数以防止第一步就卡顿
            cmd = ["ffmpeg", "-y", "-ss", str(info['start']), "-t", str(info['raw_dur']), "-i", VIDEO_FILE,
                   "-filter_complex", vf, "-map", "[v]",
                   "-r", str(TARGET_FPS),
                   "-pix_fmt", "yuv420p"]

            # 应用统一的编码参数
            cmd.extend(encode_params)
            cmd.extend(["-an", f_seg])

            if run_cmd(cmd, quiet=True):
                if os.path.exists(f_seg):
                    frag_temp_files.append(f_seg)
                    real_video_dur_sum += (info['raw_dur'] / info['speed'])

        # 5. 合并当前段落的视频 (Concat Demuxer)
        f_combined = f"temp/v_combined_{idx}.mp4"
        with open(f"temp/list_{idx}.txt", "w", encoding='utf-8') as f:
            for n in [os.path.abspath(x).replace("\\", "/") for x in frag_temp_files]: f.write(f"file '{n}'\n")

        # Concat 合并时，如果文件参数一致，copy 是最快的且无损的
        run_cmd(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", f"temp/list_{idx}.txt", "-c", "copy", f_combined], quiet=True)

        # 6. 音画对齐 (最终合并)
        f_out = f"temp/out_{idx}.mp4"
        final_dur = max(audio_dur, real_video_dur_sum)

        # 优化 tpad 逻辑：如果 ffmpeg 版本较老，tpad 可能有问题。
        # 这里改用更稳健的 loop 或 pad 滤镜通常比较复杂，我们保留 tpad 但确保 fps 滤镜位置正确
        # 关键：fps 滤镜应该在 tpad 之后，或者 tpad 之前确保时间基准正确。
        # 最稳妥的方式：只处理音频长度对齐，视频不足部分让它保持最后一帧 (tpad=stop_mode=clone) 而不是黑屏 (stop_mode=add:color=black)
        # clone 模式通常看起来更自然，不会突然黑屏。

        if real_video_dur_sum > audio_dur + 0.05:
            # 视频比音频长，截断视频? 或者填充静音? 通常保留视频长度
            filter_c = f"[1:a]apad[a]"
            map_v = "0:v" # 直接使用 0:v 引用输入流
        elif audio_dur > real_video_dur_sum + 0.05:
            pad = audio_dur - real_video_dur_sum
            # 使用 clone 模式复制最后一帧，比黑屏更平滑
            filter_c = f"[0:v]tpad=stop_mode=clone:stop_duration={pad}[v];[1:a]anull[a]"
            map_v = "[v]"
        else:
            filter_c = f"[1:a]anull[a]"
            map_v = "0:v" # 直接使用 0:v 引用输入流

        cmd_merge = ["ffmpeg", "-y", "-i", f_combined, "-i", f_wav]

        # 修正逻辑：只有当我们需要使用 filter_complex 中的视频流时才 map [v]
        # 否则，如果视频流没有进 filter_complex (即只处理音频)，我们直接 map 0:v

        if map_v == "0:v":
            # 视频流未经过处理，仅处理音频
            cmd_merge.extend(["-filter_complex", filter_c, "-map", "0:v", "-map", "[a]"])
        else:
             # 视频流经过处理（tpad等），使用 filter 输出的标签
            cmd_merge.extend(["-filter_complex", filter_c, "-map", map_v, "-map", "[a]"])

        # 再次强制输出参数，确保合并后的片段也是标准的
        cmd_merge.extend(["-t", str(final_dur)])
        cmd_merge.extend(encode_params)
        cmd_merge.extend(["-c:a", "aac", "-b:a", "128k", f_out]) # 音频编码 AAC 128k

        if run_cmd(cmd_merge, quiet=True):
            segment_files.append(os.path.abspath(f_out))
            srt_lines.append(f"{len(segment_files)}\n{fmt_time(current_srt_time)} --> {fmt_time(current_srt_time + audio_dur)}\n{text}\n")
            current_srt_time += final_dur
            print("   🆗 片段生成完毕\n")

    # 导出 SRT
    with open(OUTPUT_SRT, "w", encoding="utf-8") as f: f.write("\n".join(srt_lines))
    print(f"📝 SRT字幕已导出: {OUTPUT_SRT}")

    # 最终拼接
    print("📦 正在拼接最终视频...")
    if segment_files:
        with open("temp/list_final.txt", "w", encoding='utf-8') as f:
            for n in segment_files: f.write(f"file '{n.replace('\\','/')}'\n")
        # 最终合并使用 copy 模式即可，因为前面所有的 out_{idx} 都已经统一了编码参数
        run_cmd(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "temp/list_final.txt", "-c", "copy", "-movflags", "+faststart", OUTPUT_VIDEO], "拼接")

    if os.path.exists(OUTPUT_VIDEO):
        end_time = time.time()
        print("\n" + "="*60)
        print(f"🎉🎉🎉 全部完成！耗时: {int(end_time - start_time)}秒")
        print(f"📂 输出: {os.path.abspath(OUTPUT_VIDEO)}")
        print("="*60)

if __name__ == "__main__":
    try:
        import nest_asyncio
        nest_asyncio.apply()
    except: pass
    asyncio.run(main())
