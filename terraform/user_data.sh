#!/bin/bash
# Series Maker EC2 Bootstrap Script (runs once, on first boot)
# Installs NVIDIA drivers, ffmpeg, Piper TTS, Ollama (GPUs 0-2), ComfyUI (GPU 3)
# and the seriesmaker worker service. Everything binds to 127.0.0.1.
#
# Table/queue/bucket/model are NOT templated into this file: Terraform sets them
# as instance tags (sm-table, sm-queue-url, sm-bucket, sm-llm-model) and this
# script reads them from IMDS (instance metadata tags are enabled in main.tf).

set -eo pipefail

exec > >(tee /var/log/user-data.log) 2>&1
echo "Starting Series Maker bootstrap at $(date)"

# Update system
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get upgrade -y

# Install NVIDIA drivers and CUDA
apt-get install -y ubuntu-drivers-common
ubuntu-drivers autoinstall

# Install NVIDIA Container Toolkit (for Docker GPU support if needed later)
distribution=$(. /etc/os-release;echo $ID$VERSION_ID)
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/$distribution/libnvidia-container.list | \
  sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
  tee /etc/apt/sources.list.d/nvidia-container-toolkit.list

apt-get update
apt-get install -y nvidia-container-toolkit

# Python: 3.11 for ComfyUI, 3.12 for the seriesmaker worker (pyproject needs >=3.12)
add-apt-repository ppa:deadsnakes/ppa -y
apt-get update
apt-get install -y python3.11 python3.11-venv python3.11-dev python3-pip
apt-get install -y python3.12 python3.12-venv python3.12-dev

# Other dependencies
apt-get install -y git curl wget unzip ffmpeg fonts-dejavu-core zstd

# Create app directory
mkdir -p /opt/comfyui
chown ubuntu:ubuntu /opt/comfyui

# Install ComfyUI as ubuntu user
sudo -u ubuntu bash << 'COMFYEOF'
set -eo pipefail
cd /opt/comfyui

git clone https://github.com/comfyanonymous/ComfyUI.git
cd ComfyUI

python3.11 -m venv venv
source venv/bin/activate

# PyTorch with CUDA support
pip install --upgrade pip
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu121

pip install -r requirements.txt

# ComfyUI Manager
cd custom_nodes
git clone https://github.com/ltdrdata/ComfyUI-Manager.git
cd ..

mkdir -p models/checkpoints models/loras models/vae models/embeddings models/clip models/clip_vision models/controlnet models/upscale_models
mkdir -p output
COMFYEOF

# ComfyUI service: localhost only, pinned to GPU 3
cat > /etc/systemd/system/comfyui.service << 'SERVICEEOF'
[Unit]
Description=ComfyUI (GPU 3, localhost only)
After=network-online.target nvidia-persistenced.service
Wants=network-online.target nvidia-persistenced.service

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/opt/comfyui/ComfyUI
# GPU readiness gate: wait up to 2 min for the NVIDIA driver after boot
ExecStartPre=/bin/sh -c 'for i in $(seq 1 60); do nvidia-smi -L >/dev/null 2>&1 && exit 0; sleep 2; done; exit 1'
ExecStart=/opt/comfyui/ComfyUI/venv/bin/python main.py --listen 127.0.0.1 --port 8188
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1
Environment=CUDA_VISIBLE_DEVICES=3

[Install]
WantedBy=multi-user.target
SERVICEEOF

# Helper: download the checkpoints named in src/seriesmaker/worker/profiles.py
cat > /opt/comfyui/download-models.sh << 'MODELSEOF'
#!/bin/bash
# Downloads the SDXL checkpoints used by the seriesmaker style profiles.
# Filenames MUST match src/seriesmaker/worker/profiles.py.
set -euo pipefail
cd /opt/comfyui/ComfyUI/models/checkpoints

fetch() {  # fetch <filename> <url>
  if [ -s "$1" ]; then echo "have $1"; return; fi
  echo "Downloading $1 ..."
  wget -q --show-progress -O "$1.part" "$2"
  mv "$1.part" "$1"
}

# animation profile: Animagine XL 3.1
fetch animagine-xl-3.1.safetensors \
  "https://huggingface.co/cagliostrolab/animagine-xl-3.1/resolve/main/animagine-xl-3.1.safetensors"

