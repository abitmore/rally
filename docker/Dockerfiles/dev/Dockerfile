FROM docker.elastic.co/wolfi/chainguard-base:latest AS builder
ARG RALLY_VERSION
ARG RALLY_LICENSE

ENV RALLY_RUNNING_IN_DOCKER=True

USER root

RUN apk update
RUN for i in 1 2 3; do \
      apk add --no-cache bash bzip2 bzip2-dev curl git gcc gzip jq make openssh pigz procps wget zstd && break || sleep 5; \
    done

# pbzip2 doesn't have a package for wolfi, so we build it from source
RUN cd /tmp && \
  wget -q https://launchpad.net/pbzip2/1.1/1.1.13/+download/pbzip2-1.1.13.tar.gz && \
  tar -xzf pbzip2-1.1.13.tar.gz && \
  cd pbzip2-1.1.13/ && \
  make install && \
  rm -r /tmp/pbzip2-1.1.13/

RUN addgroup --gid 1000 rally && \
    adduser --system --home /rally --ingroup rally --no-create-home --uid 1000 --shell /bin/bash rally

WORKDIR /rally
COPY --chown=1000:0 docker/bin/entrypoint.sh /entrypoint.sh

# Openshift overrides USER and uses random ones with uid>1024 and gid=0
# Allow ENTRYPOINT (and Rally) to run even with a different user
RUN chgrp 0 /entrypoint.sh && \
    chmod g=u /etc/passwd && \
    chmod 0775 /entrypoint.sh

RUN mkdir -p /rally/.rally && \
    chown -R 1000:0 /rally/

COPY --from=ghcr.io/astral-sh/uv:0.7.12 /uv /uvx /bin/

USER 1000

COPY Makefile /rally/
COPY README.md /rally/
COPY .ci/variables.json /rally/.ci/
COPY esrally/ /rally/esrally/
COPY pyproject.toml /rally/
COPY uv.lock /rally/

ENV VIRTUAL_ENV=/rally/venv
ENV PATH=/rally/venv/bin:$PATH
ENV UV_PROJECT_ENVIRONMENT=/rally/venv

# Wipe away any lingering caches, copied over from the local machine
RUN find /rally -name "__pycache__" -exec rm -rf -- \{\} \; 2>/dev/null || true
RUN find /rally -name ".pyc" -exec rm -rf -- \{\} \; 2>/dev/null || true
RUN make install

LABEL org.label-schema.schema-version="1.0" \
  org.label-schema.vendor="Elastic" \
  org.label-schema.name="rally" \
  org.label-schema.version="${RALLY_VERSION}" \
  org.label-schema.url="https://esrally.readthedocs.io/en/stable/" \
  org.label-schema.vcs-url="https://github.com/elastic/rally" \
  license="${RALLY_LICENSE}"

VOLUME ["/rally/.rally"]

ENTRYPOINT ["/entrypoint.sh"]
