#!/bin/bash
# Update the Wavelens bridge on this Pi from the checked-out repo: pull, install, restart, tail the journal.
# Run from anywhere: ~/wavelens/companion/pi/update.sh
set -e
cd "$(dirname "$0")/../.."
git pull --ff-only
sudo install -m 0755 companion/pi/wavelens_bridge.py /usr/local/lib/wavelens/wavelens_bridge.py
sudo install -m 0755 companion/pi/run-bridge.sh /usr/local/lib/wavelens/run-bridge.sh 2>/dev/null || true
sudo systemctl restart wavelens-bridge
echo "bridge restarted from $(git rev-parse --short HEAD); journal follows (Ctrl-C to stop)"
exec journalctl -u wavelens-bridge -f --no-pager | grep -v '"t":"rf"\|"t":"spec"'
