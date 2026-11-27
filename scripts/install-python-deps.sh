#!/bin/bash
#
# Install the Python dependencies on a Render build, with CPU-only torch (#2937).
#
# `sentence-transformers` depends on torch, and the default Linux wheel brings
# about 3 GB of NVIDIA CUDA libraries (cudnn, cublas, triton, nccl, ...). Render
# has no GPU, so none of it is ever loaded; it is only installed, compressed,
# uploaded, restored from the build cache and pulled by every instance on every
# deploy. The CPU wheel is ~200 MB. Dockerfile.gcp does the same for Cloud Run.
#
# torch goes in first, from the CPU index and without its dependencies (the CPU
# index is not a mirror of PyPI), so that `pip install -r requirements.txt`
# finds `torch>=2.2` already satisfied and resolves torch's own dependencies
# from PyPI as usual. If the CPU index cannot be reached the build carries on
# with the default wheel: slower, never broken.
set -euo pipefail

cd "$(dirname "$0")/.."

TORCH_CPU_INDEX_URL="${TORCH_CPU_INDEX_URL:-https://download.pytorch.org/whl/cpu}"

echo "Installing CPU-only torch from ${TORCH_CPU_INDEX_URL}..."
if ! pip install --no-deps --index-url "$TORCH_CPU_INDEX_URL" 'torch>=2.2'; then
  echo "WARNING: CPU-only torch could not be installed; falling back to the default wheel (CUDA libraries included)." >&2
fi

pip install -r requirements.txt

# A build whose virtualenv was restored from an earlier CUDA install would keep
# the libraries nothing depends on any more. Remove them; a no-op otherwise.
leftovers=$(pip list --format=freeze | grep -iE '^(nvidia-|cuda-|triton==)' | cut -d= -f1 || true)
if [ -n "$leftovers" ] && python -c "import importlib.metadata as m, sys; sys.exit(0 if '+cpu' in m.version('torch') else 1)"; then
  echo "Removing unused CUDA libraries left by an earlier install..."
  # shellcheck disable=SC2086
  pip uninstall -y $leftovers
fi
