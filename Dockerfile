FROM python:3.11-slim

ARG DEBIAN_FRONTEND=noninteractive
ARG TARGETARCH

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SOLC_SELECT_PATH=/home/shared/.solc-select \
    FOUNDRY_DIR=/home/shared/.foundry \
    HOME=/tmp \
    PATH="/home/shared/.foundry/bin:/usr/local/bin:${PATH}"

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
    && ln -s /usr/bin/fdfind /usr/local/bin/fd \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /home/shared && chmod 777 /home/shared

RUN curl -L https://foundry.paradigm.xyz | bash && foundryup

RUN curl --proto '=https' --tlsv1.2 -LsSf https://github.com/cyfrin/aderyn/releases/latest/download/aderyn-installer.sh | bash \
    && mv /tmp/.cargo/bin/aderyn /usr/local/bin/aderyn \
    && mv /tmp/.cargo/bin/aderyn-update /usr/local/bin/aderyn-update

RUN pip install --no-cache-dir \
    slither-analyzer \
    solc-select \
    pyevmasm \
    web3 \
    eth-abi \
    crytic-compile

RUN pip install --no-cache-dir "mythril>=0.23.25"

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
RUN pip install --no-cache-dir .

VOLUME ["/project"]
ENV PROJECT_PATH=/project
ENV PYTHONPATH=/app/src

ENTRYPOINT ["python", "-m", "ignite_agent.agent"]