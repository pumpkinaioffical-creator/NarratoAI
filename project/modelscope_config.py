# ModelScope Image Generation Configuration

MODELSCOPE_BASE_URL = "https://api-inference.modelscope.cn/"

# Hardcoded models list - add more in the future
MODELSCOPE_MODELS = [
    {"id": "Tongyi-MAI/Z-Image-Turbo", "name": "Z-Image-Turbo (通义图像)", "supports_image_input": False},
    {"id": "black-forest-labs/FLUX.2-dev", "name": "FLUX.2-dev (文生图/图编辑)", "supports_image_input": True},
]

# Default timeout settings
DEFAULT_TIMEOUT_SECONDS = 300      # 5 minutes - after this user can retry
BUTTON_LOCKOUT_SECONDS = 240       # 4 minutes - button stays disabled

# Resolution presets - admin can select which ones are available
RESOLUTION_PRESETS = [
    {"id": "720x720", "name": "720x720 (方形)", "width": 720, "height": 720},
    {"id": "1024x1024", "name": "1024x1024 (方形)", "width": 1024, "height": 1024},
    {"id": "1280x720", "name": "1280x720 (横向)", "width": 1280, "height": 720},
    {"id": "720x1280", "name": "720x1280 (纵向)", "width": 720, "height": 1280},
    {"id": "1920x1080", "name": "1920x1080 (全高清横向)", "width": 1920, "height": 1080},
    {"id": "1080x1920", "name": "1080x1920 (全高清纵向)", "width": 1080, "height": 1920},
]

def get_model_by_id(model_id):
    """Get model info by ID."""
    for model in MODELSCOPE_MODELS:
        if model["id"] == model_id:
            return model
    return None

def get_default_model_id():
    """Get the default model ID."""
    if MODELSCOPE_MODELS:
        return MODELSCOPE_MODELS[0]["id"]
    return None

def get_resolution_by_id(resolution_id):
    """Get resolution info by ID."""
    for res in RESOLUTION_PRESETS:
        if res["id"] == resolution_id:
            return res
    return None

def get_default_resolution():
    """Get the default resolution."""
    if RESOLUTION_PRESETS:
        return RESOLUTION_PRESETS[0]
    return {"id": "1024x1024", "name": "1024x1024", "width": 1024, "height": 1024}