# realistic profile: Juggernaut XL v9
fetch juggernautXL_v9.safetensors \
  "https://huggingface.co/RunDiffusion/Juggernaut-XL-v9/resolve/main/Juggernaut-XL_v9_RunDiffusionPhoto_v2.safetensors"

echo "Models downloaded! Restart ComfyUI: sudo systemctl restart comfyui"
MODELSEOF
chmod +x /opt/comfyui/download-models.sh
chown ubuntu:ubuntu /opt/comfyui/download-models.sh

# GPU check script
cat > /opt/comfyui/check-gpus.sh << 'GPUEOF'
#!/bin/bash
echo "=== GPU Status ==="
nvidia-smi --query-gpu=index,name,memory.total,memory.used,utilization.gpu --format=csv
GPUEOF
chmod +x /opt/comfyui/check-gpus.sh
chown ubuntu:ubuntu /opt/comfyui/check-gpus.sh

# ---------------------------------------------------------------------------
# Series Maker: config from instance tags, worker venv, Piper, Ollama, services
# ---------------------------------------------------------------------------
imds() {  # imds <path> : IMDSv2 GET
  local tok
  tok=$(curl -fsS -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 300')
  curl -fsS -H "X-aws-ec2-metadata-token: $tok" "http://169.254.169.254/latest/$1"
}
SM_TABLE=$(imds meta-data/tags/instance/sm-table)
SM_QUEUE_URL=$(imds meta-data/tags/instance/sm-queue-url)
SM_BUCKET=$(imds meta-data/tags/instance/sm-bucket)
# Single place the model name lives (Terraform var llm_model -> tag). TO BE CHANGED after the speed spike.
LLM_MODEL=$(imds meta-data/tags/instance/sm-llm-model || echo "llama3.3:70b")
SM_REGION=$(imds meta-data/placement/region)

mkdir -p /opt/seriesmaker/voices /etc/seriesmaker /var/lib/seriesmaker
chown -R ubuntu:ubuntu /opt/seriesmaker /var/lib/seriesmaker

cat > /etc/seriesmaker/worker.env << ENVEOF
TABLE=$SM_TABLE
QUEUE_URL=$SM_QUEUE_URL
BUCKET=$SM_BUCKET
LLM_BASE_URL=http://127.0.0.1:11434
LLM_MODEL=$LLM_MODEL
LLM_TIMEOUT_S=600
COMFY_URL=http://127.0.0.1:8188
OWNER_CHAT_ID_PARAM=/seriesmaker/owner_chat_id
TELEGRAM_TOKEN_PARAM=/seriesmaker/telegram_token
SERIESMAKER_PIPER_MODEL=/opt/seriesmaker/voices/en_US-lessac-medium.onnx
WORKDIR=/var/lib/seriesmaker
AWS_DEFAULT_REGION=$SM_REGION
ENVEOF
chmod 644 /etc/seriesmaker/worker.env   # no secrets: only resource names and SSM parameter names

# Worker venv (Python 3.12) with the runtime deps; the package itself comes from S3
sudo -u ubuntu bash << 'VENVEOF'
set -eo pipefail
python3.12 -m venv /opt/seriesmaker/venv
/opt/seriesmaker/venv/bin/pip install --upgrade pip
/opt/seriesmaker/venv/bin/pip install boto3 requests pillow
VENVEOF

# seriesmaker-install-piper: Piper TTS + voice. Pinned (1.3.0 ships a cp39-abi3 wheel, so it works on 3.12).
# Idempotent, so it doubles as the manual retry helper.
cat > /usr/local/bin/seriesmaker-install-piper << 'PIPEREOF'
#!/bin/bash
set -euo pipefail
/opt/seriesmaker/venv/bin/pip install "piper-tts==1.3.0"
mkdir -p /opt/seriesmaker/voices
cd /opt/seriesmaker/voices
V=https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium
for f in en_US-lessac-medium.onnx en_US-lessac-medium.onnx.json; do
  [ -s "$f" ] || { wget -q -O "$f.part" "$V/$f" && mv "$f.part" "$f"; }
done
echo "piper ok"
PIPEREOF
chmod 755 /usr/local/bin/seriesmaker-install-piper
# Non-fatal: a Piper problem must not abort the boot script before the reboot
sudo -u ubuntu /usr/local/bin/seriesmaker-install-piper \
  || echo "WARN: Piper install failed; after boot run: sudo -u ubuntu seriesmaker-install-piper"

# seriesmaker-update: pull releases/seriesmaker-latest.tar.gz from S3 and pip install it.
# Idempotent: skips (fast) when the S3 ETag matches /opt/seriesmaker/.release-etag.
cat > /usr/local/bin/seriesmaker-update << 'UPDEOF'
#!/bin/bash
set -euo pipefail
set -a; . /etc/seriesmaker/worker.env; set +a
PY=/opt/seriesmaker/venv/bin/python
STAMP=/opt/seriesmaker/.release-etag
ETAG=$($PY - "$BUCKET" << 'PY'
import sys, boto3
print(boto3.client("s3").head_object(Bucket=sys.argv[1], Key="releases/seriesmaker-latest.tar.gz")["ETag"].strip('"'))
PY
)
if [ -f "$STAMP" ] && [ "$(cat "$STAMP")" = "$ETAG" ]; then
  echo "seriesmaker release unchanged ($ETAG)"; exit 0
fi
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
$PY - "$BUCKET" "$TMP/seriesmaker-latest.tar.gz" << 'PY'
import sys, boto3
boto3.client("s3").download_file(sys.argv[1], "releases/seriesmaker-latest.tar.gz", sys.argv[2])
PY
# One install. Runtime deps (boto3, requests, pillow) were installed at first boot, hence --no-deps;
# --force-reinstall is needed because the package version string does not change between releases.
/opt/seriesmaker/venv/bin/pip install --quiet --force-reinstall --no-deps "$TMP/seriesmaker-latest.tar.gz"
echo "$ETAG" > "$STAMP"
echo "seriesmaker updated to $ETAG at $(date)"
UPDEOF
chmod 755 /usr/local/bin/seriesmaker-update

# seriesmaker-warm-model: load $LLM_MODEL into GPU memory after ollama starts (non-fatal).
# Run in the background by ollama.service's ExecStartPost so a slow 70B load cannot time out the unit.
cat > /usr/local/bin/seriesmaker-warm-model << 'WARMEOF'
#!/bin/bash
[ -n "${LLM_MODEL:-}" ] || { set -a; . /etc/seriesmaker/worker.env; set +a; }
for _ in $(seq 1 60); do curl -fsS http://127.0.0.1:11434/api/version >/dev/null 2>&1 && break; sleep 2; done
if curl -fsS -m 1200 http://127.0.0.1:11434/api/generate \
     -d "{\"model\":\"$LLM_MODEL\",\"prompt\":\"\",\"keep_alive\":\"30m\"}" >/dev/null; then
  echo "warmed $LLM_MODEL"
else
  echo "WARN: warm-up of $LLM_MODEL failed (non-fatal)"
fi
exit 0
WARMEOF
chmod 755 /usr/local/bin/seriesmaker-warm-model

# Ollama (manual install: the official install.sh may try to install its own GPU drivers)
curl -fsSL https://ollama.com/download/ollama-linux-amd64.tar.zst | tar x --zstd -C /usr
id ollama &>/dev/null || useradd -r -s /bin/false -U -m -d /usr/share/ollama ollama

cat > /etc/systemd/system/ollama.service << 'OLLEOF'
[Unit]
Description=Ollama LLM server (GPUs 0-2, localhost only)
After=network-online.target nvidia-persistenced.service
Wants=network-online.target nvidia-persistenced.service

[Service]
Type=simple
User=ollama
Group=ollama
# GPU readiness gate: wait up to 2 min for the NVIDIA driver after boot
ExecStartPre=/bin/sh -c 'for i in $(seq 1 60); do nvidia-smi -L >/dev/null 2>&1 && exit 0; sleep 2; done; exit 1'
ExecStart=/usr/bin/ollama serve
Restart=always
RestartSec=5
Environment=CUDA_VISIBLE_DEVICES=0,1,2
Environment=OLLAMA_SCHED_SPREAD=1
Environment=OLLAMA_NUM_PARALLEL=1
# Flash attention + quantized KV cache cut VRAM per context; the speed spike decides the final model/settings
Environment=OLLAMA_FLASH_ATTENTION=1
Environment=OLLAMA_KV_CACHE_TYPE=q8_0
Environment=OLLAMA_HOST=127.0.0.1:11434
Environment=OLLAMA_KEEP_ALIVE=30m
# Default context is too small for chat history + bible; 8192 fits the q8_0 KV cache
Environment=OLLAMA_CONTEXT_LENGTH=8192
# LLM_MODEL for the warm-up (no secrets in this file); "-": optional
EnvironmentFile=-/etc/seriesmaker/worker.env
# Warm the model after boot; "-" and "&" keep it non-fatal and non-blocking (no $ to escape)
ExecStartPost=-/bin/sh -c '/usr/local/bin/seriesmaker-warm-model &'

[Install]
WantedBy=multi-user.target
OLLEOF

# Pull helper: reads LLM_MODEL from worker.env, so changing the model is one edit + one command
cat > /usr/local/bin/seriesmaker-pull-model << 'PULLEOF'
#!/bin/bash
set -euo pipefail
set -a; . /etc/seriesmaker/worker.env; set +a
ollama pull "$LLM_MODEL"
PULLEOF
chmod 755 /usr/local/bin/seriesmaker-pull-model

# Worker service: after ollama and comfyui; ExecStartPre refreshes the code from S3
cat > /etc/systemd/system/seriesmaker-worker.service << 'WKEOF'
[Unit]
Description=Series Maker worker (SQS poller)
After=network-online.target ollama.service comfyui.service
Wants=network-online.target ollama.service comfyui.service
StartLimitIntervalSec=600
StartLimitBurst=10

[Service]
Type=simple
TimeoutStartSec=300
User=ubuntu
WorkingDirectory=/var/lib/seriesmaker
EnvironmentFile=/etc/seriesmaker/worker.env
Environment=PYTHONUNBUFFERED=1
Environment=PATH=/opt/seriesmaker/venv/bin:/usr/local/bin:/usr/bin:/bin
# "-" so a missing release or S3 blip does not block starting an already-installed version
ExecStartPre=-/usr/local/bin/seriesmaker-update
ExecStart=/opt/seriesmaker/venv/bin/python -m seriesmaker.worker.main
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
WKEOF

systemctl daemon-reload
systemctl enable ollama comfyui seriesmaker-worker

# Pull the model now (downloading needs no GPU). Non-fatal: re-run
# `seriesmaker-pull-model` by hand if it fails.
# The GPU is not available until the reboot, so run a throwaway server (not the gated unit).
sudo -u ollama env OLLAMA_HOST=127.0.0.1:11434 /usr/bin/ollama serve > /var/log/ollama-bootstrap.log 2>&1 &
OLLAMA_PID=$!
for _ in $(seq 1 30); do curl -fsS http://127.0.0.1:11434/api/version >/dev/null 2>&1 && break; sleep 2; done
sudo -u ubuntu /usr/local/bin/seriesmaker-pull-model || echo "WARN: model pull failed; run seriesmaker-pull-model"
pkill -u ollama -f "ollama serve" || true
wait "$OLLAMA_PID" 2>/dev/null || true
# Worker code: may not be uploaded yet (run scripts/deploy_worker.sh, then seriesmaker-update)
sudo -u ubuntu /usr/local/bin/seriesmaker-update || echo "WARN: no worker release in S3 yet; run scripts/deploy_worker.sh"

# MOTD
cat > /etc/update-motd.d/99-comfyui << 'MOTDEOF'
#!/bin/bash
echo ""
echo "=========================================="
echo "   Series Maker Server (4x T4 GPUs)"
echo "=========================================="
echo "GPUs detected: $(nvidia-smi -L 2>/dev/null | wc -l)"
echo "ComfyUI 127.0.0.1:8188 (GPU 3), Ollama 127.0.0.1:11434 (GPUs 0-2)"
echo "Logs:  sudo journalctl -u seriesmaker-worker -f   (also: ollama, comfyui)"
echo "Update worker: sudo -u ubuntu seriesmaker-update && sudo systemctl restart seriesmaker-worker"
echo "Models: /opt/comfyui/download-models.sh ; GPUs: /opt/comfyui/check-gpus.sh"
echo "=========================================="
MOTDEOF
chmod +x /etc/update-motd.d/99-comfyui

# Bootstrap complete marker
touch /opt/comfyui/.bootstrap_complete

echo "Bootstrap complete at $(date)"
echo "Rebooting to load NVIDIA drivers; ollama, comfyui and the worker start via systemd afterwards"

reboot
