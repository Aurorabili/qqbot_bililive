FROM python:3.11-slim

# ===============================
# apt 阿里源（Debian 12 slim 专用）
# ===============================
RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources && \
    sed -i 's|security.debian.org|mirrors.aliyun.com/debian-security|g' /etc/apt/sources.list.d/debian.sources

# ===============================
# pip 阿里源
# ===============================
RUN pip config set global.index-url https://mirrors.aliyun.com/pypi/simple && \
    pip config set global.trusted-host mirrors.aliyun.com

ENV TZ=Asia/Shanghai
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

WORKDIR /app

# ===============================
# 系统依赖
# ===============================
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl \
        sqlite3 \
    && rm -rf /var/lib/apt/lists/*

# ===============================
# Python 依赖
# ===============================
COPY pyproject.toml ./

RUN pip install --no-cache-dir \
    nonebot2[fastapi] \
    nonebot-adapter-onebot \
    nonebot-plugin-orm[sqlite] \
    nb-cli

# ===============================
# 项目代码
# ===============================
COPY . /app

#EXPOSE 8080

CMD ["nb", "run"]