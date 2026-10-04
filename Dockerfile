# Qoder Multi-Account Reverse Proxy Gateway (CN + Intl)
FROM python:3.11-alpine

# Set environment
ENV PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8790 \
    API_KEY= \
    TZ=Asia/Shanghai

WORKDIR /app

# Alpine timezone & certs
RUN apk add --no-cache tzdata ca-certificates && \
    cp /usr/share/zoneinfo/${TZ} /etc/localtime && \
    echo "${TZ}" > /etc/timezone

# Copy application files (Zero external pip dependencies needed -
# AES/RSA/COSY signing are pure-stdlib implementations)
COPY qoder_proxy.py ./
COPY qoder2api/ ./qoder2api/

# 前端静态产物（源码在 web/，构建命令 npm run build:export；产物已入库，
# 因此镜像构建不需要 Node）
COPY web/out/ ./web/out/

# 迁移期的旧看板（挂在 /legacy；下个版本移除）
COPY legacy/dashboard.html ./legacy/dashboard.html

# Create data directories
RUN mkdir -p /app/accounts /app/usage

# Volume persistence for credentials and usage logs
VOLUME ["/app/accounts", "/app/usage"]

EXPOSE 8790

# Launch the proxy: listen params come from the ENV HOST/PORT above (argparse
# defaults read env vars), so `docker run -e PORT=9000` and compose's
# `environment:` actually take effect; override this CMD to add other flags.
CMD ["python", "qoder_proxy.py"]
