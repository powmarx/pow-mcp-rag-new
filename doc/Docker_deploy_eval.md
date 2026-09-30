# pow-mcp-rag-new — Docker deployment investigation

**Scope:** the three reported symptoms, their root causes, how they interact, and the
structural pattern behind them.
**Repo analysed:** `/workspace/pow-mcp-rag-new-main` (141 files, `pyproject.toml` version `1.2.1`).
**Nothing in the repository was modified.** All new files live under `/workspace/analysis/`.

---

## 0. TL;DR

The three symptoms are not three bugs. They are three views of **one design decision**:

> The embedding model is a *runtime* setting (`embedding.model` in `config.yaml`, which lives
> inside the data volume), but the model *weights* are a *build-time* artifact (baked into the
> image, with `HF_HUB_OFFLINE=1` enforced at runtime). Nothing connects the two.

From that single decision:

* **Issue 2 (newer models fail the build)** — the model is a hard-coded `ENV` (not `ARG`), the
  build-time warm-up cannot pass `trust_remote_code=True`, and the runtime is pinned offline, so
  every model that is not `BAAI/bge-small-en-v1.5` fails at build time, at container start, or
  (worst case) silently at query time.
* **Issue 3 (2.18 GB image)** — because the model must be inside the image, and because
  "any HF model" implies torch + transformers + scipy + scikit-learn, the image can never be
  small. The multi-stage build that is supposed to fix this copies the entire `site-packages`
  and saves ~0 bytes.
* **Issue 1 (`.bat` vs `.sh` divergence)** — the two setup scripts are independent transcriptions
  of an undocumented contract (image name, volume name, flags, failure semantics). Nothing
  enforces the contract, and the Docker path has **zero** CI coverage, so it drifted. The
  divergence is not cosmetic: the two scripts create **two different deployments**
  (`rag-mcp` / `rag-mcp-data` vs `rag-mcp-new-pip` / `rag-mcp-new-pip-data`), and the re-index
  command both scripts print at the end works for only one of them.

The deeper tension is documented in §7. Short version: **development concerns (swap models
freely, iterate on retrieval quality) and production concerns (immutable, offline, small,
reproducible) are both present in this repo, and the conflict was resolved by freezing the
development defaults into the production artifact.** Every attempt to vary the model hits a wall,
and every attempt to shrink the image hits the same wall from the other side.

A prioritized fix list is in §9.

---

## 1. Method, and the limitations of this environment

No Docker daemon and no access to `huggingface.co` / `download.pytorch.org` was available in the
analysis sandbox, so "run `docker build` and see" was not possible. Instead, every claim below is
backed by one of:

| Technique | What it proves | Artifact |
|---|---|---|
| Static audit of the repo's Docker surface | parity, naming, compose validity, template drift, missing guards | `tools/audit_docker_setup.py` → `audit_output.txt` (37 findings) |
| Offline reproduction of the model failures with fabricated local model dirs | the exact exceptions `docker build` / the server would print | `tools/repro_model_failures.py` → `repro_output.txt` |
| Real install of `requirements.txt` into a clean venv (PyPI reachable) | actual dependency resolution + on-disk footprint | `tools/measure_footprint.py` → `footprint_output.txt` |
| Uninstall-and-run probes on hard-linked clones of that venv | which packages are *really* needed by the hot path | `tools/probe_trimmable_deps.py` → `trim_probe_output.txt` |
| Real ChromaDB 1.5.9 writes at 384 and 1024 dims | per-chunk volume cost and where the bytes go | §5.3 |
| A locally generated random-weight ST model + cross-encoder | end-to-end hot path with no network | `tools/make_tiny_model.py` |
| `docker` replaced by a logging stub | the generated command lines of the replacement setup script | §9, `workarounds/setup-docker-unified.sh` |

Two consequences:

* Numbers for `site-packages` were measured with Python 3.12 and the **PyPI (CUDA) torch wheel**;
  the image uses Python 3.13 and the **CPU wheel**. Where that matters, the CPU figure is derived
  by subtracting the CUDA-only shared objects inside the wheel (measured: 518 MB) and the GPU-only
  packages (measured: 3 603 MB). The derived total (~1.9–2.1 GB) matches the reported 2.18 GB.
* Anything that needs a daemon (layer sizes, `COPY --from` symlink behaviour in the HF cache) is
  flagged **[verify with daemon]** and has a ready-made command.

### 1.1 A bug that blocked part of the analysis, and the workaround

`docker-compose.yml` (service `server`) mounts:

```yaml
- -rag-mcp-new-pip-data:/app/data     # leading hyphen
```

That name is (a) not declared under the top-level `volumes:` key and (b) not a legal Docker
volume name (`[a-zA-Z0-9][a-zA-Z0-9_.-]*`). `docker compose config` therefore fails for the
**whole file**, which also blocks `docker compose run --rm indexer` — the re-index command that
`setup-docker.bat`, `setup-docker.sh` and `doc/DOCKER_GUIDE.md` all tell users to run.

Verified statically (`audit_output.txt`, finding **C1**):

```
    indexer      rag-mcp-new-pip-data         ok
    server       -rag-mcp-new-pip-data        UNDECLARED ILLEGAL-DOCKER-NAME
    server-http  rag-mcp-new-pip-data         ok
```

