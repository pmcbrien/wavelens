#!/bin/bash
# Kept so older instructions still work: the P25 decoder is now APCO25, built from this repository.
exec "$(cd "$(dirname "$0")" && pwd)/install-apco25.sh" "$@"
