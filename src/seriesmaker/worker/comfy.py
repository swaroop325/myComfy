"""Minimal ComfyUI HTTP client."""
from __future__ import annotations

import time
import uuid

import requests

from seriesmaker.models import StyleProfile
from seriesmaker.worker.profiles import load_workflow


class ComfyError(Exception):
    pass


class ComfyClient:
    def __init__(self, base_url: str, timeout_s: float = 300.0, poll_interval_s: float = 1.0):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.poll_interval_s = poll_interval_s

    def render_image(self, profile: StyleProfile, prompt: str, seed: int) -> bytes:
        workflow = load_workflow(profile, prompt, seed)
        try:
            r = requests.post(
                f"{self.base_url}/prompt",
                json={"prompt": workflow, "client_id": str(uuid.uuid4())},
                timeout=30,
            )
        except requests.RequestException as e:
            raise ComfyError(f"ComfyUI unreachable: {e}") from e
        if not r.ok:
            raise ComfyError(f"ComfyUI rejected prompt: {r.status_code} {r.text[:200]}")
        image = self._wait_for_image(r.json()["prompt_id"])
        try:
            v = requests.get(f"{self.base_url}/view", params=image, timeout=60)
        except requests.RequestException as e:
            raise ComfyError(f"ComfyUI /view failed: {e}") from e
        if not v.ok:
            raise ComfyError(f"ComfyUI /view failed: {v.status_code}")
        return v.content

    def _wait_for_image(self, prompt_id: str) -> dict:
        deadline = time.monotonic() + self.timeout_s
        while True:
            try:
                h = requests.get(f"{self.base_url}/history/{prompt_id}", timeout=30)
            except requests.RequestException as e:
                raise ComfyError(f"ComfyUI history poll failed: {e}") from e
            if h.status_code >= 500:
                raise ComfyError(f"ComfyUI /history returned {h.status_code}")
            if h.ok:
                try:
                    entry = h.json().get(prompt_id)
                except ValueError as e:
                    raise ComfyError("ComfyUI /history returned invalid JSON") from e
                if entry:
                    if entry.get("status", {}).get("status_str") == "error":
                        raise ComfyError(f"ComfyUI job {prompt_id} failed")
                    for out in entry.get("outputs", {}).values():
                        for img in out.get("images", []):
                            return {
                                "filename": img["filename"],
                                "subfolder": img.get("subfolder", ""),
                                "type": img.get("type", "output"),
                            }
            if time.monotonic() >= deadline:
                raise ComfyError(f"ComfyUI job {prompt_id} timed out after {self.timeout_s}s")
            time.sleep(self.poll_interval_s)
