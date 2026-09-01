#!/usr/bin/env bash
# Test contract: the state machine + a real tiny.en transcription of TTS audio.
set -e
cd "$(dirname "$0")"
python tests/test_murmur.py
python tests/test_window.py
python tests/test_ui_rules.py
python tests/test_brand.py
python tests/test_promptify.py
python tests/test_connect.py
python tests/test_vault.py
