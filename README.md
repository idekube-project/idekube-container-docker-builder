# IDEKube Container Docker Builder

A Python CLI tool for building, publishing, and managing multi-architecture Docker container images with DAG-based dependency resolution. Designed to be included as a submodule (`third_party/docker-builder`) in IDEKube container projects.

## Features

- **DAG-based dependency resolution** -- topological sort ensures images build in correct order
- **Multi-architecture builds** -- Docker Buildx support for amd64 and arm64 (configurable)
- **Parallel execution** -- images within the same DAG tier build concurrently via ThreadPoolExecutor
- **Lineup system** -- define build variants with different architectures and build arguments
- **Automatic base image injection** -- internal deps use versioned tags, external deps use stable tags
- **QEMU VM pipeline** -- provision root disks via QEMU for specialized image builds
- **CI/CD integration** -- generates GitHub Actions matrix JSON for dynamic workflows
- **Dry-run mode** -- preview all commands without executing them
- **Zero external dependencies** -- pure Python 3 stdlib (argparse, json, subprocess, pathlib, concurrent.futures)

## Prerequisites

- Python 3.6+
- Docker with BuildKit support
- Docker Buildx (for multi-arch builds)
- Git (for automatic tag detection)
- `hub-tool` (optional, only for `rmmanifest` commands)

## Quick Start

### 1. Add as a submodule

```bash
git submodule add <repo-url> third_party/docker-builder
```

### 2. Create project configuration

In your project root, create:

**config.json**
```json
{
  "registry": "docker.io",
  "author": "yourname",
  "name": "your-container",
  "archs": ["amd64", "arm64"],
  "lineups": {
    "base": {
      "dockerargs_file": ".dockerargs.base",
      "archs": ["amd64", "arm64"]
    },
    "cuda": {
      "dockerargs_file": ".dockerargs.cuda",
      "archs": ["amd64"]
    }
  }
}
```

**.dockerargs.base**
```
BASE_IMAGE=ubuntu:22.04
TAG_POSTFIX=
```

**docker/base/images.json**
```json
{
  "branch": "base",
  "archs": ["amd64", "arm64"]
}
```

**docker/featured/images.json**
```json
{
  "branch": "featured/base",
  "depends_on": "base",
  "archs": ["amd64", "arm64"]
}
```

### 3. Create a Makefile

```makefile
BUILDER := third_party/docker-builder
include $(BUILDER)/Makefile.include
```

### 4. Build images

```bash
# Build a single image
make build BRANCH=base

# Build all images (respects dependency order)
make buildx-all

# Build and push all images
make publishx-all
```

## CLI Reference

The tool is invoked via `python3 build.py <command> [args]` or through Make targets. All commands accept `--project-root` (default: `.`) and `--lineup` (default: `base`).

### Single-Image Commands

| Command | Description | Key Flags |
|---------|-------------|-----------|
| `build <branch>` | Build image for native architecture | `--arch`, `--dry-run` |
| `buildx <branch>` | Multi-arch build via Buildx (no push) | `--dry-run` |
| `publishx <branch>` | Multi-arch build and push via Buildx | `--release`, `--dry-run` |
| `publish <branch>` | Push a single-arch image to registry | `--arch`, `--dry-run` |
| `tag-stable <branch>` | Retag image as `<slug>-stable` | `--dry-run` |

### Batch Commands

All batch commands respect the DAG order and support `--parallel` (default: 2) and `--continue-on-error`.

| Command | Description | Key Flags |
|---------|-------------|-----------|
| `build-all` | Build all images (native arch) | `--arch`, `--parallel`, `--dry-run` |
| `buildx-all` | Multi-arch build all images | `--parallel`, `--dry-run` |
| `publishx-all` | Multi-arch build and push all images | `--release`, `--parallel`, `--dry-run` |
| `publish-all` | Push all single-arch images | `--arch`, `--parallel`, `--dry-run` |

### Manifest Commands

| Command | Description | Key Flags |
|---------|-------------|-----------|
| `manifest <branch>` | Create multi-arch manifest for one image | `--release`, `--dry-run` |
| `manifest-all` | Create manifests for all images | `--release`, `--dry-run` |
| `rmmanifest <branch>` | Remove per-arch tags for one image | `--dry-run` |
| `rmmanifest-all` | Remove per-arch tags for all images | `--dry-run` |

