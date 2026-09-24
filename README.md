# Agenthon T1 v0.3.4 — current request contract

Participant image from the immutable v0.3.4-contract20260923 solver snapshot. Local public evaluation with GLM-5.2 (non-thinking) passed 43/87 tasks (49.43%), after repairing the **external scoring adapter**; solver outputs and source were unchanged. This is not an official House-model score.

Every task has at most 25 model transport attempts, including retries, at most 4000 output tokens per attempt, and its card deadline. Cumulative tokens are diagnostics only. Competition calls use injected MODEL_ENDPOINT, MODEL_NAME and MODEL_TOKEN, with the audited proxy environment preserved, `/v1/chat/completions`, bearer authentication and thinking disabled.

The image accepts `solve --task-dir /input --out /app/output`. Inputs are read in place; scratch goes under /tmp; only deliverables go into /app/output. Dependencies are installed at build time. No model weights, credentials, task datasets, reference answers or evaluation artifacts are packaged.

The original v0.2.1 image and default source branch remain available. This branch publishes `v034-contract` and `v034-contract-sha-<commit>` tags to the existing public GHCR package. Submissions pin the resulting immutable digest, never a moving tag.

CI checks the real image under non-root/read-only/noexec 64 MiB tmpfs, process and file-descriptor limits, and a 64 MiB per-file limit. The synthetic test uses 2 CPUs/4 GiB on GitHub's runner; this is a startup/protocol/resource-compatibility smoke test, not a reproduction of official 16 CPU/128 GiB task performance or accuracy.
