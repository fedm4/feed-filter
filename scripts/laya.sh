#!/usr/bin/env bash
# Install and run Laya natively on the host.
#
# Laya is deliberately kept out of the Docker stack: the model is large, slow to
# load and shared between the dev machine and the server. The application reaches
# it over HTTP at FF_LAYA_BASE_URL.
#
# Linux and macOS. CPU by default; CUDA, ROCm and Metal are opt-in.
set -euo pipefail

LAYA_HOME="${LAYA_HOME:-$HOME/.local/share/feed-filter-laya}"
LAYA_VENV="$LAYA_HOME/venv"
LAYA_MODELS="${LAYA_MODELS:-english,multilingual}"
LAYA_BIND="${LAYA_BIND:-0.0.0.0}"
LAYA_PORT="${LAYA_PORT:-8000}"
LAYA_ACCEL="${LAYA_ACCEL:-cpu}"
# macOS ships one torch wheel carrying both CPU and Metal, so there is no
# install-time choice to fall out of sync with: default to the GPU there.
# Laya itself warns and drops to CPU if Metal turns out to be missing.
case "$(uname -s)" in
    Darwin) LAYA_DEVICE="${LAYA_DEVICE:-mps}" ;;
    *)      LAYA_DEVICE="${LAYA_DEVICE:-cpu}" ;;
esac

usage() {
    cat <<'USAGE'
Usage: scripts/laya.sh <command>

  install          create the virtualenv, install torch and laya, fetch the model
  dev              run in the foreground, logs to stdout
  install-service  install and enable a systemd user service (Linux servers)
  status           check that a running instance answers a real decision

Environment:
  LAYA_HOME     install directory   (default ~/.local/share/feed-filter-laya)
  LAYA_MODELS   checkpoints to keep loaded, comma separated
                (default english,multilingual -- the server auto-routes by language)
  LAYA_BIND     bind address        (default 0.0.0.0, so containers can reach it)
  LAYA_PORT     port                (default 8000)
  LAYA_THREADS  torch threads       (default: physical cores)
  LAYA_API_KEY  require this key on every request

  LAYA_ACCEL    which torch build to install (default cpu):
                  cpu   portable, no GPU -- and the only option on macOS, whose
                        single wheel already carries Metal
                  cuda  NVIDIA, Linux
                  rocm  AMD, Linux
  LAYA_DEVICE   which device to run on. Defaults to mps on macOS, so a Mac uses
                its GPU out of the box, and to cpu elsewhere, where a GPU build
                has to be installed first. Deliberately independent from
                LAYA_ACCEL, which only decides what gets installed. A ROCm build
                still calls its device "cuda", so an AMD GPU wants
                LAYA_DEVICE=cuda.
  LAYA_TORCH_INDEX
                override the wheel index, for a CUDA or ROCm release other than
                the ones pinned below
USAGE
}

die() { echo "$*" >&2; exit 1; }

physical_cores() {
    case "$(uname -s)" in
        Darwin) sysctl -n hw.physicalcpu 2>/dev/null || echo 4 ;;
        *)
            if command -v lscpu >/dev/null 2>&1; then
                lscpu -p=Core,Socket 2>/dev/null | grep -v '^#' | sort -u | wc -l && return
            fi
            nproc 2>/dev/null || echo 4
            ;;
    esac
}

# An accelerator is tied to an operating system: ROCm and CUDA are Linux-only
# here, Metal exists only on macOS. Catching the mismatch now beats a wheel that
# resolves, installs and then finds no device.
require_supported_accel() {
    case "$LAYA_ACCEL:$(uname -s)" in
        cpu:*|cuda:Linux|rocm:Linux) ;;
        cuda:*|rocm:*) die \
            "LAYA_ACCEL=$LAYA_ACCEL needs Linux. On macOS the default cpu build
already reaches the GPU through Metal: install it and run as usual." ;;
        *) die "Unknown LAYA_ACCEL=$LAYA_ACCEL (cpu, cuda, rocm)." ;;
    esac
}

# The wheel index for LAYA_ACCEL, empty for plain PyPI. The macOS wheels are on
# PyPI only and carry CPU and Metal together, which is why macOS has no GPU
# accelerator of its own to pick.
torch_index() {
    if [ -n "${LAYA_TORCH_INDEX:-}" ]; then echo "$LAYA_TORCH_INDEX"; return; fi
    case "$LAYA_ACCEL" in
        cpu)  [ "$(uname -s)" = Darwin ] || echo "https://download.pytorch.org/whl/cpu" ;;
        cuda) echo "https://download.pytorch.org/whl/cu128" ;;
        rocm) echo "https://download.pytorch.org/whl/rocm7.2" ;;
    esac
}

require_uv() {
    command -v uv >/dev/null 2>&1 || die \
        "uv is required: https://docs.astral.sh/uv/getting-started/installation/"
}

