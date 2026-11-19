FROM python:3.12-slim
WORKDIR /app
COPY *.py barrier_deploy.json requirements-genes.txt ./
COPY genes ./genes
COPY data ./data
COPY snapshots ./snapshots
RUN useradd -m mos && mkdir -p /data && chown mos /data
USER mos
ENV MEMEOS_DB=/data/memeos-node.sqlite
# required secrets: MOS_NODE_ID, MOS_NODE_TOKEN (optional MOS_HUB_URL)
CMD ["python3", "node.py", "--loop", "60"]