An override file (`-f docker-compose.yml -f extra.yml`) cannot reliably remove a bad entry, so the
workaround is a corrected stand-alone compose file that lives outside the repo and is fully
parameterised: `workarounds/docker-compose.fixed.yml`. It also fixes the second-order problem
(hard-coded names) by driving the volume names through `name:` interpolation, so `.bat` users and
`.sh` users can address the same deployment.

---

## 2. Issue 1 — `setup-docker.bat` and `setup-docker.sh` do not behave the same

### 2.1 What actually differs

| Concern | `setup-docker.bat` | `setup-docker.sh` | Consequence |
|---|---|---|---|
| Image name | `rag-mcp` (default, `--image` overridable) | `rag-mcp-new-pip:latest` (hard-coded) | **Different image** per OS |
| Data volume | `<image>-data` → `rag-mcp-data` | `rag-mcp-new-pip-data` | **Different index/config state** per OS |
| `--src` / `SRC` env | yes | no | no way to point at another projects root |
| `--repo` | yes | no | must `cd` into the checkout |
| `--image` | yes | no | cannot rename |
| `--server-name` | yes (forwarded to `setup_mcp_config.py`) | no | custom MCP key impossible on POSIX |
| `PROJECTS_ROOT` existence check | yes (`exit /b 1`) | **no** | mounts a non-existent path; discovery finds nothing |
| Failure semantics | `errorlevel` checks, `pause`, continue-with-WARNING | `set -e` (abort) + two `|| echo WARNING` steps | **opposite** behaviour for the same step |
| Discovery `--list` step | failure → WARNING, continues | failure → whole script aborts (`set -e`) | |
| `mcp.json` ownership | N/A on Windows | written by a **root** container into `$HOME/.kiro` | root-owned file on Linux/macOS |
| Interactive tail | `pause` | none | `.bat` cannot run unattended; `.sh` can |
| Exec bit | N/A | files are `0644` in the tree | `./setup-docker.sh` → *Permission denied* |
| Final "Config:" line | `<repo>\config\config.yaml` | `<repo>/config/config.yaml` | **both wrong**: the live config is `/app/data/config.yaml` *inside the volume* |
| Printed re-index command | `docker compose run --rm indexer` | same | works only for the `.sh` naming; for `.bat` users it indexes a different volume (and rebuilds the image) |

Reproduce: `python3 tools/audit_docker_setup.py /workspace/pow-mcp-rag-new-main` (findings `P1`–`P8`).

### 2.2 Root causes

1. **No single source of truth for the deployment identity.** The image name, volume name and MCP
   key appear as literals in eight places, with **four different values**:

   | Location | Value |
   |---|---|
   | `pyproject.toml` `[project].name` | `pow-rag-mcp` |
   | `config/server_info.json` → MCP key | `rag-mcp` |
   | `setup-docker.bat` default image | `rag-mcp` |
   | `setup-docker.sh`, `docker-compose.yml`, `start-http.ps1`, `setup_mcp_config.py` defaults | `rag-mcp-new-pip` |
   | `setup_mcp_config.py --package` default | `rag-mcp-new-pip-mcp` |
   | `setup-pypi.bat --package` | `rag-mcp` |
   | `tests/server/test_setup_process.py` assertions | `project-rag` |

   Because those names address **state** (Docker volumes) and **artifacts** (images, PyPI
   distributions), a mismatch does not raise — it silently bifurcates. `setup-pypi.bat`'s
   `uv tool install rag-mcp` installs *somebody else's* PyPI project, which `README.md` even
   warns about ("unrelated to `rag-mcp` or `rag-mcp-server` packages") without fixing the script.

2. **Parity is asserted in prose.** `doc/ARCHITECTURE.md` line 92 says
   `setup-docker.sh  # Linux/macOS equivalent of setup-docker.bat`. Nothing tests that claim;
   `doc/DOCKER_GUIDE.md` only documents the `.bat`.

3. **Zero automated coverage of the Docker path.** `.github/workflows/release.yml` never builds the
   `Dockerfile` and never runs either setup script (finding `R3`); no test file mentions
   `Dockerfile`, `docker-compose.yml` or `setup-docker.*` (finding `X1`). The one test that looks
   like setup coverage *re-implements* the logic under test — `tests/server/test_setup_process.py`
   contains `_run_mcp_config_logic()` described as *"Extracted logic from setup_mcp_config.py for
   testability"* and asserts on the MCP key `project-rag`, which the real script has not produced
   for a long time (finding `X2`). The test is green and the product is wrong.

4. **Windows-first development.** Every feature added after the initial port landed in the `.bat`
   only (`--src`, `--repo`, `--image`, `--server-name`, path normalisation, existence checks). The
   same asymmetry exists repo-wide: `setup-pypi.bat`, `start-http.ps1`, `diagnose-chromadb.ps1`,
   `restart_wmi.bat` have no POSIX counterpart. The comment in `setup-docker.bat`
   (*"so a stray shift can never corrupt it (see: the E:\OneDrive incident)"*) shows where the
   debugging effort went.

