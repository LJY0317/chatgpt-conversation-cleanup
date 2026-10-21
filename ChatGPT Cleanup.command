#!/bin/zsh
set -eu

cd -- "${0:A:h}"
export PYTHONDONTWRITEBYTECODE=1

# macOS's /usr/bin/python3 may still be 3.9 even when a supported Homebrew
# interpreter is installed. Prefer a supported Python without changing the
# user's PATH or modifying any official application.
for candidate in python3.14 /opt/homebrew/bin/python3.14 python3 python3.13 python3.12 python3.11 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 11))' >/dev/null 2>&1; then
        exec "$candidate" run.py "$@"
    fi
done
print -u2 "Python 3.11 or newer is required to run ChatGPT Conversation Cleanup from source."
exit 1
