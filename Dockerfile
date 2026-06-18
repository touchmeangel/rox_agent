FROM python:3.11-slim

ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
        curl git build-essential libssl-dev \
    && rm -rf /var/lib/apt/lists/*

ENV FOUNDRY_DIR=/root/.foundry
ENV PATH="${FOUNDRY_DIR}/bin:${PATH}"
RUN curl -L https://foundry.paradigm.xyz | bash && foundryup

RUN pip install --no-cache-dir slither-analyzer solc-select

WORKDIR /app

COPY . .
RUN pip install --no-cache-dir .

VOLUME ["/project"]
ENV PROJECT_PATH=/project

ENV PYTHONPATH=/app/src

ENTRYPOINT ["python", "-m", "ignite_agent.agent"]