"""Style profiles and ComfyUI workflow loading.

Builder choices (the spec leaves them open). The box owner must download these
files to ComfyUI/models/checkpoints (and ComfyUI/models/loras) on the GPU box;
filenames below are placeholders for well-known public models:

* animation: SDXL anime/webtoon checkpoint (Animagine XL 3.1), no LoRA.
* realistic: SDXL photoreal checkpoint (Juggernaut XL v9), no LoRA.

Both are SDXL run in fp16 (no bf16: the T4 lacks it) and fit a single 16 GB T4.
Seed is fixed per profile; the pipeline uses ``profile.seed + scene_index``.
TTS voice: Piper ``en_US-lessac-medium`` (see tts.py). Music is optional and
supplied by the pipeline as a path.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

from seriesmaker.models import StyleProfile

WORKFLOW_DIR = Path(__file__).parent / "workflows"

PROFILES: dict[str, StyleProfile] = {
    "animation": StyleProfile(
        name="animation",
        checkpoint="animagine-xl-3.1.safetensors",
        lora=None,
        prompt_prefix="webtoon style, anime key visual, vibrant colors, clean lineart, vertical composition, ",
        seed=424242,
        motion="slow_zoom_in",
    ),
    "realistic": StyleProfile(
        name="realistic",
        checkpoint="juggernautXL_v9.safetensors",
        lora=None,
        prompt_prefix="photorealistic, cinematic lighting, 35mm photo, sharp focus, vertical composition, ",
        seed=777001,
        motion="slow_pan",
    ),
}


def load_workflow(profile: StyleProfile, prompt: str, seed: int) -> dict:
    """Return a ComfyUI API-format workflow with profile, prompt and seed filled in."""
    path = WORKFLOW_DIR / f"{profile.name}.json"
    wf = copy.deepcopy(json.loads(path.read_text()))
    wf["4"]["inputs"]["ckpt_name"] = profile.checkpoint
    wf["6"]["inputs"]["text"] = profile.prompt_prefix + prompt
    wf["3"]["inputs"]["seed"] = seed
    if profile.lora:
        wf["10"]["inputs"]["lora_name"] = profile.lora
    else:
        # No LoRA: drop the loader and wire consumers straight to the checkpoint.
        del wf["10"]
        wf["6"]["inputs"]["clip"] = ["4", 1]
        wf["7"]["inputs"]["clip"] = ["4", 1]
        wf["3"]["inputs"]["model"] = ["4", 0]
    return wf
