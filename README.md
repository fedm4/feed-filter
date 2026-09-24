# feed-filter

An RSS feed filter. It pulls a curated set of sources and uses [Laya][laya] — a local,
self-hosted decision model — to score each item for relevance and sensationalism, and to
tag whether it is reporting, analysis, opinion or promotion. Opinion is always marked,
never dropped in silence.

## Running Laya

Laya runs **natively on the host**, not in the Docker stack: the model is large, slow to
load and shared between the dev machine and the server. The application reaches it over
HTTP at `FF_LAYA_BASE_URL`.

Linux and macOS. On a Mac it uses the GPU straight away; on Linux it is CPU until a
GPU build is installed:

```sh
scripts/laya.sh install    # virtualenv, torch, laya, and the checkpoints
scripts/laya.sh dev        # foreground, for development
scripts/laya.sh status     # ask a running instance for a real decision
```

On a Linux server, install it as a systemd user service instead:

```sh
scripts/laya.sh install-service
loginctl enable-linger "$USER"   # otherwise it stops when you log out
journalctl --user -u laya -f
```

There is no `install-service` on macOS: systemd does not exist there and the server
this project deploys to is Linux, so a launchd agent would be code nobody runs. Use
`scripts/laya.sh dev` on a Mac.

| Variable | Default | Notes |
|---|---|---|
| `LAYA_HOME` | `~/.local/share/feed-filter-laya` | install directory |
| `LAYA_MODELS` | `english,multilingual` | kept loaded; the server routes by detected language |
| `LAYA_BIND` | `0.0.0.0` | must not be `127.0.0.1`, or containers cannot reach it |
| `LAYA_PORT` | `8000` | |
| `LAYA_THREADS` | physical cores | logical threads hurt more than they help |
| `LAYA_API_KEY` | unset | required on every request when set |
| `LAYA_ACCEL` | `cpu` | which torch build to install: `cpu`, `cuda`, `rocm` |
| `LAYA_DEVICE` | `mps` on macOS, else `cpu` | which device to run on |
| `LAYA_TORCH_INDEX` | unset | override the wheel index for another CUDA or ROCm release |

> `LAYA_BIND` defaults to `0.0.0.0` because the container reaches Laya through the host
> gateway, and `127.0.0.1` would refuse that. This exposes the port to your local
> network. On a machine you do not trust, set `LAYA_API_KEY`, or bind to the Docker
> bridge address instead.

torch is installed **before** Laya on purpose: the other order lets pip resolve the
default build, which on Linux is the CUDA one — gigabytes of wheels a machine without an
NVIDIA GPU cannot use.

### Running on a GPU

The two variables answer different questions. `LAYA_ACCEL` picks the torch wheel at
install time; `LAYA_DEVICE` picks the device at run time. They are separate on purpose, so
passing `LAYA_DEVICE` never silently reinstalls gigabytes behind your back.

**On macOS there is nothing to do.** Apple ships a single torch wheel carrying CPU and
Metal together, so there is no GPU build to install and no `LAYA_ACCEL` value for it —
`LAYA_DEVICE` simply defaults to `mps`:

```sh
scripts/laya.sh install
scripts/laya.sh dev          # already on the GPU
LAYA_DEVICE=cpu scripts/laya.sh dev    # opt back out
```

MLX is not involved. It is a separate Apple framework, not a torch backend, and Laya has
no MLX path — Metal is reached through torch's MPS backend, which Laya supports directly:
it falls back to CPU with a printed warning when Metal is missing, and gates fp16 autocast
to batches of five rows or more, below which fp16 is slower than fp32 on MPS.

**On Linux the GPU is opt-in, at both steps:**

```sh
LAYA_ACCEL=rocm scripts/laya.sh install    # once
LAYA_DEVICE=cuda scripts/laya.sh dev       # every run
```

| `LAYA_ACCEL` | Platform | Wheel index | Run with |
|---|---|---|---|
| `cpu` | Linux, macOS | pytorch `cpu` on Linux, plain PyPI on macOS | `cpu`, or `mps` on a Mac |
| `cuda` | Linux, NVIDIA | pytorch `cu128` | `LAYA_DEVICE=cuda` |
| `rocm` | Linux, AMD | pytorch `rocm7.2` | `LAYA_DEVICE=cuda` |

That last row is not a typo. **A ROCm build still calls its device `cuda`**:
`torch.cuda.is_available()` returns true on an AMD card, and `LAYA_DEVICE=rocm` is not a
thing.

After a GPU install the script asks torch whether it can actually see the card, and
refuses the install if it cannot. A wheel that resolves is not a wheel that runs: ROCm
builds carry a fixed list of compiled architectures, and a card outside that list would
otherwise fail at the first request rather than at install time.

Verified on RDNA4 (Radeon RX 9060 XT, `gfx1200`, ROCm 7.2): `torch 2.14.0+rocm7.2` lists
`gfx1200` and `gfx1201` among its compiled architectures and runs on the card with no
`HSA_OVERRIDE_GFX_VERSION` override. Serving is not where a GPU pays for itself — the
decision model is small — but fine-tuning is, and that is the project's critical path.

### Accuracy: measured, not assumed

The base checkpoints are a starting point, not a finished classifier. Measured on this
host against five hand-written samples, zero-shot:

| Sample | Sensationalism (0–3) | Content type |
|---|---|---|
| Sober English rate-rise cable | 0.94 | **opinion** (wrong) |
| Spanish tabloid outrage bait | 1.97 | **analysis** (wrong) |
| Spanish unemployment-figures cable | 1.46 | news |
| Spanish opinion column | 0.98 | opinion |
| English sponsored product review | 1.32 | promotion |

The ordering is right — the tabloid piece always scores above the sober cables — but the
margin is thin: 1.97 against 1.46 on a three-point scale. Sharpening the criteria wording
moved the tabloid from 1.26 to 1.97 and is a real lever, but it lifted the sober cable
from 0.92 to 1.46 alongside it. Content type is worse: a plain wire cable came back as
opinion.

So thresholds start permissive, and the 👍/👎 labels the feed collects are the path to a
model that actually discriminates. On the published benchmark the gap is the same shape:
0.342–0.362 zero-shot against 0.318 for random guessing, versus 0.766 fine-tuned.

Reproduce with `scripts/bench_classify.py` once B3 lands.

## Development

```sh
make check    # ruff, ruff format --check, pytest
```

Configuration is documented in `.env.example`. Every scalar setting is an `FF_`-prefixed
environment variable; sources, the interest profile and the classifier criteria live in
`config.yaml`.

## Scaffold

This repository is based on [claude-scaffold][scaffold], pulled as a git remote. To take
later updates:

```sh
git pull scaffold main
```

[laya]: https://huggingface.co/convaiinnovations/laya-multilingual
[scaffold]: https://github.com/fedm4/claude-scaffold