5. **Both scripts lie about where the config is**, because `RAG_CONFIG_PATH=/app/data/config.yaml`
   was introduced later (good change) and the final summary text was never updated. Users then edit
   `<repo>/config/config.yaml` and nothing happens — a support burden that looks like a Docker bug.

### 2.3 Second-order effect worth calling out

`setup.sh` and `setup.bat` (the native, non-Docker path) both end with a "smoke test" that runs
`tests/test_mcp_connection.py`. That file was moved to `tests/server/test_mcp_connection.py` by the
test-suite reorganisation, so the verification step can never pass; in `setup.sh` its stderr is
redirected to `/dev/null` and the failure degrades to `[WARN] Some connection tests failed`
(finding `X3`). The pattern — *a check that cannot succeed, whose failure is downgraded to a
warning* — recurs in §3 and §4.

---

## 3. Issue 2 — newer embedding models fail during the Docker build

### 3.1 The chain, in the order a user hits it

```
Dockerfile:23  ENV EMBED_MODEL=BAAI/bge-small-en-v1.5        <-- ENV, not ARG
Dockerfile:38  RUN python -c "...SentenceTransformer('${EMBED_MODEL}')"
Dockerfile:48  ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
config.yaml    embedding.model: <whatever the user sets>     <-- the only value the code reads
```

**(a) `--build-arg` is silently ignored.** There is no `ARG` in the Dockerfile (finding `D1`), so
`docker build --build-arg EMBED_MODEL=nomic-ai/nomic-embed-text-v1.5 .` only emits
`WARNING: one or more build-args were not consumed` and produces an image with the old model.

**(b) `EMBED_MODEL` / `RERANK_MODEL` are dead variables at runtime.** No Python file in the repo
reads them (finding `D2`, verified by scanning every `*.py`). `docker run -e EMBED_MODEL=...`
changes nothing. The model actually used comes from `config.yaml` → `EmbeddingConfig.model`.
So the image's baked cache and the server's requested model are two independent values.

**(c) Editing the `ENV` breaks the build for most 2024+ models.** The warm-up is a one-liner that
cannot pass `trust_remote_code=True`, and `einops` is not installed. Reproduced offline
(`repro_output.txt`):

```
ValueError: The model <...> references the module class 'modeling_custom.CustomTransformer',
which is not part of Sentence Transformers. Importing it executes third-party code.
Please pass the argument `trust_remote_code=True` to allow custom code to be run.
```

