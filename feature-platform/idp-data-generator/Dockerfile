# AgentCore Runtime container for the SEED document generator (extension build).
#
# Build context = the agent-source.zip root (see buildspec.yml). The vendored
# layout is flatter than the host's nested stack: idp_common_pkg, the SEED
# generator, the runtime handler and requirements all live under agent-source/.
#
# AgentCore Runtime requires a linux/arm64 image serving the HTTP service
# contract on port 8080. A full Debian base is used (not the Lambda base)
# because WeasyPrint's native stack (Pango, cairo, GDK-Pixbuf, HarfBuzz,
# fontconfig, fonts) and OpenCV install cleanly there. The Python base is
# pulled from the ECR Public mirror to avoid Docker Hub rate limits in CodeBuild.
FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# WeasyPrint runtime libs + fonts; build-essential/libffi-dev for any wheels
# without prebuilt arm64 artifacts; libgl1/libglib2.0-0 for OpenCV (augraphy
# pulls in non-headless opencv-python, which imports cv2 at module load).
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpango-1.0-0 \
        libpangocairo-1.0-0 \
        libpangoft2-1.0-0 \
        libcairo2 \
        libgdk-pixbuf-2.0-0 \
        libffi-dev \
        libharfbuzz0b \
        libfribidi0 \
        libfontconfig1 \
        libgl1 \
        libglib2.0-0 \
        fonts-dejavu-core \
        fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# SEED is now the published `seed-data` PyPI package (module `seed_data`) — no
# longer vendored as local source. Installing it pulls the generator plus its
# bundled schema library/packages and dependencies (strands, weasyprint,
# reportlab, xhtml2pdf, etc.). From 0.0.7 the image-augmentation libraries
# (augraphy, opencv) are only in SEED's `augment` extra, which the extension's
# optional scan/fax effects need, so the extra is named here.
#
# seed-data is pinned to one release, because the tools SEED gives its models
# can change between releases and README.md records the tool inventory for this
# version ("What the generator's model can do"). Re-read that inventory against
# the new release's source when moving the pin. xhtml2pdf is pinned because the
# synthesis adapter renders through its ResourceAccessPolicy, which 0.2.17 (the
# lowest version SEED accepts) does not have; without it the adapter falls back
# to SEED's renderer.
RUN pip install "seed-data[augment]==0.0.7" "xhtml2pdf==0.2.28"

# Extra runtime deps not in seed-data (AgentCore HTTP server + SigV4 helpers)
# plus idp_common[synthesis] for the synthesis adapter glue.
# idp_common_pkg is staged into the build-context root by package_agent_source.sh;
# requirements.txt references it as ./idp_common_pkg[synthesis].
COPY idp_common_pkg /app/idp_common_pkg
COPY agent-source/requirements.txt /tmp/runtime-requirements.txt
RUN cd /app && pip install -r /tmp/runtime-requirements.txt

# AgentCore runtime entrypoint (the /invocations + /ping HTTP server).
COPY agent-source/runtime/*.py /app/

# /app stays owned by root, so the runtime user can read the code it runs but
# not write into the directory it is started from. The runtime writes only
# under tempfile.mkdtemp(). PYTHONSAFEPATH keeps the script's directory off
# sys.path; handler.py imports no sibling module, so nothing needs it there.
ENV PYTHONSAFEPATH=1
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8080

CMD ["python", "handler.py"]
