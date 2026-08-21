# τ as a container.
#
# The image is built from the tarball `package.sh` produces, not from the source
# tree directly. That is deliberate: it means every image build also proves the
# release artifact is installable and complete. A data file missing from the
# tarball (this has happened — see tests/test_packaging.py) breaks the build here
# rather than in a user's first run.
#
# Targets:
#   runtime  — τ and nothing else. What a deployment ships.
#   verify   — runtime + the optional tau-jmfts package + a test runner.
#
#   docker build --target runtime -t ffwf/tau .
#   docker build --target verify  -t ffwf/tau-verify .

# ---------------------------------------------------------------- build stage
FROM python:3.12-slim AS build
WORKDIR /src
COPY tau-llm/ tau-llm/
COPY tau-agent-core/ tau-agent-core/
COPY tau-coding-agent/ tau-coding-agent/
COPY LICENSE package.sh ./
RUN bash package.sh && mkdir -p /dist && tar -xzf tau-*.tar.gz -C /dist --strip-components=1

# -------------------------------------------------------------- runtime stage
FROM python:3.12-slim AS runtime

# τ writes sessions and config under $HOME. A non-root user with a real home is
# the whole container-side story: no host mount, no shared ~/.tau.
RUN useradd --create-home --uid 10001 tau
ENV HOME=/home/tau \
    PATH=/home/tau/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY --from=build --chown=tau:tau /dist /opt/tau
USER tau
# Extras are explicit here because the base install is now headless-only.
#   [bus]  — this image is the one a Tectum node launches (see TAU_BIN below), and
#            the nats_bus extension is how it is reached.
#   [tui]  — kept so `docker run -it … tau` still opens the interface. Drop it for
#            a bus-only deployment: that removes textual and rich, ~14 packages.
RUN pip install --no-cache-dir --user \
      /opt/tau/tau-llm \
      "/opt/tau/tau-agent-core[bus]" \
      "/opt/tau/tau-coding-agent[tui]"

# The τ binary the Tectum node launches resolves through $TAU_BIN or PATH
# (tectum.endpoints.tau_bin) — never a baked-in path.
ENV TAU_BIN=/home/tau/.local/bin/tau

COPY --chown=tau:tau docker/tau-entrypoint.sh /usr/local/bin/tau-entrypoint
ENTRYPOINT ["/usr/local/bin/tau-entrypoint"]
CMD ["tau", "--help"]

# --------------------------------------------------------------- verify stage
FROM runtime AS verify
USER root
# tau-jmfts is optional and is NOT in the release tarball, so it is installed
# from source — the seam suites need it, a deployment does not.
COPY --chown=tau:tau tau-jmfts/ /opt/tau-jmfts/
USER tau
RUN pip install --no-cache-dir --user /opt/tau-jmfts \
 && pip install --no-cache-dir --user \
      pytest==8.3.4 pytest-asyncio==0.25.0 httpx nats-py
WORKDIR /verify
CMD ["pytest", "-q"]