This hits `nomic-ai/nomic-embed-text-v1*`, `Alibaba-NLP/gte-*-en-v1.5`, `jinaai/jina-embeddings-v2/v3`,
`dunzhang/stella*`, and anything else that ships its own modelling code. Note the aggravating
factor: **`requirements.txt` pins `sentence-transformers>=2.2.2` with no upper bound**, and
sentence-transformers **6.0** *tightened* this rule (`util/misc.py`: *".. versionchanged:: 6.0 —
Importing a module class outside the sentence_transformers.* namespace now requires
trust_remote_code=True whenever model_name_or_path is set"*). A clean resolve today yields
`sentence-transformers 6.0.0` + `transformers 5.15.1`, while `uv.lock` pins `5.6.1` / `5.14.1`
(finding `R1`). **The same commit builds differently on different days**, and a model that worked
last month can break the build this month with no repo change.

**(d) Models newer than the image's `transformers` fail with a different error.** Also reproduced:

```
ValueError: The checkpoint you are trying to load has model type `brand_new_embedding`
but Transformers does not recognize this architecture...
```

**(e) If you change only `config.yaml` (the supported-looking path), the container fails at
startup** because the runtime stage is pinned offline (finding `D4`). Reproduced:

```
OSError: We couldn't connect to 'https://huggingface.co' to load the files,
and couldn't find them in the cached files.
```

`doc/ARCHITECTURE.md` tells users to *"Change `embedding.model` in `config.yaml`, rebuild the Docker
image so the new model is baked into the image cache"* — but rebuilding bakes **the Dockerfile's
constant**, not the config's value. The documented procedure cannot work.

**(f) Worst case — it "works" and is wrong.** Suppose the model does load (cached, or built with
the ENV edited). Two silent failures remain:

* **Dimension mismatch.** `ChromaStore.get_or_create_collection()` stores only
  `{description, hnsw:space}` — never the model name or vector size (finding `S1`). With an
  existing 384-dim index and a 768/1024-dim model, ChromaDB raises
  `InvalidArgumentError: Collection expecting embedding with dimension of 384, got 1024`
  (reproduced) — but `tools/search.py` wraps `collection.query()` in `except Exception: continue`
  (finding `S2`), so **the MCP tool answers "no results found"** instead of reporting a mismatch.
* **Same-dimension swap.** `all-MiniLM-L6-v2` → `bge-small-en-v1.5` → `e5-small-v2` are all
  384-dim. No error anywhere, ever: the collection now mixes vectors from two different spaces and
  rankings are quietly garbage. `doc/ARCHITECTURE.md` warns about this in prose
  ("mixing embeddings from two models in one collection corrupts search results") — the code does
  not check it.
* **Wrong query prefix.** `EmbeddingConfig.query_instruction` defaults to the BGE string
  `"Represent this sentence for searching relevant passages: "`, and `config.template.yaml` never
  writes the key (finding `T2`), so the default applies to *every* model. E5 wants `query: ` /
  `passage: `; nomic wants `search_query: ` / `search_document: `; GTE/MiniLM want none. Also, in
  sentence-transformers ≥3 a model can ship `default_prompt_name`, which ST applies to **every**
  `encode()` call (`base/model.py::_validate_prompts`) — that prefix then stacks on top of
  rag-mcp's own, and it is applied to documents too, because the indexer calls plain `encode()`
  rather than `encode_document()`.

**(g) Mechanical side effects of a bigger model that nothing accounts for:**
`indexer.py --estimate` hard-codes `KB_PER_CHUNK = 10.0` (finding `S3`); `chunking.chunk_size`
(1000 chars) is tuned for a 512-token window and is not linked to the model's `max_seq_length`, so
an 8 k-token model gains nothing and a 256-token model (MiniLM) silently truncates.

### 3.2 Root cause, stated once

The model identity is **configuration**, its weights are **a build artifact**, its library
requirements are **unpinned**, and its vector space is **untracked**. Any one of those four would
be survivable; together they guarantee that "choose a different embedding model" fails in a
different place every time, and at least one of those places fails *silently*.

---

## 4. Issue 3 — the image is 2.18 GB

### 4.1 Where the bytes are (measured, then projected onto the CPU build)

Clean `pip install -r requirements.txt` (no CPU index) → **5 662 MB** of `site-packages`
(`footprint_output.txt`):

| category | MB | note |
|---|---:|---|
| gpu-only (`nvidia-*`, `triton`, `cuda*`) | 3 603 | never used — CPU inference only |
| `torch` | 1 142 | of which **518 MB** is `libtorch_cuda*.so` etc. |
| scipy + sklearn + numpy (+`.libs`) | 255 | pulled in by sentence-transformers |
| chroma "service" deps (kubernetes, grpc, otel, uvicorn…) | 144 | server-mode features of a library used in embedded mode |
| transformers + tokenizers + hf_hub + ST | 138 | |
| torch.compile deps (sympy, networkx, mpmath) | 96 | unused at inference |
| onnxruntime | 67 | ChromaDB's default embedding function — rag-mcp always passes its own vectors |
| chromadb + rust bindings | 66 | |
| app deps (pymupdf 67, openpyxl, pyyaml, rich, mcp, pydantic…) | 124 | |
| pip + setuptools + wheel + build | 16 | installer tooling |

Projection for the repo's Dockerfile (which *does* install CPU torch first — the one thing it gets
right): `5 662 − 3 603 − 518 ≈ **1 541 MB** site-packages`. Add `python:3.13-slim` (~130 MB), the
baked HF cache for `bge-small-en-v1.5` + `ms-marco-MiniLM-L-6-v2` (~220–450 MB depending on whether
both `.bin` and `.safetensors` land in the cache) and ~2 MB of app code → **~1.9–2.1 GB**, i.e. the
reported 2.18 GB is fully accounted for. There is no mystery 500 MB; the stack simply is this big.

### 4.2 What the Dockerfile believes vs what it does

The header says: *"Stage 1 … strips test/dev packages … Stage 2 … copies only the cleaned
site-packages + app code into a fresh slim image — no pip, no compilers, no test deps."*

* **"strips test/dev packages"** — `RUN pip uninstall -y pytest pytest-asyncio hypothesis` removes
  nothing: those live in `requirements-dev.txt`, which `.dockerignore` excludes and which is never
  installed (finding `D5`). A no-op.
* **"no pip"** — `COPY --from=builder /usr/local/lib/python3.13/site-packages` and
  `COPY --from=builder /usr/local/bin` copy pip, setuptools, wheel and every `__pycache__`
  verbatim (finding `D6`; 16 MB of installer tooling measured).
* **"no compilers"** — true, but vacuous: the builder never installed any (`apt-get` is never
  called), so the multi-stage split saves approximately **zero bytes**. It is a 2-stage build with
  1 stage of benefit.
* `PIP_NO_CACHE_DIR=1` is also redundant in a stage whose filesystem is discarded.

So the image is not 2.18 GB because of a mistake; it is 2.18 GB because **nobody has ever measured
it in CI**, and the optimisation that was believed to be in place is inert.

### 4.3 What can actually be removed — verified, not guessed

`tools/probe_trimmable_deps.py` uninstalls each candidate from a hard-linked clone and then runs the
real hot path (`EmbeddingGenerator.load()` → `encode()` → `ChromaStore.upsert_chunks()` →
`collection.query()`), so every verdict is empirical (`trim_probe_output.txt`):

