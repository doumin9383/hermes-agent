# Hermes Agent Fork

Local fork for home-cluster Hermes fixes.

Current local fix:

- route Mattermost metadata.thread_id to root_id for threaded tool/file responses.

Focused validation:

uv run --with pytest --with pytest-asyncio pytest tests/gateway/test_mattermost.py -q -o addopts=

Build and push a fixed image after Docker and GHCR auth are available:

scripts/build-mattermost-fix-image.sh

Default stable tag:

ghcr.io/doumin9383/hermes-agent:mattermost-thread-fix

Then switch the k3s deployments with:

/home/sh-fukue/k3s-home-cluster/agents/fork-image/switch-mattermost-hermes-to-fork-image.sh
