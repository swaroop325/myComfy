#!/bin/bash
# Private ComfyUI bootstrap. Idempotent: safe on first boot, reboot, and re-attach of an existing data volume.
# NOTE: rendered by Terraform templatefile(); only idle_minutes is templated (bash vars use $$ where braces are needed).
set -eo pipefail
exec > >(tee -a /var/log/user-data.log) 2>&1
echo "== bootstrap start $(date)"

DATA=/opt/comfyui/data
mkdir -p $DATA

# --- 1. find and mount the persistent EBS data volume (never the instance-store NVMe) ---
ROOT_DISK=$(lsblk -no PKNAME "$(findmnt -no SOURCE /)" | head -1)
DEV=""
for i in $(seq 1 60); do
  for d in /dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_*; do
    [ -e "$d" ] || continue
    case "$d" in *-part*) continue ;; esac
    real=$(readlink -f "$d")
    [ "$(basename "$real")" = "$ROOT_DISK" ] && continue
    DEV="$real"
  done
  [ -n "$DEV" ] && break
  sleep 5
done
[ -n "$DEV" ] || { echo "data volume not found"; exit 1; }

# format only a blank device (never reformat a volume that already has a filesystem)
if [ -z "$(blkid -o value -s TYPE "$DEV" || true)" ]; then
  mkfs.ext4 -L comfyui-data "$DEV"
fi
grep -q 'LABEL=comfyui-data' /etc/fstab || echo 'LABEL=comfyui-data /opt/comfyui/data ext4 defaults,nofail 0 2' >> /etc/fstab
mountpoint -q $DATA || mount $DATA
chown ubuntu:ubuntu $DATA

# --- 2. install ComfyUI (latest master) onto the data volume, once ---
if [ ! -f $DATA/.bootstrapped ]; then
  add-apt-repository ppa:deadsnakes/ppa -y
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y python3.12 python3.12-venv python3.12-dev git curl ffmpeg
  sudo -u ubuntu bash <<'COMFYEOF'
set -eo pipefail
cd /opt/comfyui/data
[ -d ComfyUI ] || git clone https://github.com/comfyanonymous/ComfyUI.git
cd ComfyUI
python3.12 -m venv venv
. venv/bin/activate
pip install --upgrade pip
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu126
pip install -r requirements.txt
[ -d custom_nodes/ComfyUI-Manager ] || git clone https://github.com/ltdrdata/ComfyUI-Manager.git custom_nodes/ComfyUI-Manager
git rev-parse HEAD > /opt/comfyui/data/COMFYUI_COMMIT
COMFYEOF
  touch $DATA/.bootstrapped
fi

# --- 3. service (localhost only) ---
cat > /etc/systemd/system/comfyui.service <<'SVC'
[Unit]
Description=ComfyUI (localhost only)
After=network-online.target opt-comfyui-data.mount
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/opt/comfyui/data/ComfyUI
ExecStartPre=/bin/sh -c 'for i in $(seq 1 60); do nvidia-smi -L >/dev/null 2>&1 && exit 0; sleep 2; done; exit 1'
ExecStart=/opt/comfyui/data/ComfyUI/venv/bin/python main.py --listen 127.0.0.1 --port 8188 --enable-compress-response-body
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
SVC

# --- 4. idle auto-stop: nothing queued/running, no GPU load, no recent output, no download for N minutes ---
cat > /usr/local/bin/idle-stop.sh <<'IDLE'
#!/bin/bash
LIMIT=${idle_minutes}
STATE=/run/idle_count
UP=$(cut -d. -f1 /proc/uptime)
if [ "$UP" -lt 2700 ]; then echo 0 > $STATE; exit 0; fi   # 45 min grace after boot (install, first tunnel)
busy=0
q=$(curl -s --max-time 5 http://127.0.0.1:8188/queue || true)
echo "$q" | python3 -c "import sys,json; d=json.load(sys.stdin); sys.exit(0 if d.get('queue_running') or d.get('queue_pending') else 1)" 2>/dev/null && busy=1
gpu=$(nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits 2>/dev/null | head -1)
if [ "$${gpu:-0}" -gt 5 ] 2>/dev/null; then busy=1; fi
if [ -n "$(find /opt/comfyui/data/ComfyUI/output -type f -mmin -5 2>/dev/null | head -1)" ]; then busy=1; fi
pgrep -f download_models >/dev/null && busy=1
if [ $busy -eq 1 ]; then echo 0 > $STATE; exit 0; fi
n=$(( $(cat $STATE 2>/dev/null || echo 0) + 1 )); echo $n > $STATE
if [ "$n" -ge "$LIMIT" ]; then logger "idle-stop: $n idle minutes, powering off (instance stops)"; systemctl poweroff; fi
IDLE
chmod +x /usr/local/bin/idle-stop.sh
cat > /etc/systemd/system/idle-stop.service <<'U'
[Service]
Type=oneshot
ExecStart=/usr/local/bin/idle-stop.sh
U
cat > /etc/systemd/system/idle-stop.timer <<'U'
[Timer]
OnBootSec=60
OnUnitActiveSec=60
[Install]
WantedBy=timers.target
U

systemctl daemon-reload
systemctl enable --now comfyui.service idle-stop.timer
systemctl enable --now snap.amazon-ssm-agent.amazon-ssm-agent.service 2>/dev/null || systemctl enable --now amazon-ssm-agent 2>/dev/null || true
echo "== bootstrap done $(date)"
