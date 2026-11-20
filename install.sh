#!/usr/bin/env bash
# storygen installer (Ubuntu/Debian; macOS with Homebrew works too). Everything installed is free.
set -euo pipefail
cd "$(dirname "$0")"

echo "==> System packages (ffmpeg with libass, python venv)"
if command -v apt-get >/dev/null; then
  sudo apt-get update -qq && sudo apt-get install -y -qq ffmpeg python3-venv python3-pip curl
elif command -v brew >/dev/null; then
  brew install ffmpeg python curl
fi

echo "==> Python environment"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -e ".[dev]"

echo "==> Expressive voice: Chatterbox Turbo (MIT) in its own environment (CPU-only torch, ~1.9 GB)"
# Chatterbox pins numpy 1.x / torch 2.6, which clash with the main environment, so it gets .venv-tts.
# Skip with NO_CHATTERBOX=1; storygen then uses Kokoro.
if [ "${NO_CHATTERBOX:-0}" != "1" ]; then
  [ -d .venv-tts ] || python3 -m venv .venv-tts
  .venv-tts/bin/pip install -q --upgrade pip
  .venv-tts/bin/pip install -q chatterbox-tts
  .venv-tts/bin/pip install -q --force-reinstall --no-deps torch==2.6.0 torchaudio==2.6.0 \
    --index-url https://download.pytorch.org/whl/cpu
  .venv-tts/bin/pip list 2>/dev/null | grep -iE "^nvidia|^triton" | awk '{print $1}' | xargs -r .venv-tts/bin/pip uninstall -q -y
  echo "   pre-downloading the Chatterbox Turbo weights (~1.5 GB, once)"
  .venv-tts/bin/python -c "from chatterbox.tts_turbo import ChatterboxTurboTTS as M; M.from_pretrained(device='cpu')" >/dev/null
fi

echo "==> Voice model (Kokoro-82M, Apache-2.0) and font (Anton, OFL)"
mkdir -p assets/models assets/fonts
KOKORO=https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0
[ -f assets/models/kokoro-v1.0.int8.onnx ] || curl -fsSL -o assets/models/kokoro-v1.0.int8.onnx "$KOKORO/kokoro-v1.0.int8.onnx"
[ -f assets/models/voices-v1.0.bin ] || curl -fsSL -o assets/models/voices-v1.0.bin "$KOKORO/voices-v1.0.bin"
[ -f assets/fonts/Anton-Regular.ttf ] || curl -fsSL -o assets/fonts/Anton-Regular.ttf \
  https://github.com/google/fonts/raw/main/ofl/anton/Anton-Regular.ttf

echo "==> Claude Code (the only subscription: your Claude Pro plan)"
if ! command -v claude >/dev/null; then
  curl -fsSL https://claude.ai/install.sh | bash
  echo "   Now run 'claude' once and log in with your Pro account."
fi

echo "==> Pre-downloading the Whisper model used to time the captions"
.venv/bin/python -c "from faster_whisper import WhisperModel; WhisperModel('base', device='cpu', compute_type='int8')" >/dev/null

[ -f storygen.toml ] || cp storygen.example.toml storygen.toml
echo
.venv/bin/storygen doctor --skip-llm || true
echo
echo "Done. Try:  .venv/bin/storygen make"
