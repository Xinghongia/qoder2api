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
COPY qoder_proxy.py qoder_accounts.py qoder_catalog.py qoder_fingerprint.py \
     qoder_net.py qoder_scheduler.py qoder_settings.py qoder_sign.py \
     qoder_tasks.py dashboard.html baseprompt.json ./

# 官方模型目录快照（运行时优先读取；缺失会回退 qoder_catalog.py 内嵌冻结副本）
COPY qoder_catalog_intl.json qoder_catalog_cn.json ./

# Create data directories
RUN mkdir -p /app/accounts /app/usage

# Volume persistence for credentials and usage logs
VOLUME ["/app/accounts", "/app/usage"]

EXPOSE 8790

# Launch proxy in host 0.0.0.0 mode
CMD ["python", "qoder_proxy.py", "--host", "0.0.0.0", "--port", "8790"]