| candidate | verdict | freed | evidence |
|---|---|---:|---|
| `pip` + `setuptools` + `wheel` | **removable** | 16 MB | |
| `onnxruntime` | **removable** | 67 MB | chromadb only needs it for its default embedding function |
| `kubernetes` | **removable** | 79 MB | chromadb server-mode only |
| `networkx` | **removable** | 16 MB | |
| `hf-xet` | **removable** | 13 MB | faster Hub transfer, not required |
| `uvicorn` + `starlette` | removable for stdio mode | 1 MB | needed for `--http` |
| `build` + `pyproject_hooks` | **removable** | <1 MB | |
| `pymupdf` | removable **if you drop PDF indexing** | 67 MB | feature trade-off |
| `openpyxl` | removable **if you drop .xlsx** | 2 MB | feature trade-off |
| `scipy` | required | — | `ModuleNotFoundError: No module named 'scipy'` from ST import |
| `scikit-learn` | required | — | same |
| `sympy` + `mpmath` | required | — | `import torch` fails |
| `grpcio` | required | — | chromadb imports it |
| `opentelemetry-*` | required | — | chromadb imports `opentelemetry.trace` |
| `torch/include`, `torch/test`, in-package `tests/`, `*.pyi` | removable | ~122 MB | |
| `torch/bin` | **NOT removable** | — | `RuntimeError: Unable to find torch_shm_manager` at `import torch` (discovered by testing, not by reading) |
| `__pycache__` (283 MB non-GPU) | **removable but don't** | 283 MB | cold-start import time measured **8.7 s → 19.7 s**; with `docker run --rm` per MCP session you pay it on *every* launch |

Applying the safe set and re-running the hot path: **328 MB freed, hot path still green**
(`embed + rerank + chroma upsert + query` all OK).

### 4.4 The 3.6 GB cliff next door

The CPU-index trick exists **only** in the Dockerfile (finding `D7`). Everywhere else the same
dependency set is installed the naive way:

| path | torch source | outcome |
|---|---|---|
| `Dockerfile` | `pip install --index-url .../whl/cpu torch` then `-r requirements.txt` | CPU wheel ✔ |
| `setup.sh` / `setup.bat` | `pip install -r requirements.txt` | CUDA wheel + `nvidia-*` + `triton` → **+3.6 GB** |
| `pyproject.toml` | `torch` as an *optional* extra with a comment | ineffective: `sentence-transformers` hard-depends on torch, so plain `pip install pow-rag-mcp` pulls the CUDA build anyway |
| CI (`release.yml`) | `pip install -e ".[dev]"` | CUDA wheel |
| `uvx --from pow-rag-mcp` | uv resolution | CUDA wheel in the uv cache (this is also why `doc/TROUBLESHOOTING.md` has a whole section about a slow/racey `uvx` install of a *"~110-package dependency tree"*) |

Measured directly in this sandbox: installing `requirements.txt` without the CPU index produced
`nvidia/` 2 855 MB + `triton/` 723 MB + `cuda*` 25 MB of packages that this project can never use.
The "make torch an extra" strategy in `pyproject.toml` cannot work while
`sentence-transformers` is a hard dependency; only an index/environment-marker level pin can.

### 4.5 Data-volume growth (the *other* "too big")

Measured with ChromaDB 1.5.9, 3 000 chunks of ~1 KB text:

| vector dim | total | per chunk | sqlite | HNSW segment |
|---|---:|---:|---:|---:|
| 384 | 45.3 MB | **14.7 KB** | 39 MB | 5 MB |
| 1024 | 54.3 MB | **17.7 KB** | 40 MB | 13 MB |

`dbstat` breakdown at 384 dims shows where it goes:

| table | MB | what it is |
|---|---:|---|
| `embedding_metadata_string_value` | 14.6 | the chunk text (stored as metadata) |
| `embedding_fulltext_search_data` | 12.2 | FTS5 index of the same text |
| `embedding_metadata` | 4.1 | |
| `embedding_fulltext_search_content` | 3.1 | **another copy** of the text |
| `embeddings` | 0.3 | |

So at 384 dims the index is dominated by **text stored up to three times**, not by vectors. Two
consequences for anyone trying to shrink the volume: (1) *index less text* beats *use a smaller
model*; (2) the repo's `--estimate` constant (10 KB/chunk) under-reports by 1.5–1.8× and does not
react to the model's dimension at all.

Chunking levers, measured on 60 436 characters of this repo's own text:

| `chunk_size` / `chunk_overlap` | chunks | stored text vs source |
|---|---:|---:|
| 1000 / 200 (default) | 69 | 1.15× |
| 1000 / 100 | 69 | 1.03× |
| 2000 / 200 | 36 (**−48 %**) | 1.07× |

Fewer, larger chunks is the biggest lever — but `chunk_size: 2000` characters exceeds
`bge-small`'s 512-token window, so the tail of each chunk would not be embedded at all. This is the
one place where **model choice and volume size are genuinely coupled**: an 8 k-token model
(`gte-*-en-v1.5`, `nomic-embed-text-v1.5`) makes large chunks legitimate, which halves the number
of rows, the HNSW graph and the FTS overhead. The repo has no link between `max_seq_length` and
`chunk_size`.

Minor but real: `VOLUME ["/app/data", "/projects"]` in the Dockerfile means any `docker run`
without `-v` for those paths silently gets **anonymous** volumes; for long-lived containers
(`server-http`, started with `-d`) they accumulate until `docker volume prune`.

---

## 5. How the three issues are connected

