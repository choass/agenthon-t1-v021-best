FROM python:3.13-slim
LABEL qfbench2.interface_version="2.0"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 T1_CONTAINER_RUNTIME=1
ENV HOME=/tmp XDG_CACHE_HOME=/tmp/.cache NUMBA_CACHE_DIR=/tmp/.numba MPLCONFIGDIR=/tmp/.mpl OPENBLAS_NUM_THREADS=4 OMP_NUM_THREADS=4
WORKDIR /agent
RUN apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates libseccomp2 \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml /agent/pyproject.toml
COPY requirements-runtime.txt /agent/requirements-runtime.txt
COPY requirements.lock.txt /agent/requirements.lock.txt
COPY t1_agent /agent/t1_agent
RUN pip install --no-cache-dir -r /agent/requirements.lock.txt \
    && pip install --no-cache-dir --no-deps . \
    && rm -rf /root/.cache/pip
ENTRYPOINT ["python", "-m", "t1_agent"]
CMD ["solve", "--task-dir", "/input", "--out", "/app/output"]
