import uvicorn
from fastapi import FastAPI, Body, UploadFile, File, Form
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
import edge_tts
import io
import json
import os
import shutil
import subprocess
import math
import tempfile
import argparse
import asyncio
import sys
from typing import List

# ==========================================
# 1. 后端逻辑
# ==========================================

app = FastAPI()

# 临时文件目录
TEMP_DIR = "temp_render"
if os.path.exists(TEMP_DIR):
    shutil.rmtree(TEMP_DIR)
os.makedirs(TEMP_DIR, exist_ok=True)

# ---------------------------------------------------
# FFmpeg 辅助函数
# ---------------------------------------------------

def get_duration(file_path):
    """获取媒体文件时长(秒)"""
    cmd = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", file_path
    ]
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return float(result.stdout.strip())
    except:
        return 0.0

def run_ffmpeg(cmd, verbose=False, cwd=None):
    """Run FFmpeg command with optional stderr output for debugging"""
    # Force utf-8 and relax decoding to prevent crash on Windows (GBK vs UTF-8 issues)
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, encoding='utf-8', errors='replace')
    if result.returncode != 0:
        if verbose:
            print(f"[FFmpeg 错误] 命令: {' '.join(cmd[:5])}...")
            # Print last 800 chars of stderr
            stderr_tail = result.stderr[-800:] if len(result.stderr) > 800 else result.stderr
            print(stderr_tail)
        raise subprocess.CalledProcessError(result.returncode, cmd)
    return result

def process_render(video_path, script_data, audio_files, verbose=False, resolution="native", gpu=False):
    """
    核心渲染逻辑:
    1. 遍历脚本，切割视频，处理音频同步
    2. 生成片段
    3. 合并片段
    4. 烧录字幕
    
    Args:
        verbose: If True, print progress to terminal (CLI mode)
        resolution: 'native' 保持原分辨率, '360p' 缩放到640x360
        gpu: If True, use NVIDIA GPU (h264_nvenc) for encoding
    """
    # 根据 gpu 参数选择编码器
    if gpu:
        video_codec = ["h264_nvenc", "-preset", "p4", "-cq", "23"]
        if verbose:
            print("[GPU] 使用 NVIDIA NVENC 硬件加速编码")
    else:
        video_codec = ["libx264", "-preset", "fast", "-crf", "23"]
    output_filename = "final_output.mp4"
    final_path = os.path.join(TEMP_DIR, output_filename)
    
    segment_files = []
    srt_entries = []
    current_time_cursor = 0.0
    total_scenes = len(script_data)
    
    # Progress callback
    def update_progress(step, detail=""):
        if verbose:
            print(f"[进度] {step}: {detail}")
        # Also write to file for GUI mode
        with open(os.path.join(TEMP_DIR, "progress.txt"), "w", encoding="utf-8") as f:
            f.write(f"{step}|{detail}")

    segment_files = []
    srt_entries = []
    current_time_cursor = 0.0
    report_log = []

    # 1. 处理每个片段
    for idx, scene in enumerate(script_data):
        # 支持新格式 (fragments列表) 和旧格式 (time_start/time_end)
        fragments = scene.get('fragments', [])
        if not fragments:
            # 兼容旧格式
            start_str = scene.get('time_start', '00:00')
            end_str = scene.get('time_end', '00:05')
            fragments = [{'start': start_str, 'end': end_str, 'speed': 1.0}]
        
        def parse_time(t_str):
            t_str = str(t_str)
            p = list(map(float, t_str.split(':')))
            if len(p) == 1:  # SS (pure seconds)
                return p[0]
            elif len(p) == 2:  # MM:SS
                return p[0]*60 + p[1]
            elif len(p) == 3:  # HH:MM:SS
                return p[0]*3600 + p[1]*60 + p[2]
            else:
                raise ValueError(f"无效的时间格式: {t_str}")
        
        # 对应的音频文件
        audio_path = audio_files.get(str(idx))
        
        # 临时文件名
        seg_video_name = f"seg_v_{idx}.mp4"
        seg_audio_name = f"seg_a_{idx}.wav"
        seg_out_name = f"clip_{idx}.mp4"
        
        p_seg_v = os.path.join(TEMP_DIR, seg_video_name)
        p_seg_a = os.path.join(TEMP_DIR, seg_audio_name)
        p_seg_out = os.path.join(TEMP_DIR, seg_out_name)
        
        # 获取视频总时长
        source_video_duration = get_duration(video_path)
        
        # 处理多片段: 切割每个片段并拼接
        frag_files = []
        total_video_dur = 0
        
        for frag_idx, frag in enumerate(fragments):
            frag_start = parse_time(frag.get('start', '00:00'))
            frag_end = parse_time(frag.get('end', '00:05'))
            frag_speed = float(frag.get('speed', 1.0))
            
            # 边界检查
            if frag_start >= source_video_duration:
                frag_start = max(0, source_video_duration - 2)
            if frag_end > source_video_duration:
                frag_end = source_video_duration
            
            frag_dur = frag_end - frag_start
            if frag_dur <= 0:
                frag_dur = 1
            
            # 计算变速后的时长
            actual_frag_dur = frag_dur / frag_speed
            total_video_dur += actual_frag_dur
            
            # 切割单个片段
            frag_file = os.path.join(TEMP_DIR, f"frag_{idx}_{frag_idx}.mp4")
            
            # 构建变速滤镜
            # 支持自定义分辨率：360p, 480p, 720p, 1080p 或 native（原始）
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
                    # 支持自定义 WxH 格式，如 "800x600"
                    scale_filter = f"scale={resolution.replace('x', ':')},"
            
            if frag_speed != 1.0:
                # setpts调整视频速度，例如 setpts=0.5*PTS 加速2倍
                speed_filter = f"setpts={1/frag_speed}*PTS"
                vf = f"{scale_filter}{speed_filter}"
            else:
                vf = scale_filter.rstrip(',') if scale_filter else None
            
            cmd_frag = [
                "ffmpeg", "-y", "-ss", str(frag_start), "-t", str(frag_dur),
                "-i", video_path
            ]
            if vf:
                cmd_frag.extend(["-vf", vf])
            cmd_frag.extend(["-c:v"] + video_codec + ["-an",
                frag_file
            ])
            
            try:
                run_ffmpeg(cmd_frag, verbose=verbose)
                frag_files.append(frag_file)
            except Exception as e:
                if verbose:
                    print(f"[警告] 片段 {idx+1} 子片段 {frag_idx+1} 切割失败: {e}")
        
        if not frag_files:
            if verbose:
                print(f"[跳过] 片段 {idx+1}: 无有效子片段")
            continue
        
        # 如果只有一个片段，直接使用；否则拼接
        if len(frag_files) == 1:
            import shutil
            shutil.copy(frag_files[0], p_seg_v)
        else:
            # 使用 concat demuxer 拼接多个片段
            concat_list = os.path.join(TEMP_DIR, f"concat_{idx}.txt")
            with open(concat_list, 'w', encoding='utf-8') as f:
                for ff in frag_files:
                    f.write(f"file '{os.path.abspath(ff)}'\n")
            
            cmd_concat = [
                "ffmpeg", "-y", "-f", "concat", "-safe", "0",
                "-i", concat_list,
            ] + ["-c:v"] + video_codec + ["-an",
                p_seg_v
            ]
            run_ffmpeg(cmd_concat, verbose=verbose)
        
        # 获取拼接后的实际视频时长
        actual_video_dur = get_duration(p_seg_v)
        video_dur = actual_video_dur
        
        # A. 处理音频 (计算是否需要延长视频)
        # 先转为wav并获取时长
        run_ffmpeg(["ffmpeg", "-y", "-i", audio_path, p_seg_a], verbose=verbose)
        audio_dur = get_duration(p_seg_a)
        
        final_audio_filter = "anull" # 默认不处理
        
        # 如果音频比视频长，自动延长最后一个片段
        if audio_dur > video_dur + 0.1:
            diff = audio_dur - video_dur
            vo_text = scene.get('voiceover', '').strip()
            vo_snippet = (vo_text[:30] + '..') if len(vo_text) > 30 else vo_text
            
            # 获取最后一个片段的结束时间，从那里继续延长
            last_frag = fragments[-1]
            last_frag_end = parse_time(last_frag.get('end', '00:05'))
            
            # 计算需要延长多少
            extend_start = last_frag_end
            extend_dur = diff + 0.5  # 多加0.5秒确保足够
            
            # 检查是否超出源视频
            if extend_start + extend_dur > source_video_duration:
                # 如果会超出，只能延长到视频末尾
                extend_dur = source_video_duration - extend_start
                if extend_dur <= 0:
                    # 源视频已经用完了，从头开始循环
                    extend_start = 0
                    extend_dur = diff + 0.5
                    if extend_dur > source_video_duration:
                        extend_dur = source_video_duration
            
            if extend_dur > 0:
                print(f"[自动延长] 片段 {idx+1}: 从 {extend_start:.1f}s 延长 {extend_dur:.1f}s")
                
                # 切割延长部分
                extend_file = os.path.join(TEMP_DIR, f"extend_{idx}.mp4")
                
                # 使用与主片段相同的分辨率逻辑
                scale_filter = None
                if resolution and resolution != "native":
                    res_map = {
                        "360p": "scale=640:360",
                        "480p": "scale=854:480",
                        "720p": "scale=1280:720",
                        "1080p": "scale=1920:1080",
                    }
                    if resolution in res_map:
                        scale_filter = res_map[resolution]
                    elif "x" in resolution:
                        scale_filter = f"scale={resolution.replace('x', ':')}"
                
                cmd_extend = [
                    "ffmpeg", "-y", "-ss", str(extend_start), "-t", str(extend_dur),
                    "-i", video_path
                ]
                if scale_filter:
                    cmd_extend.extend(["-vf", scale_filter])
                cmd_extend.extend(["-c:v"] + video_codec + ["-an",
                    extend_file
                ])
                
                try:
                    run_ffmpeg(cmd_extend, verbose=verbose)
                    
                    # 把延长部分拼接到原视频后面
                    concat_extend = os.path.join(TEMP_DIR, f"concat_ext_{idx}.txt")
                    with open(concat_extend, 'w', encoding='utf-8') as f:
                        f.write(f"file '{os.path.abspath(p_seg_v)}'\n")
                        f.write(f"file '{os.path.abspath(extend_file)}'\n")
                    
                    p_seg_v_extended = os.path.join(TEMP_DIR, f"seg_v_{idx}_ext.mp4")
                    cmd_concat_ext = [
                        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
                        "-i", concat_extend,
                    ] + ["-c:v"] + video_codec + ["-an",
                        p_seg_v_extended
                    ]
                    run_ffmpeg(cmd_concat_ext, verbose=verbose)
                    
                    # 用延长后的视频替换原来的
                    import shutil
                    shutil.move(p_seg_v_extended, p_seg_v)
                    
                    # 更新视频时长
                    video_dur = get_duration(p_seg_v)
                    
                except Exception as e:
                    print(f"[警告] 自动延长失败: {e}")
            
            msg = f"片段 {idx+1} [内容: {vo_snippet}]: 已自动延长视频 {diff:.2f}s"
            report_log.append(msg)
        

        # C. 合并当前片段 (视频 + 音频)
        # 注意：视频长度和音频长度可能不完全一致（由于帧率对齐等），
        # 如果视频延长后比音频略长，或者略短。
        # 使用 -shortest 可能会截断音频。如果视频定格需求，需要 pad。
        # 为简单起见，且满足“延伸视频”的需求，我们假设视频已经足够长（或者已到末尾）。
        # 如果视频比音频长，shortest会让视频适应音频。
        # 如果音频比视频长(素材耗尽)，shortest会让音频被截断。这是合理的。
        
        # 此时 final_audio_filter 应该是空的或者 anull
        
        cmd_merge = [
            "ffmpeg", "-y",
            "-i", p_seg_v,
            "-i", p_seg_a,
            "-filter_complex", f"[1:a]apad=whole_dur={video_dur}[aout]",
            "-map", "0:v", "-map", "[aout]",
            "-c:v", "copy", "-c:a", "aac",
            "-t", str(video_dur),
            p_seg_out
        ]
        run_ffmpeg(cmd_merge, verbose=verbose)
        segment_files.append(p_seg_out)
        
        # 更新视频时长用于字幕计时
        video_dur = get_duration(p_seg_out)
        
        # D. 记录字幕 (SRT格式)
        # 格式: 
        # 1
        # 00:00:00,000 --> 00:00:05,000
        # 字幕内容
        def fmt_srt_time(seconds):
            m, s = divmod(seconds, 60)
            h, m = divmod(m, 60)
            ms = int((s - int(s)) * 1000)
            return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{ms:03d}"
            
        srt_start = fmt_srt_time(current_time_cursor)
        srt_end = fmt_srt_time(current_time_cursor + video_dur)
        srt_entries.append(f"{idx+1}\n{srt_start} --> {srt_end}\n{scene['voiceover']}\n")
        
        current_time_cursor += video_dur

    # 2. 合并所有片段
    list_path = os.path.join(TEMP_DIR, "filelist.txt")
    with open(list_path, "w", encoding="utf-8") as f:
        for seg in segment_files:
            # ffmpeg concat demuxer 需要绝对路径或相对路径，注意转义
            f.write(f"file '{os.path.basename(seg)}'\n")
            
    merged_tmp = os.path.join(TEMP_DIR, "merged_tmp.mp4")
    # Use relative filename since we run with cwd=TEMP_DIR
    cmd_concat = [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", "filelist.txt",
        "-c:v"] + video_codec + ["-c:a", "aac", "-b:a", "128k",
        "merged_tmp.mp4"
    ]
    # 注意：cwd设为TEMP_DIR以便读取 filelist
    update_progress("合并片段", "正在拼接所有片段...")
    run_ffmpeg(cmd_concat, verbose=verbose, cwd=TEMP_DIR)
    
    # 3. 生成 SRT 文件 (保存备用，但不烧录)
    srt_path = os.path.join(TEMP_DIR, "subs.srt")
    with open(srt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(srt_entries))
        
    # 4. 跳过字幕烧录，直接使用合并后的视频作为最终输出
    import shutil
    shutil.copy(merged_tmp, final_path)
    update_progress("完成", "渲染完成！")
    
    # 5. 生成报告
    if report_log:
        report_path = "report.txt"
        with open(report_path, "w", encoding="utf-8") as f:
            f.write("\n".join(report_log))
        if verbose:
            print(f"[提示] 已生成延长报告: {report_path}")
            
    return final_path