```
                     model weights baked into the image
                     (ENV constant + HF_HUB_OFFLINE=1)
                              │
          ┌───────────────────┼────────────────────────┐
          │                   │                        │
  "new model breaks     image must contain        changing the model
   the build"           torch + a model            means rebuilding
   (Issue 2)            (Issue 3)                  → per-OS setup scripts
          │                   │                     each own a build
          │                   │                        (Issue 1)
          ▼                   ▼                        ▼
   trust_remote_code    1.54 GB of deps +        two image names,
   unpinned ST/tf       0.25-0.45 GB models      two volumes, one
   offline runtime      multi-stage that          printed re-index
   no dim stamp         saves nothing             command that fits
          │                   │                   only one of them
          └────────► silent failure ◄─────────────┘
             "search returns nothing" / wrong rankings
               (except Exception: continue + no model stamp)
```

Concrete couplings:

1. **Issue 1 × Issue 2:** a user who follows the `.bat` default (`rag-mcp-data`) and then uses the
   documented `docker compose run --rm indexer` re-indexes `rag-mcp-new-pip-data`. The server sees
   an index built by a *different* run of the indexer — possibly with a different model — which
   presents exactly like the Issue-2 symptom ("searches return nothing").
2. **Issue 2 × Issue 3:** the only reason the model has to be in the image is the offline runtime.
   Moving it to a cache volume removes 0.22–0.45 GB *and* makes model switching a no-rebuild
   operation. One change fixes the small half of both problems.
3. **Issue 3 × Issue 1:** because each setup script owns its own `docker build`, any slimming work
   has to be done twice, and any `--build-arg` contract has to be wired twice. Today neither script
   passes a single build argument.
4. **All three × diagnosability:** `except Exception: continue` (search), `|| echo WARNING` (setup
   scripts), `2>/dev/null` (smoke test), the silent `--project` no-op, and the ignored
   `--build-arg` mean that *every* one of these failures is reported as "nothing happened".

---

## 6. Full finding list

`audit_output.txt` contains all 37 machine-checked findings (23 HIGH / 10 MED / 4 LOW) with IDs.
Highest-value subset, in fix order:

| ID | Severity | Finding |
|---|---|---|
| C1 | HIGH | `docker-compose.yml` `server` service: illegal, undeclared volume `-rag-mcp-new-pip-data` → `docker compose config` fails for the whole file |
| D1/D2 | HIGH | model is `ENV`, not `ARG`; no code reads `EMBED_MODEL` → `--build-arg` and `-e` both silently ineffective |
| D3 | HIGH | build-time warm-up cannot pass `trust_remote_code` (and `einops` is missing) |
| D4 | HIGH | runtime pinned `HF_HUB_OFFLINE=1` → any non-baked model is unreachable |
| S1/S2 | HIGH | no model/dimension stamp on collections + `except Exception: continue` in search → silent wrong/empty results |
| T2 | HIGH | BGE query prefix applied to every model by default |
| T1 | HIGH | `config/` and `src/rag_mcp/data/` copies of `config.template.yaml`, `server_info.json`, `detection_rules.json` are all out of sync, and **both ship in the image** |
| N1/N2 | HIGH | 2 image names, 4 distribution/MCP names; `setup-pypi.bat` installs the wrong PyPI project |
| P1/P2/P8 | HIGH | `.bat`/`.sh` build different deployments; `.sh` supports no flags; printed re-index command is wrong for `.bat` users |
| R1/R2 | HIGH/MED | unpinned `sentence-transformers` (6.0.0 today vs 5.6.1 in `uv.lock`); `uv.lock` exists but the Dockerfile ignores it and the lock already contradicts `pyproject.toml` (`mcp 2.0.0` vs `mcp<2.0.0`) |
| R3/X1/X2 | HIGH | no CI build of the image, no test of the Docker surface, and the one "setup" test re-implements the code it claims to test |
| D6/D7 | MED | multi-stage copies everything (incl. pip); CPU-torch knowledge exists only in the Dockerfile |
| D9 | MED | default `CMD` lacks `--no-reindex`, contradicting every doc and generated `mcp.json` |
| P5/P6 | MED | `set -e` vs `errorlevel` semantics; `.sh` writes root-owned `mcp.json` |
| X3 | MED | `setup.sh`/`setup.bat` smoke test points at a test file that no longer exists |
| S3 | LOW | `--estimate` hard-codes 10 KB/chunk (measured 14.7–17.7) |
| D8 | LOW | container runs as root |

### 6.1 One more defect found while tracing the PDF path

`server.py::_background_reindex()` builds `FileReader()` **without** `pdf_cache_dir`, while
`indexer.py` passes `Path(config.storage.path) / "pdf_cache"`. With no cache dir, `FileReader`
falls through to *"convert alongside the source"* (`pdf_converter.convert(filepath)` →
`filepath.with_suffix(".md")`), i.e. it tries to write into the **read-only** `/projects` mount.
`doc/ARCHITECTURE.md` states the opposite: *"Conversion always targets a writable cache so
read-only source mounts are never modified."* It is true for the indexer, false for the server's
auto-reindex — two copies of the same logic, one of them wrong.

---

## 7. The pattern: development concerns frozen into production artifacts

Eight recurring patterns, each of which shows up in at least two of the three reported issues.

