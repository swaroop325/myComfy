#!/bin/bash
# Downloads Wan 2.1 T2V 1.3B (ComfyUI repackaged) into ComfyUI/models. Resumable; skips finished files.
# Usage: download_models.sh [--list]
set -euo pipefail
HF=https://huggingface.co/Comfy-Org
M=/opt/comfyui/data/ComfyUI/models
# "<repo>|<path under split_files>"  (Wan 2.2 5B is the default; 2.1 1.3B kept as a fast test)
FILES=(
  "Wan_2.2_ComfyUI_Repackaged|diffusion_models/wan2.2_ti2v_5B_fp16.safetensors"
  "Wan_2.2_ComfyUI_Repackaged|vae/wan2.2_vae.safetensors"
  "Wan_2.1_ComfyUI_repackaged|text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors"
  "Wan_2.1_ComfyUI_repackaged|diffusion_models/wan2.1_t2v_1.3B_fp16.safetensors"
  "Wan_2.1_ComfyUI_repackaged|vae/wan_2.1_vae.safetensors"
  # 2D / illustration stills (direct URL form: "url>dest under models/")
  "https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0/resolve/main/sd_xl_base_1.0.safetensors>checkpoints/sd_xl_base_1.0.safetensors"
  "https://huggingface.co/cagliostrolab/animagine-xl-4.0/resolve/main/animagine-xl-4.0.safetensors>checkpoints/animagine-xl-4.0.safetensors"
)
if [ "${1:-}" = "--list" ]; then for e in "${FILES[@]}"; do echo "$M/${e#*|}"; done; exit 0; fi
AUTH=()
TOKEN=$(aws ssm get-parameter --name /comfyui/hf_token --with-decryption --query Parameter.Value --output text --region "${AWS_REGION:-ap-southeast-1}" 2>/dev/null || true)
if [ -n "$TOKEN" ]; then AUTH=(-H "Authorization: Bearer $TOKEN"); fi
for e in "${FILES[@]}"; do
  if [[ "$e" == http* ]]; then url="${e%%>*}"; f="${e#*>}"; else repo="${e%%|*}"; f="${e#*|}"; url="$HF/$repo/resolve/main/split_files/$f"; fi
  dest="$M/$f"; mkdir -p "$(dirname "$dest")"
  if [ -f "$dest" ]; then echo "skip $f"; continue; fi
  echo "get $f"
  curl -fL -sS -C - "${AUTH[@]}" -o "$dest.part" "$url"
  mv "$dest.part" "$dest"
done
echo done
