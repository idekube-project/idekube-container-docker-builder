# CLAUDE.md

## Purpose

Python CLI tool for building, publishing, and managing multi-architecture Docker container images with DAG-based dependency resolution. Used as a submodule (`third_party/docker-builder`) by IDEKube container projects.

## Tech Stack

- Python 3 (stdlib only: argparse, json, subprocess, pathlib, concurrent.futures)
- Docker / Docker Buildx (multi-arch)
- Bash helper scripts
- GitHub Actions for CI/CD

## Key Commands

### Via Makefile (from consuming project)

```bash
# Single image
make build BRANCH=featured/base LINEUP=base
make buildx BRANCH=featured/base
make publishx BRANCH=featured/base
make publish BRANCH=featured/base

# Batch (DAG-ordered, parallel within tiers)
make build-all LINEUP=base MAX_PARALLEL=2
make buildx-all
make publishx-all
make publish-all

# Info
make discover          # Show all images and DAG tiers
make list              # List images in lineup
make deps BRANCH=featured/base   # Show dependency chain
make ci-matrix         # GitHub Actions matrix JSON

# Other
make manifest BRANCH=featured/base
make manifest-all
make tag-stable BRANCH=featured/base
```

### Direct Python CLI

```bash
python3 build.py <command> [args] --project-root=. --lineup=base
python3 build.py discover
python3 build.py build featured/base --dry-run
python3 build.py buildx-all --parallel=4 --continue-on-error
python3 build.py ci-matrix --pretty
python3 build.py qemu-build-all --dry-run
```

All mutating commands support `--dry-run`.

## Project Structure

```
build.py                          # Main orchestrator (all logic, ~1090 lines)
Makefile.include                  # Make wrapper, included by consuming projects
config.json                       # (in consuming project) Registry/author/name/archs/lineups
scripts/shell/
  docker_common.sh                # Shared env setup (legacy shell path)
  build_image.sh                  # Single native build (legacy)
  buildx_image.sh                 # Multi-arch buildx build (legacy)
  publish_image.sh                # Push single-arch (legacy)
  publishx_image.sh               # Buildx build+push (legacy)
ci-templates/github/publish.yml   # Reusable GitHub Actions workflow
tests/helpers/                    # Test helpers (placeholder)
```

## Architecture

**Pipeline**: Discover -> DAG Resolve -> Build -> Publish -> Manifest/Tag-Stable

1. **Discovery**: Scans `docker/*/images.json` and `qemu/*/images.json` in the consuming project
2. **DAG Resolution**: Topological sort via BFS on `depends_on` chains, produces tiers
3. **Build**: Executes tier-by-tier; images within a tier run in parallel (`ThreadPoolExecutor`)
4. **Publish**: Pushes to registry; buildx can build+push atomically (`publishx`)
5. **Manifest**: Creates multi-arch manifests from per-arch tags
6. **Tag-Stable**: Retags versioned image as `<slug>-stable` via `docker buildx imagetools create`

## Key Concepts

- **Branch**: Image identifier matching its subdirectory path (e.g., `featured/base`). Converted to slug (`featured-base`) for Docker tags.
- **Lineup**: Build variant defined in `config.json` with its own `dockerargs_file` and `archs` list. Default lineup is `base`.
- **Internal vs External Dependencies**: Internal deps (within same repo) use versioned tag (`<slug>-<git_tag>`). External deps use stable tag (`<slug>-stable`). Controlled by checking if `depends_on` branch exists locally.
- **Stamp Files**: QEMU operations use `.cache/` stamp files to skip completed steps (idempotent rebuilds).
- **DAG Tiers**: Images with no unmet dependencies form a tier and build in parallel. Next tier starts only after the current one completes.
- **Tag Format**: `<slug>-<git_tag><tag_postfix>` (e.g., `featured-base-v1.2.0-cuda`)

## Configuration Files (in consuming project)

- **config.json**: `{ "registry", "author", "name", "archs": ["amd64","arm64"], "lineups": { "<name>": { "dockerargs_file", "archs" } } }`
- **.dockerargs.base / .dockerargs.<lineup>**: `KEY=VALUE` per line, passed as `--build-arg`. May contain `TAG_POSTFIX`.
- **docker/<name>/images.json**: `{ "branch": "featured/base", "depends_on": "base", "archs": ["amd64","arm64"] }`
- **docker/<name>/Dockerfile**: Standard Dockerfile, receives `BASE_IMAGE`, `REGISTRY`, `AUTHOR`, `NAME`, `GIT_TAG` as build args.

## Environment Variable Overrides

`REGISTRY`, `AUTHOR`, `NAME`, `GIT_TAG`, `BASE_TAG`, `TAG_POSTFIX`, `DOCKER_BUILDKIT` -- all override config.json / .dockerargs defaults.
