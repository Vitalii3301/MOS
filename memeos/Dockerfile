FROM python:3.12-slim
WORKDIR /app
# exactly the files of the live code bundle (code_bundle.FILES); `code_root` of /app equals the live cells' (op "rule")
COPY memeos.py sandbox.py codememes.py agent.py node.py tasks.py modelgen.py barrier.py barrier_deploy.json science.py net.py immune.py emergence.py compile.py drive.py neuro.py external_gene.py formula.py restrict.py forecast.py skillgene.py genememe.py forage.py lifecycle.py breed.py code_bundle.py skillcheck.py skillimmune.py reader.py requirements-genes.txt ./
COPY genes/cards/ ./genes/cards/
RUN mkdir -p /data
ENV MEMEOS_DB=/data/memeos-node.sqlite PYTHONUNBUFFERED=1
# Runs as root on purpose: the sandbox's empty-filesystem wall needs chroot (root) or an unprivileged user namespace,
# which Docker's default seccomp profile blocks. Each code meme still runs as uid 65534 inside an empty chroot.
# required secrets: MOS_NODE_ID, MOS_NODE_TOKEN (optional MOS_HUB_URL)
CMD ["python3", "node.py", "--loop", "60"]
