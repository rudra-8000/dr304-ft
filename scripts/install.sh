#!/usr/bin/env bash
# One-time setup of the D.R304 F/T sensor package on Ubuntu 22.04.
#   bash scripts/install.sh            # installs into the currently active python / conda env
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "== 1/3 python package (editable) into: $(command -v python)"
python -m pip install -e "$HERE"

echo "== 2/3 udev rule -> /dev/dr304 (needs sudo)"
sudo cp "$HERE/udev/99-dr304.rules" /etc/udev/rules.d/
command -v setserial >/dev/null || sudo apt-get install -y setserial
sudo udevadm control --reload-rules && sudo udevadm trigger

echo "== 3/3 serial permissions"
if id -nG "$USER" | grep -qw dialout; then
  echo "$USER is already in dialout"
else
  sudo usermod -aG dialout "$USER"
  echo "Added $USER to dialout - log out and back in for it to apply."
fi
echo
echo "Done. Try:  dr304 ports    then    dr304 --serial /dev/dr304 --baud auto info"
