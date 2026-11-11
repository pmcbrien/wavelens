#!/bin/bash
# Update the Wavelens companion on this Pi. Kept for people with the old instructions; it is now a
# thin wrapper around wavelens-update (see wavelens-update for check / apply / rollback / status).
# Run from anywhere: ~/wavelens/companion/pi/update.sh
set -e
cd "$(dirname "$0")/../.."
if ! command -v wavelens-update >/dev/null 2>&1; then
    # first time with the new tool: install it from this checkout, then let it do the rest
    sudo install -m 0755 companion/pi/wavelens-update /usr/local/bin/wavelens-update
    sudo mkdir -p /etc/wavelens && pwd | sudo tee /etc/wavelens/source >/dev/null
fi
sudo WAVELENS_SRC="$(pwd)" wavelens-update apply --force
echo "journal follows (Ctrl-C to stop)"
exec journalctl -u wavelens-bridge -f --no-pager | grep -v '"t":"rf"\|"t":"spec"'
