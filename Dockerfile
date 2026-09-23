FROM ghcr.io/astral-sh/uv:python3.12-alpine@sha256:bba3bd4965901846f025ae7375402ba859862bb83181f19d7e4a82f0f1f8388e AS builder
WORKDIR /opt/remnashop
RUN apk add --no-cache git
COPY pyproject.toml uv.lock ./
COPY vendor/remnapy/remnapy-2.7.1.dev7+cleanpay.g06802538-py3-none-any.whl ./vendor/remnapy/
COPY deploy/patch_remnapy_compat.py ./deploy/patch_remnapy_compat.py
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --compile-bytecode \
    && .venv/bin/python deploy/patch_remnapy_compat.py \
    && rm -rf .venv/lib/python3.12/site-packages/{pip,setuptools,wheel}*

FROM python:3.12-alpine@sha256:b64631e04e4920160c50fbe8d8df828f7f35f06f425cb44aa09bca53e708a35a AS final
WORKDIR /opt/remnashop

ARG BUILD_TIME
ARG BUILD_BRANCH
ARG BUILD_COMMIT
ARG BUILD_TAG

ENV BUILD_TIME=${BUILD_TIME}
ENV BUILD_BRANCH=${BUILD_BRANCH}
ENV BUILD_COMMIT=${BUILD_COMMIT}
ENV BUILD_TAG=${BUILD_TAG}

RUN apk add --no-cache postgresql-client su-exec \
    && addgroup -S -g 10001 remnashop \
    && adduser -S -D -H -u 10001 -G remnashop remnashop \
    && mkdir -p \
        /opt/remnashop/assets \
        /opt/remnashop/backups \
        /opt/remnashop/logs \
        /opt/remnashop/tmp \
    && chown -R remnashop:remnashop /opt/remnashop

COPY --from=builder --chown=remnashop:remnashop /opt/remnashop/.venv /opt/remnashop/.venv
ENV PATH="/opt/remnashop/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONPATH=/opt/remnashop

COPY --chown=remnashop:remnashop ./src ./src
COPY --chown=remnashop:remnashop ./assets /opt/remnashop/assets.default

# Release builds must use a real application version (for example, v0.8.3).
# Docker image names and commit identifiers belong in the image tag/labels,
# not in BUILD_TAG, because the hourly update task parses this value.
RUN if [ -n "$BUILD_TAG" ] && [ "$BUILD_TAG" != "dev" ]; then \
        python -c 'import sys; from packaging.version import Version; Version(sys.argv[1].removeprefix("v"))' "$BUILD_TAG"; \
    fi

COPY --chown=remnashop:remnashop ./docker-entrypoint.sh ./docker-entrypoint.sh
COPY --chown=remnashop:remnashop ./docker-migrate.sh ./docker-migrate.sh
COPY --chown=remnashop:remnashop ./docker-user-entrypoint.sh ./docker-user-entrypoint.sh
RUN sed -i 's/\r$//' ./docker-entrypoint.sh \
    && sed -i 's/\r$//' ./docker-migrate.sh \
    && sed -i 's/\r$//' ./docker-user-entrypoint.sh \
    && chmod +x ./docker-entrypoint.sh ./docker-migrate.sh ./docker-user-entrypoint.sh
USER 10001:10001
ENTRYPOINT ["./docker-user-entrypoint.sh"]
CMD ["./docker-entrypoint.sh"]