**P1 — Two copies of everything, synchronised by hand.**
`indexer.py` (571 lines) ↔ `src/rag_mcp/_indexer.py::_run_inline()` (515 lines);
`server.py` (383) ↔ `_server.py::_run_inline()` (200);
`config/*.yaml|json` ↔ `src/rag_mcp/data/*` (synced by a script a human must remember to run —
and all three pairs are currently out of sync);
`setup-docker.bat` ↔ `setup-docker.sh`; `requirements.txt` ↔ `pyproject.toml` ↔ `uv.lock`.
Each pair is "the dev one" and "the shipped one". Drift is the default state, and the drift is
always discovered in production, because that is the copy nobody runs locally.

**P2 — Invariants live in prose, not in assertions.**
"always pass `--no-reindex` in Docker", "changing the model requires a reindex", "setup-docker.sh
is the equivalent of the .bat", "PDF conversion always uses a writable cache", "the multi-stage
build contains no pip". All are comments or docs. None is checked at build time, start-up time or
in CI. Three of the five are currently false.

**P3 — Build-time freezing of a runtime-configurable value.**
The model name is configuration; the weights are an image layer; the runtime is offline. The only
self-consistent configuration is the default one. Anything a developer wants to *vary* (model,
reranker, library versions) has been compiled into the artifact a user is supposed to *deploy*.

**P4 — Failure suppression as a habit.**
`except Exception: continue` (search loses the dimension-mismatch error), `|| echo WARNING`
(setup scripts continue into a broken state), `2>/dev/null` (smoke test), silent `--project`
no-op, ignored `--build-arg`, `except (ValueError, Exception): pass` in `ChromaStore`. Convenient
while iterating; in production it converts *every* distinct failure into the single symptom
"no results".

**P5 — Platform asymmetry.** Windows gets the features; POSIX gets a transcription. Over time the
transcription becomes a different program.

**P6 — Optimisation theatre.** A multi-stage build that copies everything; a "strip test deps" step
that strips nothing; a `torch` extra that does not prevent the CUDA download; `PIP_NO_CACHE_DIR` in
a discarded stage. The intent is real and the structure is there, but nothing measures the result,
so the structure decays into decoration. (Corollary: the fix for Issue 3 is not a clever trick, it
is a number in CI.)

**P7 — Names as the addressing scheme for state.** Volumes, images and PyPI distributions are all
addressed by string literals duplicated across eight files. A typo does not fail — it creates a
second, empty universe (`-rag-mcp-new-pip-data`, `rag-mcp` vs `rag-mcp-new-pip`, `rag-mcp` vs
`pow-rag-mcp`).

**P8 — The tested path and the shipped path are different paths.** CI tests the wheel; users are
pointed at Docker first. The wheel's own verification (`rag-mcp config`, stderr must be empty) is a
smoke test of the *config seeding*, not of retrieval. Nothing in CI ever embeds a sentence.

### What the tension really is

Development wants: any HF model, editable code at `/app/server.py`, scripts that double as
documentation, one command that goes from clone to working index.
Production wants: a small immutable image, no network at runtime, reproducible builds, the same
behaviour on every OS, and loud failure.

The repo currently resolves the conflict *in favour of development defaults*, then ships those
defaults as production constraints:

| dev affordance | how it became a production constraint |
|---|---|
| "just edit `config.yaml` to change the model" | weights are baked + offline → config change = broken container |
| "models are cached in the image so startup is fast and offline" | model choice now requires a rebuild, and the image is 2.18 GB |
| "unpinned deps so we always get fixes" | two builds of one commit differ; ST 6.0 changed a load-time rule |
| "setup scripts are the docs" | two OS dialects of the same undocumented contract, both drifting |
| "keep going on errors so setup never hard-fails" | silent half-configured deployments |
| "root in the container keeps mounts simple" | root-owned `mcp.json` and volume files on Linux |

None of these is a bad engineering instinct. The missing piece is the boundary: nothing in the repo
states *"this is the deployment contract"* and nothing verifies it. The contract is re-derived, by
hand, in every script, doc and test — which is exactly the set of places where the three reported
symptoms live.

---

## 8. Deliverables produced by this investigation

```
/workspace/INSTALLATION_GUIDE.md            <- unified, model-agnostic install guide (primary ask)
/workspace/analysis/
  FINDINGS.md                               <- this document
  audit_output.txt                          <- 37 machine-checked findings
  repro_output.txt                          <- the 4 model failure modes, reproduced offline
  footprint_output.txt                      <- measured dependency footprint
  trim_probe_output.txt                     <- which packages are really removable
  tools/
    audit_docker_setup.py                   <- static audit (parity, compose, Dockerfile, drift)
    repro_model_failures.py                 <- offline reproduction harness
    measure_footprint.py                    <- footprint classifier (works inside the image)
    probe_trimmable_deps.py                 <- uninstall-and-run prober
    make_tiny_model.py                      <- offline ST model + cross-encoder generator
  workarounds/
    Dockerfile.model-agnostic               <- ARG-driven model, trust_remote_code, verified trims
    docker-compose.fixed.yml                <- valid + parameterised compose
    setup-docker-unified.sh                 <- one setup path, .bat-equivalent flags
    warm_model.py                           <- validate/cache a model, emit a model card
    set_model_in_config.py                  <- pin model + matching query prefix into the volume
    check_index_model.py                    <- the missing index↔model consistency guard
```

