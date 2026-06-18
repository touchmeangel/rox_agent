FROM ubuntu:22.04

ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
        python3 python3-pip \
        curl git build-essential \
        python3-dev libssl-dev \
    && rm -rf /var/lib/apt/lists/*

RUN ln -sf /usr/bin/python3 /usr/bin/python

ENV FOUNDRY_DIR=/root/.foundry
ENV PATH="${FOUNDRY_DIR}/bin:${PATH}"

RUN curl -L https://foundry.paradigm.xyz | bash && foundryup

RUN pip3 install --no-cache-dir slither-analyzer solc-select

WORKDIR /app

COPY . .

RUN pip3 install --no-cache-dir .

VOLUME ["/project"]
ENV PROJECT_PATH=/project

ENTRYPOINT ["ignite_agent"]