# ---------------------------------------------------
# API
# ---------------------------------------------------

HTML_CONTENT = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>智能脚本配音剪辑器 v4.0 (含视频导出)</title>
    <link href="https://fonts.googleapis.com/css2?family=Noto+Sans+SC:wght@400;700;900&display=swap" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/mp4-muxer@5.1.3/build/mp4-muxer.min.js"></script>
    <style>
        :root { --bg: #111827; --card: #1f2937; --text: #f3f4f6; --accent: #3b82f6; --success: #10b981; --warn: #f59e0b; --danger: #ef4444; }
        body { background-color: var(--bg); color: var(--text); font-family: "Noto Sans SC", sans-serif; margin: 0; padding: 20px; display: flex; flex-direction: column; height: 100vh; box-sizing: border-box; }
        
        /* 置顶渲染进度条 */
        .render-progress-overlay {
            position: fixed; top: 0; left: 0; right: 0; z-index: 9999;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            padding: 12px 20px; display: none; flex-direction: column; gap: 8px;
            box-shadow: 0 4px 20px rgba(0,0,0,0.5);
        }
        .render-progress-bar { height: 8px; background: rgba(255,255,255,0.3); border-radius: 4px; overflow: hidden; }
        .render-progress-fill { height: 100%; background: #fff; width: 0%; transition: width 0.3s; border-radius: 4px; }
        .render-progress-text { font-size: 14px; font-weight: 700; color: #fff; display: flex; justify-content: space-between; }
        .render-progress-text span { opacity: 0.9; }
        
        .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px; }
        .header h1 { margin: 0; font-size: 20px; color: var(--accent); font-weight: 900; }

        .container { width: 100%; display: grid; grid-template-columns: 1fr 360px; gap: 20px; flex: 1; min-height: 0; }
        
        .video-wrapper { display: flex; flex-direction: column; height: 100%; gap: 10px; }
        .video-section { flex: 1; background: #000; border-radius: 12px; overflow: hidden; position: relative; display: flex; align-items: center; justify-content: center; }
        video { width: 100%; height: 100%; max-height: 100%; display: block; }
        
        .subtitle-overlay { position: absolute; bottom: 8%; width: 100%; text-align: center; pointer-events: none; z-index: 5; }
        .subtitle-text { 
            font-family: 'Noto Sans SC', sans-serif; font-weight: 900; 
            color: #fff; font-size: 32px; line-height: 1.3;
            text-shadow: 2px 2px 0px rgba(0,0,0,0.8);
            background: rgba(0,0,0,0.4); backdrop-filter: blur(2px);
            padding: 8px 16px; border-radius: 6px; 
            display: inline-block; max-width: 90%; opacity: 0;
        }
        
        .loading-overlay { position: absolute; inset: 0; background: rgba(0,0,0,0.8); display: flex; flex-direction: column; align-items: center; justify-content: center; z-index: 20; display: none; }
        .spinner { width: 40px; height: 40px; border: 4px solid #fff; border-top-color: var(--accent); border-radius: 50%; animation: spin 1s linear infinite; margin-bottom: 15px; }
        .progress-text { font-size: 14px; color: #fff; margin-top: 10px; }

        .player-controls { background: var(--card); padding: 10px; border-radius: 8px; display: flex; align-items: center; justify-content: center; gap: 10px; }
        .ctrl-btn { background: #374151; border: none; color: white; padding: 8px 16px; border-radius: 6px; cursor: pointer; font-size: 14px; font-weight: 700; font-family: "Noto Sans SC"; }
        .ctrl-btn:hover { background: #4b5563; }
        .ctrl-btn.main { background: var(--accent); font-weight: 900; }
        .ctrl-btn.reset { background: var(--warn); color: #000; }
        .ctrl-btn.render { background: #8b5cf6; color: #fff; }
        .ctrl-btn.render:hover { background: #7c3aed; }
        
        /* 进度条样式 */
        .progress-bar-container { background: var(--card); padding: 8px 15px; border-radius: 8px; display: flex; align-items: center; gap: 10px; }
        .time-display { font-size: 12px; color: #9ca3af; min-width: 80px; font-family: monospace; }
        .progress-slider { flex: 1; height: 6px; -webkit-appearance: none; appearance: none; background: #374151; border-radius: 3px; cursor: pointer; }
        .progress-slider::-webkit-slider-thumb { -webkit-appearance: none; width: 14px; height: 14px; background: var(--accent); border-radius: 50%; cursor: grab; }
        .progress-slider::-moz-range-thumb { width: 14px; height: 14px; background: var(--accent); border-radius: 50%; cursor: grab; border: none; }
        .progress-slider::-webkit-slider-runnable-track { height: 6px; background: linear-gradient(to right, var(--accent) var(--progress, 0%), #374151 var(--progress, 0%)); border-radius: 3px; }
        .progress-slider:active::-webkit-slider-thumb { cursor: grabbing; }

        .sidebar { background: var(--card); padding: 20px; border-radius: 12px; display: flex; flex-direction: column; gap: 15px; overflow-y: auto; }
        h2 { margin: 0 0 8px 0; font-size: 14px; color: #9ca3af; font-weight: 700; border-bottom: 1px solid #374151; padding-bottom: 5px; }
        input, select, textarea { width: 100%; background: #374151; border: 1px solid #4b5563; color: white; padding: 8px; border-radius: 4px; box-sizing: border-box; font-family: inherit; }
        textarea { height: 120px; font-family: monospace; font-size: 12px; }

        .status-list { flex: 1; overflow-y: auto; background: #111; border-radius: 6px; padding: 10px; font-size: 12px; border: 1px solid #333; }
        .status-item { padding: 4px 0; border-bottom: 1px solid #222; display: flex; justify-content: space-between; }
        .status-ready { color: var(--success); }

        @keyframes spin { to { transform: rotate(360deg); } }
    </style>
</head>
<body>

<!-- 置顶渲染进度条 -->
<div id="renderProgressOverlay" class="render-progress-overlay">
    <div class="render-progress-text">
        <span id="renderProgressLabel">🎬 渲染中...</span>
        <span id="renderProgressPercent">0%</span>
    </div>
    <div class="render-progress-bar">
        <div id="renderProgressFill" class="render-progress-fill"></div>
    </div>
    <div class="render-progress-text">
        <span id="renderProgressDetail">准备中...</span>
        <span id="renderProgressETA">预计剩余: --</span>
    </div>
</div>

<div class="header">
    <h1>🎬 智能配音剪辑器 <span style="font-size:12px; opacity:0.6;">v5.0 WebCodecs</span></h1>
    <div style="font-size: 12px; color: #aaa;">支持浏览器本地渲染 / FFmpeg 后端渲染</div>
</div>

<div class="container">
    <div class="video-wrapper">
        <div class="video-section">
            <video id="mainVideo" playsinline onclick="togglePlayPause()"></video>
            <div class="subtitle-overlay"><div id="subtitle" class="subtitle-text"></div></div>
            
            <!-- 加载遮罩 -->
            <div id="loader" class="loading-overlay">
                <div class="spinner"></div>
                <div id="loaderTitle" style="font-size:16px; font-weight:bold;">处理中...</div>
                <div id="loaderMsg" class="progress-text">0%</div>
            </div>
        </div>
        
        <!-- 进度条 -->
        <div class="progress-bar-container">
            <span class="time-display" id="currentTime">00:00 / 00:00</span>
            <input type="range" class="progress-slider" id="progressSlider" min="0" max="100" value="0" step="0.1">
        </div>
        
        <div class="player-controls">
            <button class="ctrl-btn reset" onclick="resetProject()">↺ 重置</button>
            <div style="width:1px; height:20px; background:#555; margin:0 5px;"></div>
            <button class="ctrl-btn" onclick="seek(-10)">⏪</button>
            <button class="ctrl-btn main" id="playPauseBtn" onclick="togglePlayPause()">▶ 开始预览</button>
            <button class="ctrl-btn" onclick="seek(10)">⏩</button>
            <div style="width:1px; height:20px; background:#555; margin:0 5px;"></div>
            <select id="renderMode" style="background:#374151; border:1px solid #4b5563; color:#fff; padding:6px 10px; border-radius:6px; font-size:12px;">
                <option value="webcodecs" selected>🌐 浏览器渲染</option>
                <option value="ffmpeg">🖥️ FFmpeg渲染</option>
                <option value="cli">💻 CLI渲染</option>
            </select>
            <select id="renderResolution" style="background:#374151; border:1px solid #4b5563; color:#fff; padding:6px 10px; border-radius:6px; font-size:12px;">
                <option value="native">原生分辨率</option>
                <option value="360p">360p快速</option>
            </select>
            <button class="ctrl-btn render" onclick="startRender()">🎥 渲染导出</button>
            <button class="ctrl-btn" style="background:#10b981;" onclick="exportProject()">📁 导出工程</button>
        </div>
    </div>

    <div class="sidebar">
        <div>
            <h2>1. 视频源</h2>
            <input type="file" id="videoInput" accept="video/*">
        </div>
        
        <div>
            <h2>2. 脚本 (JSON)</h2>
            <textarea id="scriptInput"></textarea>
        </div>

        <div>
            <h2>3. 语音配置</h2>
            <div style="display:grid; grid-template-columns: 1fr 1fr; gap:10px; margin-bottom:10px;">
                <select id="voiceSelect">
                    <option value="zh-CN-XiaoxiaoNeural">晓晓 (女声)</option>
                    <option value="zh-CN-YunxiNeural">云希 (男声)</option>
                </select>
                <select id="rateSelect">
                    <option value="+0%">原速</option>
                    <option value="+25%">1.25x</option>
                    <option value="+50%">1.5x</option>
                </select>
            </div>
            <select id="fontWeightSelect" onchange="updateSubtitleStyle()">
                <option value="900" selected>字幕: 粗黑 (900)</option>
                <option value="700">字幕: 粗体 (700)</option>
            </select>
        </div>

        <div style="display:flex; flex-direction:column; flex:1;">
            <h2>生成队列</h2>
            <div class="status-list" id="statusList"></div>
        </div>
    </div>
</div>

<script>
    // --- 状态 ---
    const PREFETCH_LIMIT = 3;
    let scriptData = [];
    let audioCache = new Map(); // index -> { blob, url, text }
    let isGenerating = false;
    let isRunning = false;      
    let isPaused = false;       
    let abortController = null;
    let currentSceneIndex = 0;
    let currentAudioObj = null; 
    let selectedVideoFile = null;

    // --- IndexedDB 音频缓存 ---
    const DB_NAME = 'TTSAudioCache';
    const DB_VERSION = 1;
    const STORE_NAME = 'audios';
    let dbInstance = null;

    async function openDB() {
        if (dbInstance) return dbInstance;
        return new Promise((resolve, reject) => {
            const request = indexedDB.open(DB_NAME, DB_VERSION);
            request.onerror = () => reject(request.error);
            request.onsuccess = () => { dbInstance = request.result; resolve(dbInstance); };
            request.onupgradeneeded = (e) => {
                const db = e.target.result;
                if (!db.objectStoreNames.contains(STORE_NAME)) {
                    db.createObjectStore(STORE_NAME, { keyPath: 'key' });
                }
            };
        });
    }

    function getCacheKey(text, voice, rate) {
        return `${voice}|${rate}|${text.substring(0, 100)}`;
    }

    async function getCachedAudio(key) {
        try {
            const db = await openDB();
            return new Promise((resolve) => {
                const tx = db.transaction(STORE_NAME, 'readonly');
                const store = tx.objectStore(STORE_NAME);
                const request = store.get(key);
                request.onsuccess = () => resolve(request.result?.blob || null);
                request.onerror = () => resolve(null);
            });
        } catch { return null; }
    }

    async function setCachedAudio(key, blob) {
        try {
            const db = await openDB();
            return new Promise((resolve) => {
                const tx = db.transaction(STORE_NAME, 'readwrite');
                const store = tx.objectStore(STORE_NAME);
                store.put({ key, blob, timestamp: Date.now() });
                tx.oncomplete = () => resolve(true);
                tx.onerror = () => resolve(false);
            });
        } catch { return false; }
    }

    // --- DOM ---
    const video = document.getElementById('mainVideo');
    const subtitleEl = document.getElementById('subtitle');
    const statusList = document.getElementById('statusList');
    const loader = document.getElementById('loader');
    const loaderTitle = document.getElementById('loaderTitle');
    const loaderMsg = document.getElementById('loaderMsg');
    const playPauseBtn = document.getElementById('playPauseBtn');
    const progressSlider = document.getElementById('progressSlider');
    const currentTimeDisplay = document.getElementById('currentTime');
    
    // --- 进度条控制 (基于脚本时长) ---
    let isSeeking = false;
    let scriptTotalDuration = 0;  // 脚本总时长
    let scriptCurrentTime = 0;    // 当前播放位置
    let segmentStartTimes = [];   // 每个片段的开始时间点
    
    function formatTime(seconds) {
        const m = Math.floor(seconds / 60);
        const s = Math.floor(seconds % 60);
        return `${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
    }
    
    function calculateScriptDuration() {
        scriptTotalDuration = 0;
        segmentStartTimes = [];
        for (let i = 0; i < scriptData.length; i++) {
            segmentStartTimes.push(scriptTotalDuration);
            const scene = scriptData[i];
            const start = parseTime(scene.time_start);
            const end = parseTime(scene.time_end);
            scriptTotalDuration += (end - start);
        }
        updateProgressDisplay();
    }
    
    function updateProgressDisplay() {
        if (scriptTotalDuration <= 0 || isSeeking) return;
        const progress = (scriptCurrentTime / scriptTotalDuration) * 100;
        progressSlider.value = progress;
        progressSlider.style.setProperty('--progress', progress + '%');
        currentTimeDisplay.textContent = `${formatTime(scriptCurrentTime)} / ${formatTime(scriptTotalDuration)}`;
    }
    
    function updateProgressBar() {
        // 这个函数不再使用video时间，改用脚本时间计算
        updateProgressDisplay();
    }
    
    // 根据脚本时间找到对应的片段和片段内位置
    function findSegmentByScriptTime(time) {
        for (let i = scriptData.length - 1; i >= 0; i--) {
            if (time >= segmentStartTimes[i]) {
                const scene = scriptData[i];
                const segDuration = parseTime(scene.time_end) - parseTime(scene.time_start);
                const timeInSegment = Math.min(time - segmentStartTimes[i], segDuration);
                return { index: i, timeInSegment, videoTime: parseTime(scene.time_start) + timeInSegment };
            }
        }
        return { index: 0, timeInSegment: 0, videoTime: parseTime(scriptData[0]?.time_start || '0:0') };
    }
    
    let wasPlaying = false;
    
    progressSlider.addEventListener('mousedown', () => {
        wasPlaying = isRunning && !isPaused;
        if (wasPlaying) {
            isPaused = true;
            video.pause();
            if (currentAudioObj) currentAudioObj.pause();
        }
    });
    
    progressSlider.addEventListener('input', (e) => {
        isSeeking = true;
        if (scriptTotalDuration <= 0 || scriptData.length === 0) return;
        const newTime = (e.target.value / 100) * scriptTotalDuration;
        scriptCurrentTime = newTime;
        currentTimeDisplay.textContent = `${formatTime(newTime)} / ${formatTime(scriptTotalDuration)}`;
        progressSlider.style.setProperty('--progress', e.target.value + '%');
        
        // 跳转视频到对应位置
        const seg = findSegmentByScriptTime(newTime);
        video.currentTime = seg.videoTime;
        
        // 同步更新字幕
        if (scriptData[seg.index]) {
            subtitleEl.innerText = scriptData[seg.index].voiceover;
            subtitleEl.style.opacity = 1;
        }
        
        console.log('Seek:', newTime, '-> segment', seg.index, 'videoTime', seg.videoTime);
    });
    
    progressSlider.addEventListener('change', (e) => {
        if (scriptTotalDuration <= 0) {
            isSeeking = false;
            return;
        }
        const newTime = (e.target.value / 100) * scriptTotalDuration;
        scriptCurrentTime = newTime;
        const seg = findSegmentByScriptTime(newTime);
        
        isSeeking = false;
        
        // 如果之前在播放，跳转到对应片段继续
        if (wasPlaying && isRunning) {
            currentSceneIndex = seg.index;
            isPaused = false;
            // 从当前片段重新开始播放，并传入偏移量
            playScene(seg.index, seg.timeInSegment);
        } else if (isRunning) {
            // 暂停状态下只定位
            currentSceneIndex = seg.index;
            playScene(seg.index, seg.timeInSegment, true); // true = 仅定位不播放
        }
    });

    // 默认脚本
    const defaultScript = {
        "script_content": [
            { "scenes": [
                { "time_start": "00:00", "time_end": "00:05", "voiceover": "这是一个测试渲染的脚本。点击紫色按钮导出。" },
                { "time_start": "00:05", "time_end": "00:10", "voiceover": "系统会确保所有语音先生成完毕，然后调用 FFmpeg 合并。" },
                { "time_start": "00:10", "time_end": "00:15", "voiceover": "最后您将下载到一个包含字幕硬烧的 MP4 文件。" }
            ]}
        ]
    };
    document.getElementById('scriptInput').value = JSON.stringify(defaultScript, null, 2);

    // ========== 智能脚本解析器 ==========
    function parseScriptSmart(inputText) {
        const result = { scenes: [], warnings: [], errors: [] };
        let raw = null;
        
        // Step 1: 尝试解析JSON
        try {
            raw = JSON.parse(inputText);
        } catch (jsonError) {
            // JSON解析失败，尝试修复常见问题
            let fixed = inputText
                .replace(/,\s*}/g, '}')  // 移除尾随逗号
                .replace(/,\s*]/g, ']')  // 移除数组尾随逗号
                .replace(/'/g, '"')       // 单引号转双引号
                .replace(/(\w+):/g, '"$1":'); // 无引号的key
            
            try {
                raw = JSON.parse(fixed);
                result.warnings.push('JSON格式已自动修复（尾随逗号/引号问题）');
            } catch (e2) {
                result.errors.push(`JSON解析失败: ${jsonError.message}`);
                // 尝试提取行号
                const match = jsonError.message.match(/position (\d+)/);
                if (match) {
                    const pos = parseInt(match[1]);
                    const lines = inputText.substring(0, pos).split('\n');
                    result.errors.push(`错误位置: 第 ${lines.length} 行，第 ${lines[lines.length-1].length + 1} 列`);
                    result.errors.push(`问题附近: "${inputText.substring(Math.max(0, pos-20), pos+20)}"`);
                }
                return result;
            }
        }
        
        // Step 2: 智能提取scenes数组
        let scenesArray = [];
        
        // 格式1: { script_content: [ { scenes: [...] } ] }
        if (raw.script_content && Array.isArray(raw.script_content)) {
            raw.script_content.forEach((part, partIdx) => {
                if (part.scenes && Array.isArray(part.scenes)) {
                    scenesArray.push(...part.scenes);
                } else if (Array.isArray(part)) {
                    scenesArray.push(...part);
                }
            });
        }
        // 格式2: { scenes: [...] }
        else if (raw.scenes && Array.isArray(raw.scenes)) {
            scenesArray = raw.scenes;
        }
        // 格式3: { script: [...] }
        else if (raw.script && Array.isArray(raw.script)) {
            scenesArray = raw.script;
        }
        // 格式4: 直接是数组 [...]
        else if (Array.isArray(raw)) {
            scenesArray = raw;
        }
        // 格式5: 单个对象 { time_start, time_end, voiceover }
        else if (raw.time_start && raw.voiceover) {
            scenesArray = [raw];
        }
        else {
            result.errors.push('无法识别脚本格式。支持的格式：');
            result.errors.push('  1. { "script_content": [{ "scenes": [...] }] }');
            result.errors.push('  2. { "scenes": [...] }');
            result.errors.push('  3. { "script": [...] }');
            result.errors.push('  4. [ {...}, {...} ]');
            return result;
        }
        
        // Step 3: 验证并修复每个scene
        let lastEndTime = 0;
        scenesArray.forEach((scene, idx) => {
            const sceneNum = idx + 1;
            const issues = [];
            let fixedScene = { ...scene };
            
            // 检查必要字段
            const hasStart = scene.time_start !== undefined;
            const hasEnd = scene.time_end !== undefined;
            const hasVoiceover = scene.voiceover !== undefined || scene.text !== undefined || scene.content !== undefined;
            
            // 修复voiceover字段名
            if (!scene.voiceover) {
                if (scene.text) { fixedScene.voiceover = scene.text; issues.push('使用 text 作为 voiceover'); }
                else if (scene.content) { fixedScene.voiceover = scene.content; issues.push('使用 content 作为 voiceover'); }
                else if (scene.subtitle) { fixedScene.voiceover = scene.subtitle; issues.push('使用 subtitle 作为 voiceover'); }
            }
            
            // 修复时间字段名
            if (!scene.time_start) {
                if (scene.start) { fixedScene.time_start = scene.start; issues.push('使用 start 作为 time_start'); }
                else if (scene.begin) { fixedScene.time_start = scene.begin; issues.push('使用 begin 作为 time_start'); }
                else if (scene.startTime) { fixedScene.time_start = scene.startTime; }
            }
            if (!scene.time_end) {
                if (scene.end) { fixedScene.time_end = scene.end; issues.push('使用 end 作为 time_end'); }
                else if (scene.endTime) { fixedScene.time_end = scene.endTime; }
            }
            
            // 验证最终结果
            if (!fixedScene.time_start) {
                // 自动生成开始时间
                fixedScene.time_start = formatTimeForScript(lastEndTime);
                issues.push(`缺少 time_start，自动设为 ${fixedScene.time_start}`);
            }
            if (!fixedScene.time_end) {
                // 自动生成结束时间（开始时间 + 5秒）
                const startSec = parseTime(fixedScene.time_start);
                fixedScene.time_end = formatTimeForScript(startSec + 5);
                issues.push(`缺少 time_end，自动设为 ${fixedScene.time_end}`);
            }
            if (!fixedScene.voiceover) {
                issues.push('缺少 voiceover 文本，已跳过');
                result.warnings.push(`片段 ${sceneNum}: ${issues.join('; ')}`);
                return; // 跳过这个scene
            }
            
            // 验证时间格式
            try {
                const startSec = parseTime(fixedScene.time_start);
                const endSec = parseTime(fixedScene.time_end);
                if (endSec <= startSec) {
                    issues.push(`结束时间 <= 开始时间，自动修正`);
                    fixedScene.time_end = formatTimeForScript(startSec + 5);
                }
                lastEndTime = parseTime(fixedScene.time_end);
            } catch (e) {
                issues.push(`时间格式错误: ${e.message}`);
            }
            
            // 清理voiceover文本
            if (typeof fixedScene.voiceover === 'string') {
                fixedScene.voiceover = fixedScene.voiceover.trim();
                if (fixedScene.voiceover.length === 0) {
                    issues.push('voiceover 为空，已跳过');
                    result.warnings.push(`片段 ${sceneNum}: ${issues.join('; ')}`);
                    return;
                }
            }
            
            // 添加到结果
            result.scenes.push(fixedScene);
            if (issues.length > 0) {
                result.warnings.push(`片段 ${sceneNum}: ${issues.join('; ')}`);
            }
        });
        
        if (result.scenes.length === 0 && scenesArray.length > 0) {
            result.errors.push(`所有 ${scenesArray.length} 个片段都无法使用`);
        }
        
        return result;
    }
    
    function formatTimeForScript(seconds) {
        const m = Math.floor(seconds / 60);
        const s = Math.floor(seconds % 60);
        return `${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
    }
    
    // 显示解析结果的函数
    function showParseResult(result) {
        let msg = '';
        if (result.errors.length > 0) {
            msg += '❌ 错误:\\n' + result.errors.join('\\n') + '\\n\\n';
        }
        if (result.warnings.length > 0) {
            msg += '⚠️ 警告 (已自动修复):\\n' + result.warnings.join('\\n') + '\\n\\n';
        }
        if (result.scenes.length > 0) {
            msg += `✅ 成功解析 ${result.scenes.length} 个片段`;
        }
        return msg;
    }

    // 文件选择
    document.getElementById('videoInput').addEventListener('change', (e) => {
        const file = e.target.files[0];
        if (file) {
            selectedVideoFile = file;
            video.src = URL.createObjectURL(file);
            video.load();
        }
    });

    function updateSubtitleStyle() {
        subtitleEl.style.fontWeight = document.getElementById('fontWeightSelect').value;
    }

    // --- 导出工程文件 ---
    function exportProject() {
        // 使用智能解析器
        const inputText = document.getElementById('scriptInput').value;
        const parseResult = parseScriptSmart(inputText);
        
        if (parseResult.errors.length > 0) {
            alert(showParseResult(parseResult));
            return;
        }
        
        let currentScript = parseResult.scenes;
        if (parseResult.warnings.length > 0) {
            console.warn("解析警告:", parseResult.warnings);
        }
        
        if (currentScript.length === 0) {
            alert("脚本为空，无法导出");
            return;
        }
        
        // 构建工程文件
        const project = {
            video_path: selectedVideoFile ? selectedVideoFile.name : "请填入视频绝对路径",
            voice: document.getElementById('voiceSelect').value,
            rate: document.getElementById('rateSelect').value,
            script: currentScript
        };
        
        // 下载 JSON
        const blob = new Blob([JSON.stringify(project, null, 2)], {type: 'application/json'});
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = 'project.json';
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
        
        alert("工程文件已导出！\\n\\n请注意：video_path 需要手动修改为视频的绝对路径。\\n\\n使用方法：python app.py --render project.json");
    }
    
    // ========== 渲染入口 ==========
    function startRender() {
        const mode = document.getElementById('renderMode').value;
        if (mode === 'webcodecs') {
            startWebCodecsRender();
        } else if (mode === 'cli') {
            startCLIRender();
        } else {
            startRenderExport(); // FFmpeg 服务器模式
        }
    }
    
    // ========== CLI 渲染模式 ==========
    async function startCLIRender() {
        if (!selectedVideoFile) { alert("请先上传视频文件！"); return; }
        
        // 使用智能解析器
        const inputText = document.getElementById('scriptInput').value;
        const parseResult = parseScriptSmart(inputText);
        
        if (parseResult.errors.length > 0) {
            alert(showParseResult(parseResult));
            return;
        }
        
        let currentScript = parseResult.scenes;
        if (parseResult.warnings.length > 0) {
            console.warn("解析警告:", parseResult.warnings);
        }
        
        if (currentScript.length === 0) {
            alert("脚本为空，无法导出");
            return;
        }
        
        // 2. 生成所有TTS音频（确保缓存）
        showRenderProgress(true, "准备CLI渲染...", "生成TTS音频");
        try {
            for (let i = 0; i < currentScript.length; i++) {
                if (!audioCache.has(i)) {
                    updateRenderProgress((i / currentScript.length) * 50, `生成语音 ${i+1}/${currentScript.length}`, "");
                    const blob = await fetchTTS(currentScript[i].voiceover);
                    audioCache.set(i, { url: URL.createObjectURL(blob), blob, text: currentScript[i].voiceover });
                }
            }
        } catch (e) {
            showRenderProgress(false);
            alert("语音生成失败: " + e.message);
            return;
        }
        
        // 3. 构建工程文件
        const resolution = document.getElementById('renderResolution').value;
        const project = {
            video_path: "请填入视频绝对路径",
            voice: document.getElementById('voiceSelect').value,
            rate: document.getElementById('rateSelect').value,
            resolution: resolution,  // 'native' 或 '360p'
            script: currentScript
        };
        
        // 4. 下载工程文件
        updateRenderProgress(60, "导出工程文件...", "");
        const projectBlob = new Blob([JSON.stringify(project, null, 2)], {type: 'application/json'});
        const projectUrl = URL.createObjectURL(projectBlob);
        const a1 = document.createElement('a');
        a1.href = projectUrl;
        a1.download = 'project.json';
        document.body.appendChild(a1);
        a1.click();
        document.body.removeChild(a1);
        URL.revokeObjectURL(projectUrl);
        
        // 5. 下载所有音频文件为ZIP（简化：逐个下载）
        updateRenderProgress(70, "导出音频文件...", "");
        for (let i = 0; i < currentScript.length; i++) {
            const cached = audioCache.get(i);
            if (cached) {
                const audioUrl = URL.createObjectURL(cached.blob);
                const a2 = document.createElement('a');
                a2.href = audioUrl;
                a2.download = `audio_${i}.mp3`;
                document.body.appendChild(a2);
                a2.click();
                document.body.removeChild(a2);
                URL.revokeObjectURL(audioUrl);
                await new Promise(r => setTimeout(r, 200)); // 避免浏览器阻止多次下载
            }
        }
        
        // 6. 显示CLI命令
        updateRenderProgress(100, "完成！", "");
        const cliCommand = `python app.py --render project.json`;
        
        // 复制到剪贴板
        try {
            await navigator.clipboard.writeText(cliCommand);
        } catch {}
        
        showRenderProgress(false);
        alert(`CLI渲染文件已导出！\\n\\n步骤：\\n1. 将下载的 project.json 中 video_path 改为视频绝对路径\\n2. 确保音频文件 audio_*.mp3 在同一目录\\n3. 运行命令：\\n\\n${cliCommand}\\n\\n(命令已复制到剪贴板)`);
    }
    
    // ========== WebCodecs 浏览器渲染器 (优化版) ==========
    async function startWebCodecsRender() {
        if (!selectedVideoFile) { alert("请先上传视频文件！"); return; }
        
        // 检查 WebCodecs 支持
        if (!('VideoEncoder' in window) || !('AudioEncoder' in window)) {
            alert("您的浏览器不支持 WebCodecs API！\\n请使用最新版 Chrome/Edge，或切换到 FFmpeg 模式。");
            return;
        }
        
        // 使用智能解析器
        const inputText = document.getElementById('scriptInput').value;
        const parseResult = parseScriptSmart(inputText);
        
        if (parseResult.errors.length > 0) {
            alert(showParseResult(parseResult));
            return;
        }
        
        scriptData = parseResult.scenes;
        if (parseResult.warnings.length > 0) {
            console.warn("解析警告:", parseResult.warnings);
        }
        
        if (scriptData.length === 0) { alert("脚本为空"); return; }
        
        // 2. 生成所有音频并解码
        showRenderProgress(true, "准备中...", "解码音频数据");
        const audioBuffers = [];
        const audioContext = new AudioContext();
        
        try {
            for (let i = 0; i < scriptData.length; i++) {
                if (!audioCache.has(i)) {
                    updateRenderProgress((i / scriptData.length) * 10, `生成语音 ${i+1}/${scriptData.length}`, "");
                    const blob = await fetchTTS(scriptData[i].voiceover);
                    audioCache.set(i, { url: URL.createObjectURL(blob), blob, text: scriptData[i].voiceover });
                }
                // 解码音频数据用于混合
                const cached = audioCache.get(i);
                const arrayBuffer = await cached.blob.arrayBuffer();
                const audioBuffer = await audioContext.decodeAudioData(arrayBuffer);
                audioBuffers.push(audioBuffer);
            }
        } catch (e) {
            showRenderProgress(false);
            alert("音频处理失败: " + e.message);
            return;
        }
        
        // 3. 获取分辨率
        const resolution = document.getElementById('renderResolution').value;
        let targetWidth, targetHeight;
        if (resolution === '360p') {
            targetWidth = 640; targetHeight = 360;
        } else {
            targetWidth = video.videoWidth || 1280;
            targetHeight = video.videoHeight || 720;
        }
        
        // 4. 计算参数
        const fps = 30;
        const sampleRate = 44100;
        const numberOfChannels = 2;
        let totalDuration = 0;
        const segmentInfos = [];
        
        scriptData.forEach((s, idx) => {
            const start = parseTime(s.time_start);
            const end = parseTime(s.time_end);
            const duration = end - start;
            segmentInfos.push({ start, end, duration, audioBuffer: audioBuffers[idx] });
            totalDuration += duration;
        });
        const totalFrames = Math.ceil(totalDuration * fps);
        
        updateRenderProgress(10, "初始化编码器...", `共 ${totalFrames} 帧`);
        
        // 5. 创建 Muxer
        let muxer;
        try {
            muxer = new Mp4Muxer.Muxer({
                target: new Mp4Muxer.ArrayBufferTarget(),
                video: {
                    codec: 'avc',
                    width: targetWidth,
                    height: targetHeight
                },
                audio: {
                    codec: 'aac',
                    numberOfChannels: numberOfChannels,
                    sampleRate: sampleRate
                },
                fastStart: 'in-memory'
            });
        } catch (e) {
            showRenderProgress(false);
            alert("创建 MP4 Muxer 失败: " + e.message);
            return;
        }
        
        // 6. 创建 Encoders
        const videoEncoder = new VideoEncoder({
            output: (chunk, meta) => muxer.addVideoChunk(chunk, meta),
            error: (e) => console.error('VideoEncoder error:', e)
        });
        
        videoEncoder.configure({
            codec: 'avc1.42001f',
            width: targetWidth,
            height: targetHeight,
            bitrate: resolution === '360p' ? 1_000_000 : 8_000_000,
            framerate: fps
        });

        const audioEncoder = new AudioEncoder({
            output: (chunk, meta) => muxer.addAudioChunk(chunk, meta),
            error: (e) => console.error('AudioEncoder error:', e)
        });

        audioEncoder.configure({
            codec: 'mp4a.40.2',
            numberOfChannels: numberOfChannels,
            sampleRate: sampleRate,
            bitrate: 128_000
        });
        
        // 7. 渲染循环
        const renderStartTime = Date.now();
        let globalFrameIndex = 0;
        
        // 用于绘制字幕的 Canvas
        const canvas = document.createElement('canvas');
        canvas.width = targetWidth;
        canvas.height = targetHeight;
        const ctx = canvas.getContext('2d');
        
        // 视频静音，我们自己处理音频
        video.muted = true;
        
        for (let i = 0; i < segmentInfos.length; i++) {
            const seg = segmentInfos[i];
            const segFrames = Math.ceil(seg.duration * fps);
            
            // --- 音频处理 ---
            // 创建该片段的音频数据
            // 简单处理：如果音频比视频短，补静音；如果长，截断（或加速，这里先做简单截断）
            // 更好的做法是 time-stretch，但 WebAudioAPI 在离线模式下做 time-stretch 比较复杂
            // 这里我们采用：如果音频长，则截断；如果短，保持原样（尾部自动静音？）
            // 实际上我们需要把 audioBuffer 重新采样并编码
            
            // 为了简化，我们按片段编码音频
            // 计算需要的 PCM 数据长度
            const totalSamples = Math.ceil(seg.duration * sampleRate);
            const audioData = new Float32Array(totalSamples * numberOfChannels);
            
            // 填充音频数据
            const ab = seg.audioBuffer;
            if (ab) {
                // 简单的重采样/填充逻辑
                // 这里假设采样率匹配，如果不匹配需要重采样（WebAudioContext decode已经帮我们重采样到环境采样率了，但我们需要 44100）
                // 暂时假设 audioContext.sampleRate 和我们目标 sampleRate 一致，或者接近
                // 实际生产需要做重采样，这里简化直接拷贝
                for (let ch = 0; ch < numberOfChannels; ch++) {
                    const chData = ab.getChannelData(Math.min(ch, ab.numberOfChannels - 1));
                    // 考虑倍速：如果音频太长，需要按比例丢弃样本；如果短，补0
                    // 现在的逻辑是：视频画面决定时长，音频播放对应时长
                    // 计算播放倍率
                    let rate = 1.0;
                    if (ab.duration > seg.duration + 0.1) {
                         rate = ab.duration / seg.duration; // 需要加速
                    }
                    
                    // 填充 output buffer
                    for (let s = 0; s < totalSamples; s++) {
                         // 映射到源音频的样本索引
                         const srcIdx = Math.floor(s * rate);
                         if (srcIdx < chData.length) {
                             audioData[s * numberOfChannels + ch] = chData[srcIdx];
                         } else {
                             audioData[s * numberOfChannels + ch] = 0;
                         }
                    }
                }
            }
            
            // 创建 AudioData 并编码
            // AudioEncoder 需要特定的 chunk size，通常是 1024 的倍数框架会自动处理？
            // VideoEncoder/AudioEncoder 都接受 Data 对象
            // 我们将整个片段的音频切分成小块送入 Encoder
            const chunkSize = 44100; // 1秒一块
            for(let offset = 0; offset < totalSamples; offset += chunkSize) {
                 const size = Math.min(chunkSize, totalSamples - offset);
                 const chunkData = new Float32Array(size * numberOfChannels);
                 // 复制数据
                 for(let k=0; k<size * numberOfChannels; k++) {
                     chunkData[k] = audioData[offset * numberOfChannels + k];
                 }
                 
                 // 重新构建 Planar 数据
                 const planarData = new Float32Array(size * numberOfChannels);
                 for(let s=0; s<size; s++) {
                     for(let c=0; c<numberOfChannels; c++) {
                         planarData[c * size + s] = audioData[(offset + s) * numberOfChannels + c];
                     }
                 }
                 
                 const audioFrame = new AudioData({
                    format: 'f32-planar',
                    numberOfChannels: numberOfChannels,
                    numberOfFrames: size,
                    sampleRate: sampleRate,
                    timestamp: Math.round((globalFrameIndex / fps * 1_000_000) + (offset / sampleRate * 1_000_000)),
                    data: planarData
                 });
                 
                 audioEncoder.encode(audioFrame);
                 audioFrame.close();
            }
            
            // --- 视频处理 ---
            // 预加载 seek 优化
            // 使用 requestVideoFrameCallback 并不是 seek 的替代，我们还是需要 seek
            // 优化：只有当时间跨度大时才等待 seeked，连续帧通常很快
            
            video.currentTime = seg.start;
            // 初始 seek
             await new Promise(r => {
                const onSeeked = () => { video.removeEventListener('seeked', onSeeked); r(); };
                video.addEventListener('seeked', onSeeked);
            });
            
            for (let f = 0; f < segFrames; f++) {
                const videoTime = seg.start + (f / fps);
                // 只有当时间变动超过阈值才设置 currentTime (浏览器自己会插值?)
                // 不，我们需要精确帧。为了速度，我们可以容忍少量时间误差? 为了质量不行。
                // 技巧：不要每次都 seek，而是播放 video，然后抓取？
                // 不行，播放速度不可控。
                // 方法：设置 currentTime，如果 gap 很小，浏览器可能不需要触发 seeked
                
                if (Math.abs(video.currentTime - videoTime) > 0.001) {
                    video.currentTime = videoTime;
                }
                
                // 快速等待
                // 在Chrome中，设置currentTime后，数据不一定立即更新。
                // 但对于本地文件，通常很快。
                // 我们用一个小技巧：等待 video.readyState >= 2
                while (video.readyState < 2) {
                    await new Promise(r => setTimeout(r, 10));
                }
                
                // 绘制到 Canvas 以便添加字幕
                ctx.drawImage(video, 0, 0, targetWidth, targetHeight);
                
                // 绘制字幕
                ctx.font = `bold ${Math.floor(targetHeight / 15)}px "Noto Sans SC", sans-serif`;
                ctx.textAlign = 'center';
                ctx.fillStyle = 'white';
                ctx.strokeStyle = 'black';
                ctx.lineWidth = 4;
                ctx.lineJoin = 'round';
                const text = scriptData[i].voiceover;
                const textY = targetHeight - targetHeight * 0.1;
                ctx.strokeText(text, targetWidth / 2, textY);
                ctx.fillText(text, targetWidth / 2, textY);
                
                // 从 Canvas 创建 VideoFrame
                const frame = new VideoFrame(canvas, {
                    timestamp: globalFrameIndex * (1_000_000 / fps)
                });
                
                videoEncoder.encode(frame);
                frame.close(); // 必须关闭以释放显存
                
                globalFrameIndex++;
                
                // 每10帧更新一次UI，避免卡顿
                if (globalFrameIndex % 10 === 0) {
                     const progress = 10 + (globalFrameIndex / totalFrames) * 85;
                     const elapsed = (Date.now() - renderStartTime) / 1000;
                     const speed = globalFrameIndex / elapsed;
                     const remaining = (totalFrames - globalFrameIndex) / speed;
                     updateRenderProgress(progress, `渲染帧 ${globalFrameIndex}/${totalFrames} (${Math.round(speed)} fps)`, `片段 ${i+1}/${scriptData.length}`, remaining);
                     // 让出主线程
                     await new Promise(r => setTimeout(r, 0));
                }
            }
        }
        
        // 9. 完成编码
        updateRenderProgress(96, "完成编码...", "");
        await videoEncoder.flush();
        await audioEncoder.flush();
        videoEncoder.close();
        audioEncoder.close();
        
        // 10. 生成文件
        updateRenderProgress(99, "打包文件...", "");
        muxer.finalize();
        
        const buffer = muxer.target.buffer;
        const outputBlob = new Blob([buffer], { type: 'video/mp4' });
        
        // 11. 下载
        const downloadUrl = URL.createObjectURL(outputBlob);
        const a = document.createElement('a');
        a.href = downloadUrl;
        a.download = 'rendered_video.mp4';
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(downloadUrl);
        
        updateRenderProgress(100, "渲染完成！", "");
        setTimeout(() => showRenderProgress(false), 2000);
    }
    
    // ========== 渲染进度显示 ==========
    function showRenderProgress(show, label = "", detail = "") {
        const overlay = document.getElementById('renderProgressOverlay');
        overlay.style.display = show ? 'flex' : 'none';
        if (show) {
            document.getElementById('renderProgressLabel').innerText = label;
            document.getElementById('renderProgressDetail').innerText = detail;
            document.getElementById('renderProgressPercent').innerText = '0%';
            document.getElementById('renderProgressFill').style.width = '0%';
            document.getElementById('renderProgressETA').innerText = '预计剩余: --';
        }
    }
    
    function updateRenderProgress(percent, label, detail, etaSeconds) {
        document.getElementById('renderProgressPercent').innerText = Math.round(percent) + '%';
        document.getElementById('renderProgressFill').style.width = percent + '%';
        if (label) document.getElementById('renderProgressLabel').innerText = '🎬 ' + label;
        if (detail) document.getElementById('renderProgressDetail').innerText = detail;
        if (etaSeconds !== undefined && etaSeconds > 0) {
            const mins = Math.floor(etaSeconds / 60);
            const secs = Math.floor(etaSeconds % 60);
            document.getElementById('renderProgressETA').innerText = `预计剩余: ${mins}分${secs}秒`;
        }
        console.log(`[渲染] ${Math.round(percent)}% - ${label} - ${detail}`);
    }

    // ========== FFmpeg 服务器渲染 ==========
    async function startRenderExport() {
        if (!selectedVideoFile) { alert("请先上传视频文件！"); return; }
        
        // 使用智能解析器
        const inputText = document.getElementById('scriptInput').value;
        const parseResult = parseScriptSmart(inputText);
        
        if (parseResult.errors.length > 0) {
            alert(showParseResult(parseResult));
            return;
        }
        
        scriptData = parseResult.scenes;
        if (parseResult.warnings.length > 0) {
            console.warn("解析警告:", parseResult.warnings);
        }

        showLoader(true, "准备渲染素材...", "正在检查语音完整性");

        // 2. 强制检查并生成所有音频
        try {
            for (let i = 0; i < scriptData.length; i++) {
                if (!audioCache.has(i)) {
                    loaderMsg.innerText = `正在生成语音 ${i+1}/${scriptData.length}`;
                    const scene = scriptData[i];
                    const blob = await fetchTTS(scene.voiceover);
                    const url = URL.createObjectURL(blob);
                    audioCache.set(i, { url, blob, text: scene.voiceover });
                    updateStatusItem(i, "✅ 渲染准备");
                }
            }
        } catch (e) {
            showLoader(false);
            alert("语音生成失败，请检查网络");
            return;
        }

        // 3. 打包上传到后端
        loaderTitle.innerText = "正在服务器渲染...";
        loaderMsg.innerText = "上传素材中...";
        
        // Start progress polling
        let progressInterval = setInterval(async () => {
            try {
                const pRes = await fetch('/render_progress');
                if (pRes.ok) {
                    const pData = await pRes.json();
                    loaderMsg.innerText = `${pData.step}: ${pData.detail}`;
                }
            } catch {}
        }, 500);

        const formData = new FormData();
        formData.append("video_file", selectedVideoFile);
        formData.append("script_json", JSON.stringify(scriptData));
        
        // 将所有音频按顺序加入 FormData (Map 遍历顺序通常是插入顺序，但为了保险我们按索引遍历)
        for(let i=0; i<scriptData.length; i++) { 
            if(audioCache.has(i)) {
                formData.append("audio_files", audioCache.get(i).blob, `audio_${i}.mp3`); 
            }
        }

        try {
            const res = await fetch("/render_video", {
                method: "POST",
                body: formData
            });

            if (!res.ok) throw new Error("Server Error");

            // 4. 下载文件
            const blob = await res.blob();
            const url = window.URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = "rendered_video.mp4";
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            window.URL.revokeObjectURL(url);
            
            showLoader(false);
            clearInterval(progressInterval);
            alert("渲染完成！已开始下载。");

        } catch (e) {
            showLoader(false);
            clearInterval(progressInterval);
            console.error(e);
            alert("渲染失败，请检查后端控制台日志 (需要安装 FFmpeg)");
        }
    }

    // --- 预览播放逻辑 (简化版) ---
    async function startProcess() {
        console.log("startProcess called");
        if (!video.src) { alert("请选视频"); return; }
        
        // 使用智能解析器
        const inputText = document.getElementById('scriptInput').value;
        const parseResult = parseScriptSmart(inputText);
        
        if (parseResult.errors.length > 0) {
            alert(showParseResult(parseResult));
            return;
        }
        
        scriptData = parseResult.scenes;
        console.log("Script parsed:", scriptData);
        
        if (parseResult.warnings.length > 0) {
            console.warn("解析警告:", parseResult.warnings);
        }

        isRunning = true; isPaused = false; isGenerating = true;
        scriptCurrentTime = 0;  // 重置进度
        calculateScriptDuration();  // 计算脚本总时长
        abortController = new AbortController();
        updatePlayBtnState();
        generateQueueLoop(); // 开启后台生成
        playScene(0);
    }

    function togglePlayPause() {
        if (!isRunning) startProcess();
        else {
            isPaused = !isPaused;
            updatePlayBtnState();
            if(isPaused) { video.pause(); if(currentAudioObj) currentAudioObj.pause(); }
            else { video.play(); if(currentAudioObj) currentAudioObj.play(); }
        }
    }

    async function playScene(index, startOffset = 0, pauseAfterSeek = false) {
        if (!isRunning) return;
        if (index >= scriptData.length) { stopAll(); return; }

        currentSceneIndex = index;
        const scene = scriptData[index];
        const start = parseTime(scene.time_start);
        const end = parseTime(scene.time_end);
        
        subtitleEl.innerText = scene.voiceover;
        subtitleEl.style.opacity = 1;
        updateStatusItem(index, "▶️ 预览中");

        // 设置视频位置
        const targetVideoTime = start + startOffset;
        if (Math.abs(video.currentTime - targetVideoTime) > 0.1) video.currentTime = targetVideoTime;

        // 等待音频
        let audioData = audioCache.get(index);
        while (!audioData && isRunning) {
            if(!document.getElementById('loader').style.display) {
                // simple wait visual
            }
            await new Promise(r => setTimeout(r, 200));
            audioData = audioCache.get(index);
        }
        if (!isRunning) return;

        const audio = new Audio(audioData.url);
        currentAudioObj = audio;
        
        // 简单同步逻辑
        await new Promise(r => { audio.onloadedmetadata = r; audio.load(); setTimeout(r, 500); });
        
        const vDur = end - start;
        let playbackRate = 1.0;
        if (audio.duration > vDur + 0.2) playbackRate = Math.min(audio.duration / vDur, 3.0);
        audio.playbackRate = playbackRate;

        // 设置音频位置
        if (startOffset > 0) {
            // 根据倍速调整音频位置
            // 如果音频被压缩(加快)，同样的视频时间对应的音频时间更长
            // 如果音频自然时长 < 视频时长，通常不加速，此时 startOffset 对应音频时间就是 startOffset
            // 但是这里 playbackRate 只有在音频比视频长时才 > 1.0 (加速播放)
            // 所以音频进度 = 视频进度 * (音频总长 / 视频总长) ? 不，是 视频进度 * playbackRate
            // 验证: 播放 1s, 音频应该走 playbackRate 秒
            audio.currentTime = startOffset * playbackRate;
        }

        if (pauseAfterSeek) {
            video.pause();
            audio.pause();
            isPaused = true;
            updatePlayBtnState();
            return; // 仅定位
        }

        video.play();
        try { await audio.play(); } catch(e) { console.error(e); }

        await new Promise(resolve => {
            let aDone = false, vDone = false;
            audio.onended = () => { aDone = true; check(); };
            const timeUp = () => {
                if(!isRunning) { video.removeEventListener('timeupdate', timeUp); resolve(); return; }
                // 更新脚本进度
                const elapsed = video.currentTime - start;
                scriptCurrentTime = segmentStartTimes[index] + Math.max(0, Math.min(elapsed, vDur));
                updateProgressDisplay();
                
                if(video.currentTime >= end) {
                    vDone = true;
                    if(!aDone) { video.pause(); video.currentTime = end; }
                    else check();
                }
            };
            video.addEventListener('timeupdate', timeUp);
            function check() { if(aDone && vDone) { video.removeEventListener('timeupdate', timeUp); resolve(); } }
        });
        
        playScene(index + 1);
    }

    // --- 辅助 ---
    function stopAll(manual) {
        isRunning = false; isGenerating = false; isPaused = false;
        if(abortController) abortController.abort();
        video.pause(); if(currentAudioObj) currentAudioObj.pause();
        updatePlayBtnState();
        if(manual) {
            audioCache.forEach(v => URL.revokeObjectURL(v.url));
            audioCache.clear();
            video.currentTime = 0;
            subtitleEl.innerText = "";
            statusList.innerHTML = "";
            scriptCurrentTime = 0;
            scriptTotalDuration = 0;
            currentTimeDisplay.textContent = '00:00 / 00:00';
            progressSlider.value = 0;
            progressSlider.style.setProperty('--progress', '0%');
        }
    }
    
    function resetProject() { stopAll(true); }
    
    // 基于脚本时间的seek
    function seek(off) { 
        if (scriptTotalDuration <= 0) return;
        const newTime = Math.max(0, Math.min(scriptCurrentTime + off, scriptTotalDuration));
        scriptCurrentTime = newTime;
        const seg = findSegmentByScriptTime(newTime);
        video.currentTime = seg.videoTime;
        updateProgressDisplay();
        
        if (isRunning && !isPaused) {
            currentSceneIndex = seg.index;
            playScene(seg.index, seg.timeInSegment);
        } else if (isRunning) {
             currentSceneIndex = seg.index;
             playScene(seg.index, seg.timeInSegment, true);
        }
    }
    
    function updatePlayBtnState() { playPauseBtn.innerText = isRunning ? (isPaused ? "▶ 继续" : "⏸ 暂停") : "▶ 开始预览"; }
    
    // 支持 MM:SS 和 HH:MM:SS 格式
    function parseTime(s) { 
        const p = s.split(':').map(Number); 
        if (p.length === 2) return p[0]*60 + p[1];
        if (p.length === 3) return p[0]*3600 + p[1]*60 + p[2];
        return 0;
    }
    
    // TTS with retry logic
    async function fetchTTSWithRetry(text, maxRetries = 3) {
        for (let attempt = 1; attempt <= maxRetries; attempt++) {
            try {
                return await fetchTTS(text);
            } catch (e) {
                if (e.name === 'AbortError') throw e;
                console.log(`TTS attempt ${attempt} failed, retrying...`);
                if (attempt === maxRetries) throw e;
                await new Promise(r => setTimeout(r, 1000));
            }
        }
    }
    
    async function fetchTTS(text) {
        const voice = document.getElementById('voiceSelect').value;
        const rate = document.getElementById('rateSelect').value;
        const cacheKey = getCacheKey(text, voice, rate);
        
        // 先检查 IndexedDB 缓存
        const cachedBlob = await getCachedAudio(cacheKey);
        if (cachedBlob) {
            console.log('从缓存加载音频:', text.substring(0, 20) + '...');
            return cachedBlob;
        }
        
        // 没有缓存则请求网络
        const res = await fetch('/tts', {
            method: 'POST', headers: {'Content-Type':'application/json'},
            body: JSON.stringify({text, voice, rate}),
            signal: abortController ? abortController.signal : null
        });
        if(!res.ok) throw new Error("TTS Fail");
        const blob = await res.blob();
        
        // 保存到缓存
        await setCachedAudio(cacheKey, blob);
        console.log('已缓存音频:', text.substring(0, 20) + '...');
        
        return blob;
    }
    
    // Fully parallel TTS generation - all at once
    async function generateQueueLoop() {
        const promises = [];
        
        for (let idx = 0; idx < scriptData.length; idx++) {
            if (audioCache.has(idx)) continue;
            updateStatusItem(idx, "🔄 生成中");
            
            const p = (async (i) => {
                try {
                    const blob = await fetchTTSWithRetry(scriptData[i].voiceover);
                    audioCache.set(i, { url: URL.createObjectURL(blob), blob, text: scriptData[i].voiceover });
                    updateStatusItem(i, "✅ 就绪");
                } catch(e) { 
                    if(e.name !== 'AbortError') updateStatusItem(i, "❌ 失败"); 
                }
            })(idx);
            
            promises.push(p);
        }
        
        await Promise.all(promises);
    }

    function updateStatusItem(i, status) {
        let el = document.getElementById(`st-${i}`);
        if(!el) {
            el = document.createElement('div'); el.id=`st-${i}`; el.className='status-item';
            statusList.appendChild(el);
        }
        el.innerHTML = `<span>#${i+1}</span> <span class="${status.includes('✅')?'status-ready':''}">${status}</span>`;
    }

    function showLoader(show, title, msg) {
        loader.style.display = show ? 'flex' : 'none';
        if(title) loaderTitle.innerText = title;
        if(msg) loaderMsg.innerText = msg;
    }
</script>
</body>
</html>
"""

# ==========================================
# 路由定义
# ==========================================

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML_CONTENT

@app.post("/tts")
async def generate_tts(
    text: str = Body(..., embed=True),
    voice: str = Body("zh-CN-XiaoxiaoNeural", embed=True),
    rate: str = Body("+0%", embed=True)
):
    communicate = edge_tts.Communicate(text, voice, rate=rate)
    audio_stream = io.BytesIO()
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            audio_stream.write(chunk["data"])
    audio_stream.seek(0)
    return StreamingResponse(audio_stream, media_type="audio/mpeg")


@app.post("/render_video")
async def render_video_final(
    video_file: UploadFile = File(...),
    script_json: str = Form(...),
    # 接收文件列表
    audio_files: List[UploadFile] = File(None) 
    # 注意：前端必须把所有 blob append 到 'audio_files' 这个同一个 key 下
    # 但是前端 JS 现在的逻辑是 audio_0, audio_1... 为了兼容之前的逻辑，我们需要修改前端或者后端
    # 这里修改后端去适配前端的 key pattern 比较困难，我们修改上面的 JS 代码吗？
    # 不，我们直接用 request.form() 读取
):
    # 保存视频源
    src_video_path = os.path.join(TEMP_DIR, "source_video.mp4")
    with open(src_video_path, "wb") as f:
        shutil.copyfileobj(video_file.file, f)
    
    # 解析脚本
    script_data = json.loads(script_json)
    
    # 保存音频文件到 dict: index -> path
    # 由于 UploadFile 列表顺序可能和 append 顺序一致，但为了保险，前端应该按顺序 append
    # 或者前端全部 append 到 'audio_files' 列表里
    # 我们假设前端代码修改为 formData.append("audio_files", blob)
    
    saved_audio_paths = {}
    
    # 如果前端还是 audio_0, audio_1... 我们无法通过参数直接获取，需要用 request
    # 为了保证代码能跑，我们在 HTML 里修改 JS逻辑：formData.append('audio_files', val.blob)
    # 并确保顺序
    
    if audio_files:
        for i, af in enumerate(audio_files):
            p = os.path.join(TEMP_DIR, f"upload_a_{i}.mp3")
            with open(p, "wb") as f:
                shutil.copyfileobj(af.file, f)
            saved_audio_paths[str(i)] = p
            
    # 开始 FFmpeg 处理
    try:
        final_video_path = process_render(src_video_path, script_data, saved_audio_paths)
        return FileResponse(final_video_path, filename="rendered_video.mp4", media_type="video/mp4")
    except Exception as e:
        print(f"Render Error: {e}")
        return HTMLResponse(content=f"Render Failed: {e}", status_code=500)

@app.get("/render_progress")
async def get_render_progress():
    """Return current render progress from temp file"""
    progress_file = os.path.join(TEMP_DIR, "progress.txt")
    try:
        if os.path.exists(progress_file):
            with open(progress_file, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if "|" in content:
                    step, detail = content.split("|", 1)
                    return {"step": step, "detail": detail}
        return {"step": "等待中", "detail": "准备开始..."}
    except:
        return {"step": "处理中", "detail": "..."}


# ==========================================
# CLI Mode Functions
# ==========================================

async def generate_tts_audio(text: str, voice: str, rate: str, output_path: str, max_retries: int = 3):
    """Generate TTS audio with retry logic"""
    for attempt in range(max_retries):
        try:
            communicate = edge_tts.Communicate(text, voice, rate=rate)
            await communicate.save(output_path)
            return True
        except Exception as e:
            if attempt < max_retries - 1:
                await asyncio.sleep(2)  # Wait before retry
            else:
                print(f"[TTS 错误] 生成失败: {str(e)[:50]}")
                raise

async def cli_generate_all_audio(script_data: list, voice: str, rate: str, output_dir: str):
    """Generate all TTS audio files with limited concurrency"""
    semaphore = asyncio.Semaphore(5)  # Limit to 5 concurrent requests
    
    async def generate_one(idx, scene):
        async with semaphore:
            output_path = os.path.join(output_dir, f"audio_{idx}.mp3")
            print(f"[TTS] 生成语音 {idx+1}/{len(script_data)}: {scene['voiceover'][:30]}...")
            await generate_tts_audio(scene['voiceover'], voice, rate, output_path)
    
    tasks = [generate_one(idx, scene) for idx, scene in enumerate(script_data)]
    await asyncio.gather(*tasks)
    print(f"[TTS] 所有 {len(script_data)} 个语音生成完成!")
    
    # Return paths dict
    return {str(i): os.path.join(output_dir, f"audio_{i}.mp3") for i in range(len(script_data))}

def render_from_project(project_path: str, output_path: str = None):
    """CLI: Render video from project file"""
    print(f"\n{'='*50}")
    print("智能配音剪辑器 - CLI 渲染模式")
    print(f"{'='*50}\n")
    
    # Load project
    print(f"[加载] 读取工程文件: {project_path}")
    with open(project_path, "r", encoding="utf-8") as f:
        project = json.load(f)
    
    # 智能获取视频路径
    video_path = project.get("video_path", "")
    if not video_path or not os.path.exists(video_path):
        # 尝试同名视频
        base_name = os.path.splitext(project_path)[0]
        auto_video = base_name + ".mp4"
        if os.path.exists(auto_video):
            video_path = auto_video
            print(f"[自动] 找到同名视频: {video_path}")
        else:
            # 尝试当前目录的mp4
            mp4_files = [f for f in os.listdir(os.path.dirname(project_path) or '.') if f.endswith('.mp4')]
            if mp4_files:
                video_path = os.path.join(os.path.dirname(project_path) or '.', mp4_files[0])
                print(f"[自动] 使用目录下第一个视频: {video_path}")
    
    # 默认yunxi语音
    voice = project.get("voice", "zh-CN-YunxiNeural")
    if not voice:
        voice = "zh-CN-YunxiNeural"
    rate = project.get("rate", "+0%")
    resolution = project.get("resolution", "native")
    
    # 智能提取脚本
    script_data = []
    if "script_content" in project:
        for part in project["script_content"]:
            if isinstance(part, dict) and "scenes" in part:
                script_data.extend(part["scenes"])
            elif isinstance(part, dict):
                script_data.append(part)
    elif "script" in project:
        script_data = project["script"]
    elif "scenes" in project:
        script_data = project["scenes"]
    elif isinstance(project, list):
        script_data = project
    
    if not script_data:
        print("[错误] 无法找到脚本数据")
        sys.exit(1)
    
    if not os.path.exists(video_path):
        print(f"[错误] 视频文件不存在: {video_path}")
        sys.exit(1)
    
    print(f"[视频] {video_path}")
    print(f"[语音] {voice} @ {rate}")
    print(f"[片段] {len(script_data)} 个场景")
    
    # 获取视频实际时长并过滤超时片段
    video_duration = get_duration(video_path)
    print(f"[视频时长] {video_duration:.1f} 秒 ({video_duration/60:.1f} 分钟)")
    
    def parse_time(t_str):
        p = list(map(float, t_str.split(':')))
        if len(p) == 2:
            return p[0]*60 + p[1]
        elif len(p) == 3:
            return p[0]*3600 + p[1]*60 + p[2]
        return 0
    
    
    # 显示视频信息（不过滤，由切割阶段自动适应）
    print(f"[片段] {len(script_data)} 个场景（超时片段将自动适应）\n")
    
    # Ensure temp dir
    if os.path.exists(TEMP_DIR):
        shutil.rmtree(TEMP_DIR)
    os.makedirs(TEMP_DIR, exist_ok=True)
    
    # Generate all TTS audio
    print("[阶段1] 生成语音...")
    audio_paths = asyncio.run(cli_generate_all_audio(script_data, voice, rate, TEMP_DIR))
    
    # Run FFmpeg render
    print("\n[阶段2] FFmpeg 渲染...")
    print(f"[分辨率] {resolution}")
    final_video = process_render(video_path, script_data, audio_paths, verbose=True, resolution=resolution)
    
    # Copy to output
    if output_path is None:
        output_path = os.path.splitext(os.path.basename(video_path))[0] + "_rendered.mp4"
    
    shutil.copy(final_video, output_path)
    print(f"\n[完成] 输出文件: {os.path.abspath(output_path)}")
    
    # Cleanup
    shutil.rmtree(TEMP_DIR)
    print("[清理] 临时文件已删除\n")

def create_sample_project(output_path: str):
    """Create a sample project file"""
    sample = {
        "video_path": "C:/path/to/your/video.mp4",
        "voice": "zh-CN-YunxiNeural",
        "rate": "+0%",
        "script": [
            {"time_start": "00:00", "time_end": "00:05", "voiceover": "这是第一段配音文本。"},
            {"time_start": "00:05", "time_end": "00:10", "voiceover": "这是第二段配音文本。"},
            {"time_start": "00:10", "time_end": "00:15", "voiceover": "这是第三段配音文本。"}
        ]
    }
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(sample, f, ensure_ascii=False, indent=2)
    print(f"[导出] 示例工程文件已创建: {output_path}")
    print("请编辑此文件，填入正确的视频路径和脚本内容。")

def check_script(script_path: str):
    """CLI: 检查脚本文件是否有效"""
    print(f"\n{'='*50}")
    print("智能配音剪辑器 - 脚本检测模式")
    print(f"{'='*50}\n")
    
    print(f"[检测] 读取文件: {script_path}")
    
    if not os.path.exists(script_path):
        print(f"❌ 错误: 文件不存在")
        return
    
    try:
        with open(script_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        print(f"❌ 读取失败: {e}")
        return
    
    # 尝试解析JSON
    try:
        raw = json.loads(content)
    except json.JSONDecodeError as e:
        print(f"❌ JSON解析失败: {e}")
        print(f"   错误位置: 第 {e.lineno} 行，第 {e.colno} 列")
        # 显示问题附近
        lines = content.split('\n')
        if 0 < e.lineno <= len(lines):
            print(f"   问题行: {lines[e.lineno-1][:80]}")
        return
    
    print("✅ JSON格式正确")
    
    # 智能提取脚本
    script_data = []
    format_name = ""
    
    if "script_content" in raw and isinstance(raw["script_content"], list):
        for part in raw["script_content"]:
            if isinstance(part, dict) and "scenes" in part:
                script_data.extend(part["scenes"])
            elif isinstance(part, dict):
                script_data.append(part)
        format_name = "script_content格式"
    elif "scenes" in raw:
        script_data = raw["scenes"]
        format_name = "scenes格式"
    elif "script" in raw:
        script_data = raw["script"]
        format_name = "script格式"
    elif isinstance(raw, list):
        script_data = raw
        format_name = "数组格式"
    else:
        print("❌ 无法识别脚本格式")
        print("   支持的格式: script_content, scenes, script, 或直接数组")
        return
    
    print(f"✅ 识别格式: {format_name}")
    print(f"✅ 片段数量: {len(script_data)}")
    
    # 检查每个片段
    warnings = []
    errors = []
    valid_count = 0
    
    def parse_time(t_str):
        p = list(map(float, t_str.split(':')))
        if len(p) == 2:
            return p[0]*60 + p[1]
        elif len(p) == 3:
            return p[0]*3600 + p[1]*60 + p[2]
        return 0
    
    for idx, scene in enumerate(script_data):
        num = idx + 1
        issues = []
        
        # 检查voiceover
        voiceover = scene.get("voiceover") or scene.get("text") or scene.get("content")
        if not voiceover:
            errors.append(f"片段 {num}: 缺少 voiceover/text/content")
            continue
        if len(voiceover.strip()) == 0:
            errors.append(f"片段 {num}: voiceover 为空")
            continue
        
        # 检查时间
        time_start = scene.get("time_start") or scene.get("start") or scene.get("begin")
        time_end = scene.get("time_end") or scene.get("end")
        
        if not time_start:
            issues.append("缺少 time_start，将自动生成")
        if not time_end:
            issues.append("缺少 time_end，将自动+5秒")
        
        if time_start and time_end:
            try:
                t1 = parse_time(str(time_start))
                t2 = parse_time(str(time_end))
                if t2 <= t1:
                    issues.append(f"time_end({time_end}) <= time_start({time_start})")
            except:
                issues.append("时间格式解析失败")
        
        if issues:
            warnings.append(f"片段 {num}: {'; '.join(issues)}")
        
        valid_count += 1
    
    # 检查voice设置
    voice = raw.get("voice", "")
    if not voice:
        print("⚠️  未指定语音，将使用默认: zh-CN-YunxiNeural")
    else:
        print(f"✅ 语音设置: {voice}")
    
    # 输出结果
    print(f"\n{'='*40}")
    print(f"检测结果汇总")
    print(f"{'='*40}")
    print(f"✅ 有效片段: {valid_count}/{len(script_data)}")
    
    if warnings:
        print(f"\n⚠️  警告 ({len(warnings)} 个，可自动修复):")
        for w in warnings[:10]:  # 最多显示10个
            print(f"   • {w}")
        if len(warnings) > 10:
            print(f"   ... 还有 {len(warnings)-10} 个警告")
    
    if errors:
        print(f"\n❌ 错误 ({len(errors)} 个，需手动修复):")
        for e in errors[:10]:
            print(f"   • {e}")
        if len(errors) > 10:
            print(f"   ... 还有 {len(errors)-10} 个错误")
    
    if not errors:
        print(f"\n✅ 脚本可用！可以进行渲染。")
    else:
        print(f"\n❌ 脚本有问题，请修复后再渲染。")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="智能配音剪辑器 - 支持 GUI 和 CLI 模式",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例:
  python app.py                          # 启动 GUI 服务器
  python app.py --render project.json    # CLI 渲染
  python app.py --check script.json      # 检测脚本格式
  python app.py --export sample.json     # 导出示例工程文件
  python app.py --render project.json -o output.mp4  # 指定输出文件
        """
    )
    parser.add_argument("--render", "-r", metavar="PROJECT", help="从工程文件渲染视频 (CLI模式)")
    parser.add_argument("--output", "-o", metavar="FILE", help="输出文件路径 (配合 --render 使用)")
    parser.add_argument("--export", "-e", metavar="FILE", help="导出示例工程文件")
    parser.add_argument("--check", "-c", metavar="SCRIPT", help="检测脚本文件格式")
    
    args = parser.parse_args()
    
    if args.check:
        check_script(args.check)
    elif args.render:
        render_from_project(args.render, args.output)
    elif args.export:
        create_sample_project(args.export)
    else:
        # GUI mode
        print("启动服务器: http://127.0.0.1:8000")
        uvicorn.run(app, host="127.0.0.1", port=8000)