Everything under `workarounds/` runs against the **unmodified** repo (`docker build -f …`,
`docker compose -f …`, `-v …:/tools:ro`).

---

## 9. Recommendations, in the order I would do them

### Tier 0 — stop the silent failures (small, high leverage)

1. **Stamp the collection.** Write `{embedding_model, embedding_dim, query_instruction}` into the
   collection metadata in `ChromaStore.get_or_create_collection()`, and refuse (loudly) to
   upsert/query when the configured model disagrees. This is the only fix that catches the
   same-dimension swap. Interim: `workarounds/check_index_model.py`.
2. **Stop swallowing query errors.** In `tools/search.py`, catch `InvalidArgumentError` (and
   friends) separately and return the message; keep `continue` only for genuinely optional
   collections.
3. **Fix `docker-compose.yml`'s `-rag-mcp-new-pip-data`** typo (one character).
4. **Make the printed "re-index later" command use the names the run actually used** in both setup
   scripts.

### Tier 1 — make the model a first-class parameter

5. `ARG EMBED_MODEL` / `ARG RERANK_MODEL` / `ARG TRUST_REMOTE_CODE`, used by the warm-up and
   recorded in the image (`/app/model-card.json`).
6. Pass `trust_remote_code` from config (`embedding.trust_remote_code: false` by default) into
   `EmbeddingGenerator`/`Reranker`, and add `einops` to the image.
7. **Move the HF cache to a named volume** (`rag-mcp-models:/opt/hf-cache`) and make
   `HF_HUB_OFFLINE` conditional. Changing models stops requiring a rebuild, and the image drops
   0.22–0.45 GB.
8. Derive `query_instruction` from the model family (table in `workarounds/warm_model.py`) instead
   of defaulting to the BGE string, and prefer `encode_query()` / `encode_document()` so
   model-declared prompts are honoured exactly once.
9. Pin the inference stack: `sentence-transformers>=5,<7`, `transformers>=4.45,<6`, `torch==2.*`
   from the CPU index, and either use `uv.lock` in the Dockerfile or delete it so it stops lying.

### Tier 2 — one deployment contract, enforced

10. A single `deploy.env` (or `docker/defaults.env`) holding `IMAGE`, `DATA_VOLUME`,
    `MODEL_VOLUME`, `MCP_SERVER_NAME`, `EMBED_MODEL`, `RERANK_MODEL`. `.bat`, `.sh`, compose,
    `start-http.ps1` and `setup_mcp_config.py` all read it; no literal names anywhere else.
11. Replace the two setup scripts with one cross-platform implementation (the `.sh`/`.bat` become
    three-line wrappers). `workarounds/setup-docker-unified.sh` is a working reference with
    `.bat`-equivalent flags, verified end-to-end with a stubbed `docker`.
12. **CI:** build the image; assert `docker image inspect -f {{.Size}}` is under a budget; run
    `--list-tools`; index a fixture repo; run one real search and assert a hit; run the audit script
    as a test. Add a matrix over 2–3 embedding models (one remote-code model) — that single job
    would have caught Issues 1, 2 and 3.
13. Delete the duplicated `indexer.py` / `_indexer.py` and `server.py` / `_server.py` bodies (make
    the root scripts thin `main()` shims), and generate `src/rag_mcp/data/*` at build time instead
    of committing a second copy.

### Tier 3 — if sub-1 GB is a real requirement

14. Drop torch at inference time: ONNX Runtime (already present, 67 MB) + `optimum`/`fastembed`
    with pre-exported ONNX models removes torch, sympy, scikit-learn, scipy and most of
    transformers (~0.9–1.0 GB measured), landing the image in the 0.8–1.0 GB class. The cost is
    architectural: arbitrary `sentence-transformers` checkpoints are no longer loadable, so the
    "any HF model" promise would have to become "any model we publish an ONNX export for". That is
    precisely the dev/prod trade-off this repo has been deferring; it should be decided explicitly,
    not by default.

---

## 10. What still needs a real Docker daemon

| Check | Command |
|---|---|
| Actual image size + layer attribution | `docker image inspect -f '{{.Size}}' rag-mcp:latest`, `docker history --no-trunc rag-mcp:latest` |
| Whether `COPY --from=builder /opt/hf-cache` duplicates blobs (HF uses `snapshots/*` symlinks into `blobs/*`) | `docker run --rm rag-mcp:latest sh -c 'du -sh /opt/hf-cache; find /opt/hf-cache -type l | head'` |
| Footprint inside the real image | `docker run --rm -v /workspace/analysis/tools:/t:ro rag-mcp:latest python /t/measure_footprint.py /usr/local/lib/python3.13/site-packages` |
| Real model cache size per model | `docker run --rm -v rag-mcp-models:/opt/hf-cache alpine du -sh /opt/hf-cache` |
| Trim savings end-to-end | build `workarounds/Dockerfile.model-agnostic`, compare sizes, run `--list-tools` + one search |
| Compose validity after the fix | `docker compose -f workarounds/docker-compose.fixed.yml config -q` |