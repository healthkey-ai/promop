"""The GCP image must bake the embedding model the code loads (#1847).

Cloud Run hands every container a fresh filesystem, so a model that is not in
the image is fetched from huggingface.co at runtime, unauthenticated, from a
shared egress NAT. One measured suggestion spent 121s of its 167s asleep in
the resulting rate-limit backoff, for 0.3s of actual embedding. A name that
drifts between the Dockerfile and the code brings that back silently, which is
what this file is here to prevent.
"""
import re
from pathlib import Path

from omop_core.mapping.suggestions import EMBEDDING_MODEL_NAME

DOCKERFILE = Path(__file__).resolve().parent.parent / 'Dockerfile.gcp'


def _dockerfile():
    return DOCKERFILE.read_text()


def test_dockerfile_bakes_the_model_the_code_loads():
    match = re.search(r'^ENV EMBEDDING_MODEL_NAME=(\S+)', _dockerfile(), re.MULTILINE)
    assert match, 'Dockerfile.gcp no longer declares EMBEDDING_MODEL_NAME'
    assert match.group(1) == EMBEDDING_MODEL_NAME


def test_runtime_stage_reads_the_baked_cache_offline():
    text = _dockerfile()
    # The final stage is what runs on Cloud Run; the build stage downloads.
    runtime = text.rsplit('FROM python:3.12-slim', 1)[1]
    assert 'HF_HOME=/opt/huggingface' in runtime
    assert 'HF_HUB_OFFLINE=1' in runtime
    assert 'COPY --from=backend /opt/huggingface /opt/huggingface' in runtime


def test_the_model_download_is_cached_above_the_source_copy():
    """A code change must not invalidate the download layer."""
    text = _dockerfile()
    assert text.index('EMBEDDING_MODEL_NAME') < text.index('COPY . .')