### QEMU Commands

| Command | Description |
|---------|-------------|
| `qemu-prepare` | Download QEMU files and base cloud images |
| `qemu-build-tools` | Build cloud-localds, QEMU engine, and healthcheck binaries |
| `qemu-build-root <branch>` | Provision root disk via QEMU VM |
| `qemu-build <branch>` | Build final QEMU Docker image |
| `qemu-publish <branch>` | Push QEMU image to registry |
| `qemu-manifest <branch>` | Create and push multi-arch manifest |
| `qemu-build-all` | Full QEMU pipeline for all branches |
| `qemu-publish-all` | Full QEMU pipeline with push for all branches |

### Info Commands

| Command | Description |
|---------|-------------|
| `discover` | List all discovered images and their DAG tiers |
| `list` | List images in the current lineup |
| `deps <branch>` | Show full dependency chain (root first) |
| `ci-matrix` | Output GitHub Actions matrix JSON (`--pretty` for formatting) |
| `qemu-ci-matrix` | Output tier JSON for QEMU branches |

### Make Targets

The `Makefile.include` exposes these variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `BRANCH` | (empty) | Image branch to operate on |
| `LINEUP` | `base` | Lineup name |
| `MAX_PARALLEL` | `2` | Max parallel builds within a tier |

```bash
make build BRANCH=featured/base LINEUP=base
make buildx-all MAX_PARALLEL=4
make publishx BRANCH=base
make ci-matrix
make discover
make deps BRANCH=featured/base
```

## Configuration

### config.json

Project-level configuration in the consuming project root.

```json
{
  "registry": "docker.io",
  "author": "yourname",
  "name": "your-container",
  "archs": ["amd64", "arm64"],
  "lineups": {
    "base": {
      "dockerargs_file": ".dockerargs.base",
      "archs": ["amd64", "arm64"]
    }
  }
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `registry` | Yes | Container registry (e.g., `docker.io`, `ghcr.io`) |
| `author` | Yes | Registry namespace / organization |
| `name` | Yes | Image repository name |
| `archs` | No | Default architectures (default: `["amd64", "arm64"]`) |
| `lineups` | No | Named build variants with their own dockerargs and archs |

### .dockerargs files

Key-value files (one `KEY=VALUE` per line) passed as Docker `--build-arg` flags. Lines starting with `#` and blank lines are ignored.

```
BASE_IMAGE=ubuntu:22.04
TAG_POSTFIX=-cuda
CUDA_VERSION=12.0
```

The `TAG_POSTFIX` key is special -- it is appended to all generated Docker tags (e.g., `featured-base-v1.0.0-cuda`).

### images.json

Per-image metadata in `docker/<name>/images.json` or `qemu/<name>/images.json`.

