FROM python:3.11-slim

ARG DEBIAN_FRONTEND=noninteractive
ARG TARGETARCH

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SOLC_SELECT_PATH=/home/shared/.solc-select \
    FOUNDRY_DIR=/home/shared/.foundry \
    HOME=/home/shared \
    PATH="/home/shared/.foundry/bin:/usr/local/bin:${PATH}" \
    MPLCONFIGDIR=/home/shared/matplotlib_cache \
    PLAYWRIGHT_BROWSERS_PATH=/home/shared/.cache/ms-playwright \
    UV_CACHE_DIR=/home/shared/.cache/uv

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libssl-dev pkg-config \
    git \
    curl wget ca-certificates \
    jq \
    ripgrep \
    fd-find \
    xxd file binutils \
    cloc \
    tree \
    bc \
    unzip \
    cmake \
    libgmp-dev \
    poppler-utils \
    && ln -s /usr/bin/fdfind /usr/local/bin/fd \
    && rm -rf /var/lib/apt/lists/*

RUN if [ "$TARGETARCH" = "arm64" ]; then \
    apt-get update && apt-get install -y --no-install-recommends libc6-amd64-cross \
    && rm -rf /var/lib/apt/lists/* \
    && ln -s /usr/x86_64-linux-gnu/lib64/ /lib64; \
    fi

ENV LD_LIBRARY_PATH="/lib64:/usr/x86_64-linux-gnu/lib"

RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /tmp/matplotlib_cache && chmod -R 777 /tmp/matplotlib_cache
RUN mkdir -p /home/shared && chmod 777 /home/shared

RUN curl -L https://foundry.paradigm.xyz | bash \
    && foundryup --install v1.7.1

RUN curl --proto '=https' --tlsv1.2 -LsSf https://github.com/cyfrin/aderyn/releases/latest/download/aderyn-installer.sh | bash \
    && mv /home/shared/.cargo/bin/aderyn /usr/local/bin/aderyn \
    && mv /home/shared/.cargo/bin/aderyn-update /usr/local/bin/aderyn-update

RUN pip install --no-cache-dir \
    slither-analyzer \
    solc-select \
    pyevmasm \
    web3 \
    eth-abi \
    crytic-compile \
    git+https://github.com/touchmeangel/evm_decode_constructor

RUN npm install -g \
    solhint

RUN solc-select install 0.8.28 && \
    solc-select install 0.8.26 && \
    solc-select install 0.8.24 && \
    solc-select install 0.8.20 && \
    solc-select install 0.8.19 && \
    solc-select install 0.8.17 && \
    solc-select install 0.8.15 && \
    solc-select install 0.8.13 && \
    solc-select install 0.8.0  && \
    solc-select install 0.7.6  && \
    solc-select install 0.6.12 && \
    solc-select use 0.8.28

RUN chmod -R 777 /home/shared

WORKDIR /app
COPY . .

RUN pip install --no-cache-dir uv \
    && uv sync --frozen --no-dev --all-packages

RUN uv run playwright install --with-deps chromium && chmod -R 777 /home/shared

VOLUME ["/project"]

ENTRYPOINT ["uv", "run", "--no-sync", "python", "-m", "ignite_worker.agent"]