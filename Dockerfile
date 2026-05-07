# Pre-baked runner image for `qa.py`. Inherits catthehacker (the standard
# act runner image — has node/npm/python/git/jq/sudo all configured the way
# act expects), then bakes Playwright + chromium on top so we don't pay
# the 3-minute install cost on every local act run.
#
# Built multi-arch (amd64 + arm64) by .github/workflows/build-image.yml in
# this repo, published to ghcr.io/pagerguild/qa-cli/runner:latest. The wrapper
# pins to a digest, not :latest, so cache-busting is explicit.
#
# Local override during dev: `docker build -t qa-cli/runner:local .` and pass
# `--runner-image qa-cli/runner:local` to qa.py.

FROM catthehacker/ubuntu:act-latest

# Auto-link the published GHCR package to this repo so visibility flips
# (private → public) appear in pagerguild/qa's package UI.
LABEL org.opencontainers.image.source=https://github.com/pagerguild/qa

# Pin Playwright to a known-good version. Bump in lockstep with whatever the
# personas expect; the chromium binary version is tied to the Playwright
# release. Re-bake the image when this changes.
ARG PLAYWRIGHT_VERSION=1.49.1

RUN set -eux; \
    npx -y playwright@${PLAYWRIGHT_VERSION} install --with-deps chromium; \
    CHROME_BIN=$(find /root/.cache/ms-playwright -path '*/chromium-*/chrome-linux*/chrome' -type f | head -1); \
    test -n "$CHROME_BIN"; \
    ln -sf "$CHROME_BIN" /usr/bin/chromium; \
    test -x /usr/bin/chromium; \
    echo "baked $CHROME_BIN → /usr/bin/chromium"

# Bake in resend-listener — the CLI personas use to receive verification emails
# during signup flows. Public release, no auth needed. TARGETARCH is set
# automatically by buildx for the multi-arch build (amd64 / arm64).
ARG RESEND_LISTENER_VERSION=0.3.0
ARG TARGETARCH
RUN set -eux; \
    url="https://github.com/pagerguild/resend-listener/releases/download/v${RESEND_LISTENER_VERSION}/resend-listener_${RESEND_LISTENER_VERSION}_linux_${TARGETARCH}.tar.gz"; \
    curl -fsSL "$url" | tar -xzC /usr/local/bin resend-listener; \
    chmod +x /usr/local/bin/resend-listener; \
    test -x /usr/local/bin/resend-listener; \
    printf '%s\n' '#!/bin/sh' 'exec /usr/local/bin/resend-listener -wait "$@"' \
        > /usr/local/bin/check-mail; \
    chmod +x /usr/local/bin/check-mail; \
    echo "baked resend-listener ${RESEND_LISTENER_VERSION} (${TARGETARCH}) + check-mail wrapper"

# Reset to the entrypoint catthehacker expects so act's container lifecycle
# (tail -f /dev/null then docker exec) keeps working.
ENTRYPOINT []
CMD ["bash"]
