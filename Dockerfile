# syntax=docker/dockerfile:1
FROM python:3.13-slim AS build
WORKDIR /src
COPY . .
RUN pip install --no-cache-dir build && python -m build --wheel --outdir /dist

FROM python:3.13-slim
LABEL org.opencontainers.image.title="downctl" \
      org.opencontainers.image.source="https://github.com/ThzxxArt/downctl" \
      org.opencontainers.image.licenses="MIT"
COPY --from=build /dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm -f /tmp/*.whl
WORKDIR /data
ENTRYPOINT ["downctl"]
CMD ["--help"]