```json
{
  "branch": "featured/base",
  "depends_on": "base",
  "archs": ["amd64", "arm64"]
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `branch` | Yes | Unique image identifier, converted to slug for tags (`/` -> `-`) |
| `depends_on` | No | Branch name of parent image (forms the DAG) |
| `archs` | No | Override architectures for this specific image |

## Architecture

### Build Pipeline

```
Discovery -> DAG Resolution -> Tier Execution -> Registry
```

1. **Discovery**: Scans `docker/*/images.json` and `qemu/*/images.json` to find all image definitions
2. **DAG Resolution**: Builds a dependency graph from `depends_on` fields, performs topological sort into tiers using BFS. Detects cycles.
3. **Lineup Filtering**: Filters images by architecture compatibility with the selected lineup
4. **Tier Execution**: Processes tiers sequentially. Within each tier, images build in parallel (up to `--parallel` workers)
5. **Registry Operations**: Push, manifest creation, and stable tagging

### Dependency Resolution

Images declare dependencies via the `depends_on` field in `images.json`. The tool distinguishes:

- **Internal dependencies**: The depended-on branch exists as `docker/<name>/images.json` in the same project. The `BASE_IMAGE` build-arg is set to the versioned tag (`<slug>-<git_tag><postfix>`).
- **External dependencies**: The depended-on branch is not found locally. The `BASE_IMAGE` build-arg uses the stable tag (`<slug>-stable<postfix>`), overridable via the `BASE_TAG` env var.

### Multi-Architecture Strategy

- **Buildx** (preferred): Uses `docker buildx build --platform=linux/amd64,linux/arm64` to produce multi-arch images in a single build. The builder named `idekube` (docker-container driver) is created automatically.
- **Manifest** (alternative): Build per-arch images separately, push each with an arch-suffixed tag, then create a multi-arch manifest pointing to all of them.
- **Tag-Stable**: Uses `docker buildx imagetools create` to retag a versioned manifest as stable without re-pushing layers.

### Tag Naming

Tags follow the pattern: `<slug>-<git_tag><tag_postfix>`

- **slug**: Branch with `/` replaced by `-` (e.g., `featured/base` -> `featured-base`)
- **git_tag**: Latest git tag (excluding pre-releases), or `latest` if none found. Overridable via `GIT_TAG` env var.
- **tag_postfix**: From `.dockerargs` file (e.g., `-cuda`). Overridable via env var.

Example full reference: `docker.io/yourname/your-container:featured-base-v1.2.0-cuda`

### QEMU Pipeline

For images that require full VM provisioning (e.g., root filesystem builds):

```
qemu-prepare -> qemu-build-tools -> qemu-build-root -> qemu-build -> qemu-publish -> qemu-manifest
```

Each step uses stamp files in `.cache/` for idempotency. The `qemu-build-all` and `qemu-publish-all` commands run the full pipeline respecting the QEMU image DAG.

## Environment Variables

All environment variables override their corresponding config.json or .dockerargs values.

| Variable | Description | Default |
|----------|-------------|---------|
| `REGISTRY` | Container registry | From config.json |
| `AUTHOR` | Registry namespace | From config.json |
| `NAME` | Image repository name | From config.json |
| `GIT_TAG` | Version tag for images | Latest git tag or `latest` |
| `BASE_TAG` | Tag suffix for external dependencies | `stable` |
| `TAG_POSTFIX` | Appended to all generated tags | From .dockerargs |
| `DOCKER_BUILDKIT` | Enable Docker BuildKit | `1` (set automatically) |

Additionally, any key present in the `.dockerargs` file can be overridden by setting an environment variable with the same name.

## CI/CD Integration

### GitHub Actions

A reusable workflow is provided at `ci-templates/github/publish.yml`. It triggers on version tag pushes (`v*`) and manual dispatch.

**Setup**:

1. Add repository secrets: `DOCKER_USERNAME` and `DOCKER_PASSWORD`
2. Copy or reference the workflow in your project's `.github/workflows/`

**Workflow behavior**:

- On tag push: builds and pushes all images, then tags them as stable
- On manual dispatch: optionally specify a single branch to build, or build all

### Matrix Generation

Use `ci-matrix` to generate dynamic job matrices:

```bash
python3 build.py ci-matrix --pretty --lineup=base
```

Output:
```json
{
  "lineup": "base",
  "tier0": ["base"],
  "tier1": ["featured/base", "featured/extra"]
}
```

This JSON can be consumed by GitHub Actions `fromJson()` for parallel tier-based CI jobs.

## Project Structure

```
build.py                          # Main CLI orchestrator (~1090 lines)
Makefile.include                  # Make target definitions for consuming projects
config.json                       # (in consuming project) Project configuration
.dockerargs.base                  # (in consuming project) Default build arguments
ci-templates/
  github/
    publish.yml                   # Reusable GitHub Actions workflow
scripts/
  shell/
    docker_common.sh              # Shared environment setup (legacy)
    build_image.sh                # Native Docker build (legacy)
    buildx_image.sh               # Multi-arch Buildx build (legacy)
    publish_image.sh              # Push single-arch image (legacy)
    publishx_image.sh             # Buildx build+push (legacy)
tests/
  helpers/                        # Test helper utilities
docker/                           # (in consuming project) Image subdirectories
  <name>/
    Dockerfile
    images.json
qemu/                             # (in consuming project) QEMU image subdirectories
  <name>/
    images.json
```

## License

See the project repository for license information.
