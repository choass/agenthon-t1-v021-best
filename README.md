# Agenthon T1 — v0.2.1 official submission adaptation

Team uranus (516). This repository contains the independently implemented T1 harness based on our frozen v0.2.1 source, adapted to the current House request contract.

The historical v0.2.1 local GLM-5.2 evaluation scored 52/87 (59.77%) under the former token budget. This is **not** a score for this adapted image or the official House model. Official evaluation results are pending.

## Runtime

Entrypoint: `python -m t1_agent solve --task-dir /input --out /app/output`.

The runner injects `MODEL_ENDPOINT`, `MODEL_NAME`, and `MODEL_TOKEN`. Model requests use the OpenAI-compatible chat completions protocol. Each task permits at most 25 HTTP attempts, counting retries conservatively, with `max_tokens` capped at 4000 per attempt. Cumulative input/output tokens are diagnostics, not admission limits. Thinking is disabled. Each card's agent timeout bounds solving and local checks.

Tools: checkpoint, bash, verify, finish. The workflow builds and executes a solution, then starts a fresh review context. Contracts and compact progress survive context trimming. Local verification checks deliverables and hashes; it is not a correctness label from the official grader.

The container reads the participant input tree in place (no dataset copy into the 64 MiB tmpfs). Paths specified by task Docker COPY instructions are translated to participant-visible input paths. Scratch and logs live under `/tmp`; only requested deliverables go to `/app/output`. No official answers, grading checks, task datasets, model weights, or credentials are included. Dependencies are installed during build. There is no runtime dependency download.

## Build and verification

`docker build -t agenthon-t1:ci .`

The manually triggered GitHub Actions workflow builds linux/amd64, tests request accounting, and performs an offline mock-model smoke test under nonroot/read-only/noexec-tmpfs restrictions before publishing the **tested image** to GHCR. The smoke verifies the HTTP protocol, native tool history, execution, review, and output aliases; it does not measure financial correctness.

## License

MIT. The harness is an independent implementation; it uses external Python packages under their respective licenses.
