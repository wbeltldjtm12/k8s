#!/usr/bin/env bash
set -euo pipefail
# Deployment controller가 새 Pod를 생성하므로 별도 변경을 되돌릴 것은 없다.
echo '[scenario] 18-cascade-carts: controller 자가 복구 대기'
