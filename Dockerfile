FROM alpine/git@sha256:2306e2b05ee0ef99f5d25b0779f645b24ae9f91ba187107503592fcdfe78da3a AS source
RUN git init -q /checkout && \
    git -C /checkout fetch --depth=1 https://github.com/tobymao/sqlglot.git eb11aec4be8cb2122afe713a4816662c3f0fb2fa && \
    test "$(git -C /checkout rev-parse FETCH_HEAD)" = eb11aec4be8cb2122afe713a4816662c3f0fb2fa && \
    git -C /checkout archive --format=tar --output=/tmp/sqlglot.tar FETCH_HEAD && \
    mkdir /source && tar -xf /tmp/sqlglot.tar -C /source

FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
WORKDIR /app
COPY --from=source /source /opt/sqlglot
COPY selector_v2.py select_workers.py dependencies.json lines.json ./
CMD ["python", "-B", "/app/selector_v2.py", "/data", "/opt/sqlglot"]
