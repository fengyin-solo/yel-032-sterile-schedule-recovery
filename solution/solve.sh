#!/bin/bash
set -euo pipefail
cd /app
install -m 0755 /solution/reference.py plan_sterilization.py
runuser -u nobody -- python3 plan_sterilization.py --root /data