# A GPU wheel that installs is not a GPU wheel that runs: a ROCm build carries a
# fixed list of compiled architectures, and a card outside it fails at the first
# request. Ask torch here, while the answer is still cheap to act on.
verify_accel() {
    "$LAYA_VENV/bin/python" - "$LAYA_ACCEL" <<'PY'
import sys

import torch

accel = sys.argv[1]
if not torch.cuda.is_available():
    sys.exit(
        f"torch {torch.__version__} sees no {accel.upper()} device. "
        "Reinstall with LAYA_ACCEL=cpu."
    )
props = torch.cuda.get_device_properties(0)
print(f"{torch.cuda.get_device_name(0)} ({props.gcnArchName})")
PY
}

do_install() {
    require_supported_accel
    require_uv
    mkdir -p "$LAYA_HOME"
    uv venv "$LAYA_VENV"

    # torch first, on its own. Installing laya first would let pip resolve the
    # default build -- on Linux that is the CUDA one, gigabytes of wheels a host
    # without an NVIDIA GPU cannot use.
    local index
    index="$(torch_index)"
    if [ -n "$index" ]; then
        uv pip install --python "$LAYA_VENV/bin/python" --index-url "$index" torch
    else
        uv pip install --python "$LAYA_VENV/bin/python" torch
    fi
    [ "$LAYA_ACCEL" = cpu ] || verify_accel

    uv pip install --python "$LAYA_VENV/bin/python" "laya[serve]"

    echo "Fetching checkpoints: $LAYA_MODELS ..."
    # Both by default: the server routes by detected language, so an English
    # article reaches the english checkpoint and a Spanish one the multilingual
    # checkpoint. Preloading only one leaves a surprise download at first use.
    "$LAYA_VENV/bin/python" -c "
from laya import Router
Router().preload('$LAYA_MODELS'.split(','))
"
    echo "Installed in $LAYA_HOME ($LAYA_ACCEL build)"
}

serve_env() {
    export LAYA_DEVICE
    export LAYA_PRELOAD=1
    export LAYA_MODELS="$LAYA_MODELS"
    export LAYA_HOST="$LAYA_BIND"
    export LAYA_PORT="$LAYA_PORT"
    export LAYA_THREADS="${LAYA_THREADS:-$(physical_cores)}"
}

do_dev() {
    [ -x "$LAYA_VENV/bin/laya-serve" ] || die \
        "Not installed yet. Run: scripts/laya.sh install"
    serve_env
    echo "Laya on $LAYA_BIND:$LAYA_PORT, models $LAYA_MODELS, device $LAYA_DEVICE, $LAYA_THREADS threads"
    exec "$LAYA_VENV/bin/laya-serve"
}

do_install_service() {
    [ "$(uname -s)" = Linux ] || die \
        "install-service uses systemd and is Linux-only. On macOS run it in the
foreground instead:

  scripts/laya.sh dev"

    [ -x "$LAYA_VENV/bin/laya-serve" ] || do_install

    local unit_dir="$HOME/.config/systemd/user"
    mkdir -p "$unit_dir"
    sed -e "s|@VENV@|$LAYA_VENV|g" \
        -e "s|@MODELS@|$LAYA_MODELS|g" \
        -e "s|@BIND@|$LAYA_BIND|g" \
        -e "s|@PORT@|$LAYA_PORT|g" \
        -e "s|@DEVICE@|$LAYA_DEVICE|g" \
        -e "s|@THREADS@|${LAYA_THREADS:-$(physical_cores)}|g" \
        "$(dirname "$0")/laya.service.template" > "$unit_dir/laya.service"

    systemctl --user daemon-reload
    systemctl --user enable --now laya.service
    echo "Enabled on device $LAYA_DEVICE. Logs: journalctl --user -u laya -f"
    echo "Survives logout only with: loginctl enable-linger $USER"
}

do_status() {
    local base="http://${LAYA_BIND/0.0.0.0/127.0.0.1}:$LAYA_PORT"
    curl -fsS "$base/health" || die "No answer from $base/health"
    echo
    # /health returns before the weights finish loading, so also ask for a real
    # decision: that is what the application actually depends on.
    curl -fsS "$base/v1/systemone" -H 'content-type: application/json' -d '{
        "state": {"body": "The central bank raised rates by 25 basis points."},
        "questions": {"kind": {"type": "choice", "instructions": "What is this?",
                      "criteria": {"news": "factual report", "opinion": "an argued position"}}}
    }' || die "Model not answering decisions yet"
    echo
}

case "${1:-}" in
    install)         do_install ;;
    dev)             do_dev ;;
    install-service) do_install_service ;;
    status)          do_status ;;
    *)               usage; exit 1 ;;
esac
