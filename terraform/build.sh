#!/usr/bin/env bash
# Builds terraform/build/lambda.zip: src/seriesmaker + requests (boto3 is in the Lambda runtime).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
OUT="$HERE/build"
PKG="$OUT/package"

rm -rf "$OUT"
mkdir -p "$PKG"
python3 -m pip install --quiet --target "$PKG" --upgrade \
  --platform manylinux2014_x86_64 --implementation cp --python-version 3.12 \
  --only-binary=:all: requests
cp -R "$ROOT/src/seriesmaker" "$PKG/seriesmaker"
find "$PKG" -name '__pycache__' -type d -prune -exec rm -rf {} +
find "$PKG" -name '*.pyc' -delete
python3 - "$PKG" "$OUT/lambda.zip" <<'PY'
import os, sys, zipfile
pkg, out = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    entries = []
    for d, _, files in os.walk(pkg):
        for f in files:
            p = os.path.join(d, f)
            entries.append((os.path.relpath(p, pkg), p))
    for rel, p in sorted(entries):
        zi = zipfile.ZipInfo(rel, date_time=(1980, 1, 1, 0, 0, 0))
        zi.compress_type = zipfile.ZIP_DEFLATED
        zi.external_attr = 0o644 << 16
        with open(p, "rb") as fh:
            z.writestr(zi, fh.read())
PY
echo "built $OUT/lambda.zip"
