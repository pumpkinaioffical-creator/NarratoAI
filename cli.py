#!/usr/bin/env python
# -*- coding: utf-8 -*-

import argparse
import os
import sys
import uuid
from loguru import logger
from app.models.schema import VideoClipParams, VideoAspect
from app.services import task as tm
from app.config import config
from app.utils import utils

def init_log():
    logger.remove()
    logger.add(
        sys.stdout,
        level="INFO",
        format='<green>{time:%Y-%m-%d %H:%M:%S}</> | <level>{level}</> | <level>{message}</>'
    )

def main():
    parser = argparse.ArgumentParser(description="NarratoAI CLI - Generate videos from the terminal")

    parser.add_argument("--script", required=True, help="Path to the video script JSON file")
    parser.add_argument("--video", required=True, help="Path to the original video file")

    # Optional parameters
    parser.add_argument("--aspect", default="9:16", choices=["16:9", "9:16", "1:1", "4:3", "3:4"], help="Video aspect ratio")
    parser.add_argument("--voice", default="zh-CN-YunjianNeural", help="TTS voice name")
    parser.add_argument("--tts_engine", default="edge", help="TTS engine (edge/openai/azure/etc)")
    parser.add_argument("--subtitle", action="store_true", default=True, help="Enable subtitles")
    parser.add_argument("--no-subtitle", action="store_false", dest="subtitle", help="Disable subtitles")

    args = parser.parse_args()

    init_log()

    # Check files
    if not os.path.exists(args.script):
        logger.error(f"Script file not found: {args.script}")
        sys.exit(1)

    if not os.path.exists(args.video):
        logger.error(f"Video file not found: {args.video}")
        sys.exit(1)

    # Map aspect string to Enum
    aspect_map = {
        "16:9": VideoAspect.landscape.value,
        "9:16": VideoAspect.portrait.value,
        "1:1": VideoAspect.square.value,
        "4:3": VideoAspect.landscape_2.value,
        "3:4": VideoAspect.portrait_2.value
    }

    # Initialize basic resources if needed
    try:
        utils.init_resources()
    except Exception as e:
        logger.warning(f"Resource initialization warning: {e}")

    # Construct params
    params = VideoClipParams(
        video_clip_json_path=args.script,
        video_origin_path=args.video,
        video_aspect=aspect_map.get(args.aspect, VideoAspect.portrait.value),
        voice_name=args.voice,
        tts_engine=args.tts_engine,
        subtitle_enabled=args.subtitle,
        # Default volumes
        tts_volume=1.0,
        bgm_volume=0.3,
        original_volume=1.2,
        n_threads=4
    )

    task_id = str(uuid.uuid4())
    logger.info(f"Starting task {task_id} with script {args.script} and video {args.video}")

    try:
        tm.start_subclip_unified(task_id=task_id, params=params)
        logger.success(f"Task {task_id} completed successfully!")
    except Exception as e:
        logger.error(f"Task failed: {e}")
        logger.exception(e)
        sys.exit(1)

if __name__ == "__main__":
    main()
