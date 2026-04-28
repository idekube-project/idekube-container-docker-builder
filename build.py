#!/usr/bin/env python3
"""
IDEKube Container Build Orchestrator (v2 - Sub-directory driven)

Stateless CLI that scans docker/*/ and qemu/*/ sub-directories for images.json,
resolves the dependency DAG, injects BASE_IMAGE build-arg, and runs Docker
build/push commands.

Usage:
    python3 build.py <command> [args] [--lineup=base]

Discovery:
    discover                List all discovered images and their DAG

Docker commands:
    build <branch>          Build single image (native arch)
    buildx <branch>         Multi-arch build via buildx (no push)
    publishx <branch>       Multi-arch build+push via buildx
    publish <branch>        Push single-arch image to registry
    build-all               Build all images (DAG order)
    buildx-all              Multi-arch build all images (DAG order)
    publishx-all            Multi-arch build+push all (DAG order)
    publish-all             Push all single-arch images (DAG order)

Manifest commands:
    manifest <branch>       Create multi-arch manifest for one image
    manifest-all            Create manifests for all images
    rmmanifest <branch>     Remove per-arch tags for one image
    rmmanifest-all          Remove per-arch tags for all images

Stable tag:
    tag-stable <branch>     Retag an image as <slug>-stable

QEMU commands:
    qemu-prepare            Download QEMU files and base cloud images
    qemu-build-tools        Build cloud-localds and QEMU engine images
    qemu-build-root <br>    Provision root disk via QEMU VM
    qemu-build <branch>     Build final QEMU Docker image
    qemu-publish <branch>   Push QEMU image to registry (arch-tagged)
    qemu-manifest <branch>  Create and push multi-arch manifest
    qemu-build-all          Full pipeline for all QEMU branches
    qemu-publish-all        Full pipeline + push for all QEMU branches

Info commands:
    ci-matrix               Output JSON for GitHub Actions fromJson()
    qemu-ci-matrix          Output tier JSON for QEMU branches
    list                    List images
    deps <branch>           Show dependency chain for an image
"""

import argparse
import json
import os
import platform
import subprocess
import sys
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# ---------------------------------------------------------------------------
# Project config loading
# ---------------------------------------------------------------------------

def load_project_config(project_root):
    """Load config.json from project root."""
    path = project_root / "config.json"
    if not path.is_file():
        sys.exit(f"Error: config.json not found in {project_root}")
    with open(path) as f:
        config = json.load(f)
    for key in ("registry", "author", "name"):
        if key not in config:
            sys.exit(f"Error: missing key '{key}' in config.json")
    return config


def get_lineup_config(project_config, lineup_name):
    """Get lineup-specific config (dockerargs_file, archs)."""
    lineups = project_config.get("lineups", {})
    if lineup_name not in lineups:
        # Fall back to project defaults
        return {
            "dockerargs_file": project_config.get("dockerargs_file", ".dockerargs.base"),
            "archs": project_config.get("archs", ["amd64", "arm64"]),
        }
    lineup = lineups[lineup_name]
    return {
        "dockerargs_file": lineup.get("dockerargs_file", project_config.get("dockerargs_file", ".dockerargs.base")),
        "archs": lineup.get("archs", project_config.get("archs", ["amd64", "arm64"])),
    }


# ---------------------------------------------------------------------------
# Image discovery
# ---------------------------------------------------------------------------

def discover_images(project_root):
    """Scan docker/*/ and qemu/*/ for images.json, return list of image configs."""
    images = []

    for images_json in sorted(project_root.glob("docker/*/images.json")):
        with open(images_json) as f:
            cfg = json.load(f)
        cfg["_type"] = "docker"
        cfg["_path"] = images_json.parent
        cfg["_dockerfile"] = images_json.parent / "Dockerfile"
        if "branch" not in cfg:
            sys.exit(f"Error: 'branch' missing in {images_json}")
        images.append(cfg)

    for images_json in sorted(project_root.glob("qemu/*/images.json")):
        with open(images_json) as f:
            cfg = json.load(f)
        cfg["_type"] = "qemu"
        cfg["_path"] = images_json.parent
        if "branch" not in cfg:
            sys.exit(f"Error: 'branch' missing in {images_json}")
        images.append(cfg)

    return images


def filter_images_by_type(images, image_type):
    """Filter discovered images by type (docker or qemu)."""
    return [img for img in images if img["_type"] == image_type]


def filter_images_by_lineup(images, project_config, lineup_name):
    """Filter images that are valid for a given lineup (by archs compatibility)."""
    lineup_cfg = get_lineup_config(project_config, lineup_name)
    lineup_archs = set(lineup_cfg["archs"])

    result = []
    for img in images:
        img_archs = set(img.get("archs", project_config.get("archs", ["amd64", "arm64"])))
        # Image is in lineup if its archs intersect with lineup archs
        if img_archs & lineup_archs:
            result.append(img)
    return result


def get_image_by_branch(images, branch):
    """Find an image config by branch name."""
    for img in images:
        if img["branch"] == branch:
            return img
    return None


# ---------------------------------------------------------------------------
# Dockerargs parsing
# ---------------------------------------------------------------------------

def parse_dockerargs(filepath):
    """Parse a .dockerargs file (KEY=VALUE per line) into a dict."""
    args = {}
    path = Path(filepath)
    if not path.is_file():
        sys.exit(f"Error: dockerargs file not found: {filepath}")
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            args[key.strip()] = value.strip()
    return args


def dockerargs_to_build_flags(args):
    """Convert a dict of build args to Docker --build-arg flags."""
    flags = []
    for key, value in args.items():
        flags.extend(["--build-arg", f"{key}={value}"])
    return flags


# ---------------------------------------------------------------------------
# Environment detection
# ---------------------------------------------------------------------------

def detect_git_tag():
    """Detect latest git tag, fallback to 'latest'."""
    env_tag = os.environ.get("GIT_TAG")
    if env_tag:
        return env_tag
    try:
        result = subprocess.run(
            ["git", "tag", "--list", "--sort=-v:refname"],
            capture_output=True, text=True, check=True,
        )
        tags = [t for t in result.stdout.strip().splitlines()
                if not any(x in t for x in ("-rc", "-alpha", "-beta"))]
        return tags[0] if tags else "latest"
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "latest"


def detect_arch():
    """Detect native architecture, normalized to amd64/arm64."""
    machine = platform.machine()
    mapping = {"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64", "amd64": "amd64"}
    return mapping.get(machine, machine)


# ---------------------------------------------------------------------------
# Image reference computation
# ---------------------------------------------------------------------------

def branch_to_slug(branch):
    """Convert branch name to Docker tag slug: featured/base -> featured-base."""
    return branch.replace("/", "-")


def compute_refs(project_config, branch, lineup_name, project_root):
    """Compute image tag and full ref for a branch+lineup combination."""
    registry = os.environ.get("REGISTRY", project_config["registry"])
    author = os.environ.get("AUTHOR", project_config["author"])
    name = os.environ.get("NAME", project_config["name"])
    git_tag = detect_git_tag()

    lineup_cfg = get_lineup_config(project_config, lineup_name)
    docker_args = parse_dockerargs(project_root / lineup_cfg["dockerargs_file"])
    # Allow environment variables to override dockerargs values
    for key in list(docker_args.keys()):
        env_value = os.environ.get(key)
        if env_value is not None:
            docker_args[key] = env_value
    tag_postfix = docker_args.get("TAG_POSTFIX", "")

    slug = branch_to_slug(branch)
    tag = f"{slug}-{git_tag}{tag_postfix}"
    latest_tag = f"{slug}-latest{tag_postfix}"
    stable_tag = f"{slug}-stable{tag_postfix}"
    image_ref = f"{registry}/{author}/{name}:{tag}"
    latest_ref = f"{registry}/{author}/{name}:{latest_tag}"
    stable_ref = f"{registry}/{author}/{name}:{stable_tag}"

    return {
        "registry": registry,
        "author": author,
        "name": name,
        "git_tag": git_tag,
        "tag_postfix": tag_postfix,
        "slug": slug,
        "tag": tag,
        "latest_tag": latest_tag,
        "stable_tag": stable_tag,
        "image_ref": image_ref,
        "latest_ref": latest_ref,
        "stable_ref": stable_ref,
        "docker_args": docker_args,
    }


def compute_base_image_arg(image_cfg, project_config, lineup_name, project_root):
    """Compute the BASE_IMAGE build-arg value from depends_on.

    For external dependencies (base image from another repo), uses the stable tag.
    For internal dependencies (within same repo), uses the versioned tag.
    """
    depends_on = image_cfg.get("depends_on")
    if depends_on is None:
        return None  # Root image, uses BASE_IMAGE from .dockerargs

    registry = os.environ.get("REGISTRY", project_config["registry"])
    author = os.environ.get("AUTHOR", project_config["author"])
    name = os.environ.get("NAME", project_config["name"])

    lineup_cfg = get_lineup_config(project_config, lineup_name)
    docker_args = parse_dockerargs(project_root / lineup_cfg["dockerargs_file"])
    for key in list(docker_args.keys()):
        env_value = os.environ.get(key)
        if env_value is not None:
            docker_args[key] = env_value
    tag_postfix = docker_args.get("TAG_POSTFIX", "")

    slug = branch_to_slug(depends_on)

    # Check if this is an internal dependency (within same repo)
    # by checking if the depends_on branch exists locally
    internal = (project_root / "docker" / depends_on.split("/")[-1] / "images.json").exists()

    if internal:
        # Use versioned tag for internal deps
        git_tag = detect_git_tag()
        tag = f"{slug}-{git_tag}{tag_postfix}"
    else:
        # Use stable tag for external deps (from another repo)
        base_tag = os.environ.get("BASE_TAG", "stable")
        tag = f"{slug}-{base_tag}{tag_postfix}"

    return f"{registry}/{author}/{name}:{tag}"


def get_full_build_args(refs, base_image=None):
    """Build the complete --build-arg flag list."""
    flags = dockerargs_to_build_flags(refs["docker_args"])
    flags.extend(["--build-arg", f"REGISTRY={refs['registry']}"])
    flags.extend(["--build-arg", f"AUTHOR={refs['author']}"])
    flags.extend(["--build-arg", f"NAME={refs['name']}"])
    flags.extend(["--build-arg", f"GIT_TAG={refs['git_tag']}"])
    if base_image:
        flags.extend(["--build-arg", f"BASE_IMAGE={base_image}"])
    return flags


# ---------------------------------------------------------------------------
# DAG operations
# ---------------------------------------------------------------------------

def resolve_dag(images):
    """Build DAG from discovered images, return topo-sorted tiers.

    Only internal dependencies (within the same repo) form the DAG.
    External dependencies are assumed to already exist in the registry.
    """
    local_branches = {img["branch"] for img in images}
    branch_to_img = {img["branch"]: img for img in images}

    in_degree = {img["branch"]: 0 for img in images}
    dependents = {img["branch"]: [] for img in images}

    for img in images:
        dep = img.get("depends_on")
        if dep and dep in local_branches:
            in_degree[img["branch"]] += 1
            dependents[dep].append(img["branch"])

    tiers = []
    queue = deque(sorted([b for b in in_degree if in_degree[b] == 0]))

    while queue:
        tier = sorted(queue)
        tiers.append(tier)
        next_queue = deque()
        for b in tier:
            for child in dependents[b]:
                in_degree[child] -= 1
                if in_degree[child] == 0:
                    next_queue.append(child)
        queue = next_queue

    placed = sum(len(t) for t in tiers)
    if placed != len(images):
        sys.exit("Error: dependency cycle detected")

    return tiers


def get_dep_chain(images, branch):
    """Return the full dependency chain for an image (root first)."""
    branch_to_img = {img["branch"]: img for img in images}
    if branch not in branch_to_img:
        sys.exit(f"Error: unknown image '{branch}'")
    chain = [branch]
    dep = branch_to_img[branch].get("depends_on")
    while dep:
        chain.append(dep)
        if dep in branch_to_img:
            dep = branch_to_img[dep].get("depends_on")
        else:
            break
    chain.reverse()
    return chain


# ---------------------------------------------------------------------------
# Docker command runners
# ---------------------------------------------------------------------------

def ensure_buildx_builder(dry_run=False):
    """Ensure the 'idekube' buildx builder exists."""
    try:
        result = subprocess.run(
            ["docker", "buildx", "ls"],
            capture_output=True, text=True, check=True,
        )
        if "idekube" in result.stdout:
            return
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass

    cmd = ["docker", "buildx", "create", "--name", "idekube", "--driver", "docker-container"]
    if dry_run:
        print(f"[dry-run] {' '.join(cmd)}")
    else:
        print(f"Creating buildx builder: {' '.join(cmd)}")
        subprocess.run(cmd, check=True)


def run_build(project_config, image_cfg, lineup_name, project_root, dry_run=False, arch=None):
    """Build a single image for the specified (or native) architecture."""
    branch = image_cfg["branch"]
    refs = compute_refs(project_config, branch, lineup_name, project_root)
    base_image = compute_base_image_arg(image_cfg, project_config, lineup_name, project_root)
    build_args = get_full_build_args(refs, base_image)
    arch = arch or detect_arch()
    dockerfile = str(image_cfg["_dockerfile"])

    cmd = [
        "docker", "build",
        *build_args,
        ".", "-t", f"{refs['image_ref']}-{arch}",
        "-t", refs["image_ref"],
        "-f", dockerfile,
    ]

    if dry_run:
        print(f"[dry-run] DOCKER_BUILDKIT=1 {' '.join(cmd)}")
        return

    env = {**os.environ, "DOCKER_BUILDKIT": "1"}
    print(f"Building {refs['image_ref']} ({arch})")
    subprocess.run(cmd, check=True, env=env)

    # Clean dangling images
    try:
        result = subprocess.run(
            ["docker", "images", "--filter", "dangling=true", "-q"],
            capture_output=True, text=True, check=True,
        )
        dangling = result.stdout.strip()
        if dangling:
            subprocess.run(
                ["docker", "rmi"] + dangling.splitlines(),
                check=False, capture_output=True,
            )
    except subprocess.CalledProcessError:
        pass


def run_buildx(project_config, image_cfg, lineup_name, project_root,
               push=False, release=False, dry_run=False):
    """Multi-arch build (and optionally push) via buildx."""
    branch = image_cfg["branch"]
    refs = compute_refs(project_config, branch, lineup_name, project_root)
    base_image = compute_base_image_arg(image_cfg, project_config, lineup_name, project_root)
    build_args = get_full_build_args(refs, base_image)
    lineup_cfg = get_lineup_config(project_config, lineup_name)
    archs = image_cfg.get("archs", lineup_cfg["archs"])
    platforms = ",".join(f"linux/{a}" for a in archs)
    dockerfile = str(image_cfg["_dockerfile"])

    ensure_buildx_builder(dry_run=dry_run)

    cmd = [
        "docker", "buildx", "build",
        "--builder", "idekube",
        f"--platform={platforms}",
        *build_args,
    ]
    if push:
        cmd.append("--push")
    cmd.extend([".", "-t", refs["image_ref"], "-f", dockerfile])
    if release and push:
        cmd.extend(["-t", refs["latest_ref"]])

    if dry_run:
        action = "publishx" if push else "buildx"
        print(f"[dry-run] ({action}) {' '.join(cmd)}")
        return

    action = "Publishing" if push else "Building (multi-arch)"
    extra = f" (+ {refs['latest_ref']})" if release and push else ""
    print(f"{action} {refs['image_ref']}{extra}")
    subprocess.run(cmd, check=True)

    # For non-push buildx, load per-arch images locally
    if not push:
        for arch in archs:
            load_cmd = [
                "docker", "buildx", "build",
                "--builder", "idekube",
                f"--platform=linux/{arch}",
                *build_args,
                "--load", ".",
                "-t", f"{refs['image_ref']}-{arch}",
                "-f", dockerfile,
            ]
            if dry_run:
                print(f"[dry-run] (load {arch}) {' '.join(load_cmd)}")
            else:
                print(f"Loading {refs['image_ref']}-{arch}")
                subprocess.run(load_cmd, check=True)


def run_publish(project_config, image_cfg, lineup_name, project_root, dry_run=False, arch=None):
    """Push a single-arch image to the registry."""
    branch = image_cfg["branch"]
    refs = compute_refs(project_config, branch, lineup_name, project_root)
    arch = arch or detect_arch()
    tag = f"{refs['image_ref']}-{arch}"
    cmd = ["docker", "push", tag]

    if dry_run:
        print(f"[dry-run] {' '.join(cmd)}")
        return

    print(f"Pushing {tag}")
    subprocess.run(cmd, check=True)


def run_tag_stable(project_config, image_cfg, lineup_name, project_root, dry_run=False):
    """Retag an image as <slug>-stable (create and push manifest)."""
    branch = image_cfg["branch"]
    refs = compute_refs(project_config, branch, lineup_name, project_root)
    lineup_cfg = get_lineup_config(project_config, lineup_name)
    archs = image_cfg.get("archs", lineup_cfg["archs"])

    source_ref = refs["image_ref"]
    stable_ref = refs["stable_ref"]

    if len(archs) == 1:
        # Single arch: just retag
        cmds = [
            ["docker", "buildx", "imagetools", "create", "-t", stable_ref, source_ref],
        ]
    else:
        # Multi-arch: create manifest pointing to same digests
        cmds = [
            ["docker", "buildx", "imagetools", "create", "-t", stable_ref, source_ref],
        ]

    for cmd in cmds:
        if dry_run:
            print(f"[dry-run] {' '.join(cmd)}")
        else:
            print(f"Tagging {stable_ref} -> {source_ref}")
            subprocess.run(cmd, check=True)


# ---------------------------------------------------------------------------
# Manifest operations
# ---------------------------------------------------------------------------

def run_manifest(project_config, image_cfg, lineup_name, project_root,
                 release=False, dry_run=False):
    """Create a multi-arch manifest for one image."""
    branch = image_cfg["branch"]
    refs = compute_refs(project_config, branch, lineup_name, project_root)
    lineup_cfg = get_lineup_config(project_config, lineup_name)
    archs = image_cfg.get("archs", lineup_cfg["archs"])
    ref = refs["image_ref"]
    arch_refs = [f"{ref}-{a}" for a in archs]

    targets = [ref]
    if release:
        targets.append(refs["latest_ref"])

    for target in targets:
        cmds = [
            (["docker", "manifest", "rm", target], True),
            (["docker", "manifest", "create", target, *arch_refs], False),
        ]
        for a in archs:
            cmds.append((
                ["docker", "manifest", "annotate", "--os", "linux", "--arch", a, target, f"{ref}-{a}"],
                False,
            ))
        cmds.append((["docker", "manifest", "push", target], False))

        for cmd, ignore_error in cmds:
            if dry_run:
                prefix = "[dry-run] " + ("(ignore-error) " if ignore_error else "")
                print(f"{prefix}{' '.join(cmd)}")
            else:
                subprocess.run(cmd, check=not ignore_error, capture_output=ignore_error)


def run_rmmanifest(project_config, image_cfg, lineup_name, project_root, dry_run=False):
    """Remove per-arch tags for one image from the registry."""
    branch = image_cfg["branch"]
    refs = compute_refs(project_config, branch, lineup_name, project_root)
    lineup_cfg = get_lineup_config(project_config, lineup_name)
    archs = image_cfg.get("archs", lineup_cfg["archs"])

    for a in archs:
        tag = f"{refs['image_ref']}-{a}"
        cmd = ["hub-tool", "tag", "rm", tag]
        if dry_run:
            print(f"[dry-run] (ignore-error) {' '.join(cmd)}")
        else:
            subprocess.run(cmd, check=False)


# ---------------------------------------------------------------------------
# QEMU operations
# ---------------------------------------------------------------------------

def qemu_prepare(project_root, dry_run=False):
    """Download QEMU files and base cloud images."""
    stamp_files = project_root / ".cache/qemu_files/.ready"
    if stamp_files.exists():
        print("QEMU files already prepared (stamp exists), skipping")
    else:
        cmd = ["bash", "scripts/shell/prepare_qemu_files.sh"]
        if dry_run:
            print(f"[dry-run] {' '.join(cmd)}")
        else:
            subprocess.run(cmd, check=True, cwd=str(project_root))

    stamp_images = project_root / ".cache/qemu_images/artifacts/empty"
    if stamp_images.exists():
        print("QEMU base images already prepared (stamp exists), skipping")
    else:
        cmd = ["bash", "scripts/shell/prepare_qemu_images.sh"]
        if dry_run:
            print(f"[dry-run] {' '.join(cmd)}")
        else:
            subprocess.run(cmd, check=True, cwd=str(project_root))


def qemu_build_tools(project_root, dry_run=False):
    """Build cloud-localds, QEMU engine, and healthcheck binaries."""
    # cloud-localds
    stamp_tools = project_root / ".cache/qemu_images/cloud-localds.created"
    if stamp_tools.exists():
        print("cloud-localds already built (stamp exists), skipping")
    else:
        cmd = ["docker", "build", "-t", "cloud-localds:latest",
               "-f", "tools/utility/cloud-localds/Dockerfile", "."]
        if dry_run:
            print(f"[dry-run] {' '.join(cmd)}")
        else:
            print("Building cloud-localds")
            subprocess.run(cmd, check=True, cwd=str(project_root))
            stamp_tools.parent.mkdir(parents=True, exist_ok=True)
            stamp_tools.touch()

    # QEMU engine
    stamp_engine = project_root / ".cache/qemu_images/idekube-qemu-engine.created"
    if stamp_engine.exists():
        print("QEMU engine already built (stamp exists), skipping")
    else:
        cmd = ["docker", "build",
               "--build-arg", "ROOT_DISK_IMAGE_DIR=.cache/qemu_images/artifacts/empty",
               "-t", "idekube-qemu-engine:latest",
               "-f", "manifests/qemu/Dockerfile.engine", "."]
        if dry_run:
            print(f"[dry-run] {' '.join(cmd)}")
        else:
            print("Building QEMU engine")
            subprocess.run(cmd, check=True, cwd=str(project_root))
            stamp_engine.parent.mkdir(parents=True, exist_ok=True)
            stamp_engine.touch()

    # idekube-healthcheck cross-compile
    qemu_build_healthcheck(project_root, dry_run=dry_run)


def qemu_build_healthcheck(project_root, dry_run=False):
    """Cross-compile idekube-healthcheck for amd64+arm64."""
    out_dir = project_root / ".cache/qemu_tools"
    stamp = out_dir / ".healthcheck.ready"
    archs = ("amd64", "arm64")

    if stamp.exists() and all((out_dir / f"idekube-healthcheck.{a}").exists() for a in archs):
        print("idekube-healthcheck binaries already built (stamp exists), skipping")
        return

    out_dir.mkdir(parents=True, exist_ok=True)
    src_abs = str((project_root / "tools/idekube-healthcheck").resolve())
    out_abs = str(out_dir.resolve())

    build_cmd = " && ".join(
        f"CGO_ENABLED=0 GOOS=linux GOARCH={a} go build -trimpath -ldflags='-s -w' "
        f"-o /out/idekube-healthcheck.{a} ."
        for a in archs
    )
    cmd = [
        "docker", "run", "--rm",
        "-v", f"{src_abs}:/src",
        "-v", f"{out_abs}:/out",
        "-w", "/src",
        "golang:1.25-alpine",
        "sh", "-c", f"go mod download && {build_cmd}",
    ]

    if dry_run:
        print(f"[dry-run] {' '.join(cmd)}")
        return

    print(f"Cross-compiling idekube-healthcheck for {', '.join(archs)}")
    subprocess.run(cmd, check=True)
    stamp.touch()


def qemu_build_root(project_root, branch, dry_run=False):
    """Provision root disk via QEMU VM."""
    stamp = project_root / f".cache/{branch}/.root_ready"
    if stamp.exists():
        print(f"Root disk for {branch} already built (stamp exists), skipping")
        return

    cmd = ["bash", "scripts/shell/build_qemu_root.sh"]
    if dry_run:
        print(f"[dry-run] BRANCH={branch} {' '.join(cmd)}")
        return

    print(f"Building root disk for {branch} (this may take 20-60 minutes)")
    env = {**os.environ, "BRANCH": branch}
    subprocess.run(cmd, check=True, env=env, cwd=str(project_root))


def qemu_build(project_root, branch, dry_run=False):
    """Build final QEMU Docker image."""
    stamp = project_root / f".cache/{branch}/.image_ready"
    if stamp.exists():
        print(f"QEMU image for {branch} already built (stamp exists), skipping")
        return

    cmd = ["bash", "scripts/shell/build_qemu.sh"]
    if dry_run:
        print(f"[dry-run] BRANCH={branch} {' '.join(cmd)}")
        return

    print(f"Building QEMU image for {branch}")
    env = {**os.environ, "BRANCH": branch}
    subprocess.run(cmd, check=True, env=env, cwd=str(project_root))


def qemu_publish(project_root, branch, dry_run=False):
    """Push QEMU image to registry."""
    cmd = ["bash", "scripts/shell/publish_qemu.sh"]
    if dry_run:
        print(f"[dry-run] BRANCH={branch} {' '.join(cmd)}")
        return

    print(f"Publishing QEMU image for {branch}")
    env = {**os.environ, "BRANCH": branch}
    subprocess.run(cmd, check=True, env=env, cwd=str(project_root))


def qemu_manifest(project_root, branch, dry_run=False):
    """Create and push a multi-arch manifest for a QEMU branch."""
    cmd = ["bash", "scripts/shell/manifest_qemu.sh"]
    env = {**os.environ, "BRANCH": branch}
    if dry_run:
        print(f"[dry-run] BRANCH={branch} {' '.join(cmd)}")
        return
    print(f"Creating multi-arch manifest for QEMU {branch}")
    subprocess.run(cmd, check=True, env=env, cwd=str(project_root))


def qemu_build_all(images, project_root, publish=False, dry_run=False):
    """Run the full QEMU pipeline for all QEMU branches, respecting deps."""
    qemu_images = filter_images_by_type(images, "qemu")
    if not qemu_images:
        print("No QEMU images found")
        return

    print("=== QEMU: Preparing files and images ===")
    qemu_prepare(project_root, dry_run=dry_run)

    print("\n=== QEMU: Building tools ===")
    qemu_build_tools(project_root, dry_run=dry_run)

    tiers = resolve_dag(qemu_images)
    for tier_idx, tier_branches in enumerate(tiers):
        for branch in tier_branches:
            print(f"\n=== QEMU Tier {tier_idx}: {branch} ===")
            qemu_build_root(project_root, branch, dry_run=dry_run)
            qemu_build(project_root, branch, dry_run=dry_run)
            if publish:
                qemu_publish(project_root, branch, dry_run=dry_run)


# ---------------------------------------------------------------------------
# Batch operations
# ---------------------------------------------------------------------------

def run_batch(project_config, images, lineup_name, project_root, action_fn,
              parallel=2, continue_on_error=False, dry_run=False):
    """Execute an action for all images, respecting the DAG."""
    tiers = resolve_dag(images)

    failed = []
    for tier_idx, tier_branches in enumerate(tiers):
        print(f"\n{'='*60}")
        print(f"Tier {tier_idx}: {', '.join(tier_branches)}")
        print(f"{'='*60}")

        tier_images = [get_image_by_branch(images, b) for b in tier_branches]

        if parallel <= 1 or len(tier_images) == 1:
            for img in tier_images:
                try:
                    action_fn(project_config, img, lineup_name, project_root, dry_run=dry_run)
                except Exception as e:
                    if continue_on_error:
                        print(f"WARNING: {img['branch']} failed: {e}")
                        failed.append(img["branch"])
                    else:
                        raise
        else:
            with ThreadPoolExecutor(max_workers=parallel) as pool:
                futures = {
                    pool.submit(action_fn, project_config, img, lineup_name, project_root, dry_run): img
                    for img in tier_images
                }
                for future in as_completed(futures):
                    img = futures[future]
                    try:
                        future.result()
                    except Exception as e:
                        if continue_on_error:
                            print(f"WARNING: {img['branch']} failed: {e}")
                            failed.append(img["branch"])
                        else:
                            raise

    if failed:
        print(f"\nFailed images: {', '.join(failed)}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# CI matrix generation
# ---------------------------------------------------------------------------

def ci_matrix(images, project_config, lineup_name):
    """Generate JSON matrix for GitHub Actions fromJson()."""
    tiers = resolve_dag(images)
    result = {"lineup": lineup_name}
    for i, tier in enumerate(tiers):
        result[f"tier{i}"] = tier
    return result


def qemu_ci_matrix(images):
    """Generate JSON matrix for QEMU branches."""
    qemu_images = filter_images_by_type(images, "qemu")
    tiers = resolve_dag(qemu_images)
    result = {}
    for i, tier in enumerate(tiers):
        result[f"tier{i}"] = tier
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--project-root", default=".", help="Project root directory")
    common.add_argument("--lineup", default="base", help="Lineup name (default: base)")

    parser = argparse.ArgumentParser(
        description="IDEKube Container Build Orchestrator (v2)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # --- Discovery ---
    sub.add_parser("discover", parents=[common], help="List discovered images and DAG")

    # --- Docker single-image commands ---
    for cmd_name in ("build", "publish"):
        p = sub.add_parser(cmd_name, parents=[common], help=f"{cmd_name} a single image")
        p.add_argument("branch", help="Image branch (e.g., featured/base)")
        p.add_argument("--arch", help="Override architecture")
        p.add_argument("--dry-run", action="store_true")

    for cmd_name in ("buildx", "publishx"):
        p = sub.add_parser(cmd_name, parents=[common], help=f"{cmd_name} a single image")
        p.add_argument("branch", help="Image branch (e.g., featured/base)")
        p.add_argument("--dry-run", action="store_true")
        if cmd_name == "publishx":
            p.add_argument("--release", action="store_true",
                           help="Also push as <branch>-latest")

    # --- Docker batch commands ---
    for cmd_name in ("build-all", "buildx-all", "publishx-all", "publish-all"):
        p = sub.add_parser(cmd_name, parents=[common], help=f"{cmd_name} for all images")
        p.add_argument("--parallel", type=int, default=2)
        p.add_argument("--continue-on-error", action="store_true")
        p.add_argument("--dry-run", action="store_true")
        if cmd_name in ("build-all", "publish-all"):
            p.add_argument("--arch", help="Override architecture")
        if cmd_name == "publishx-all":
            p.add_argument("--release", action="store_true")

    # --- Stable tag ---
    p = sub.add_parser("tag-stable", parents=[common], help="Retag image as stable")
    p.add_argument("branch", help="Image branch")
    p.add_argument("--dry-run", action="store_true")

    # --- Manifest commands ---
    for cmd_name in ("manifest", "rmmanifest"):
        p = sub.add_parser(cmd_name, parents=[common], help=f"{cmd_name} for a single image")
        p.add_argument("branch", help="Image branch")
        p.add_argument("--dry-run", action="store_true")
        if cmd_name == "manifest":
            p.add_argument("--release", action="store_true")

    for cmd_name in ("manifest-all", "rmmanifest-all"):
        p = sub.add_parser(cmd_name, parents=[common], help=f"{cmd_name} for all images")
        p.add_argument("--dry-run", action="store_true")
        if cmd_name == "manifest-all":
            p.add_argument("--release", action="store_true")

    # --- QEMU commands ---
    for cmd_name in ("qemu-prepare", "qemu-build-tools"):
        p = sub.add_parser(cmd_name, parents=[common], help=cmd_name)
        p.add_argument("--dry-run", action="store_true")

    for cmd_name in ("qemu-build-root", "qemu-build", "qemu-publish", "qemu-manifest"):
        p = sub.add_parser(cmd_name, parents=[common], help=cmd_name)
        p.add_argument("branch", help="QEMU branch")
        p.add_argument("--dry-run", action="store_true")

    for cmd_name in ("qemu-build-all", "qemu-publish-all"):
        p = sub.add_parser(cmd_name, parents=[common], help=cmd_name)
        p.add_argument("--dry-run", action="store_true")

    # --- Info commands ---
    p = sub.add_parser("ci-matrix", parents=[common], help="Output GitHub Actions matrix JSON")
    p.add_argument("--pretty", action="store_true")

    p = sub.add_parser("qemu-ci-matrix", parents=[common], help="Output QEMU tier JSON")
    p.add_argument("--pretty", action="store_true")

    sub.add_parser("list", parents=[common], help="List images")

    p = sub.add_parser("deps", parents=[common], help="Show dependency chain")
    p.add_argument("branch", help="Image branch")

    args = parser.parse_args()
    project_root = Path(args.project_root).resolve()
    project_config = load_project_config(project_root)

    # Discover all images
    all_images = discover_images(project_root)
    docker_images = filter_images_by_type(all_images, "docker")
    lineup_images = filter_images_by_lineup(docker_images, project_config, args.lineup)

    # --- Dispatch ---

    if args.command == "discover":
        print(f"Project: {project_config['registry']}/{project_config['author']}/{project_config['name']}")
        print(f"Discovered {len(docker_images)} docker images, {len(filter_images_by_type(all_images, 'qemu'))} qemu images\n")
        tiers = resolve_dag(docker_images)
        for i, tier in enumerate(tiers):
            print(f"  Tier {i}: {', '.join(tier)}")
        qemu_imgs = filter_images_by_type(all_images, "qemu")
        if qemu_imgs:
            print(f"\n  QEMU: {', '.join(img['branch'] for img in qemu_imgs)}")

    # Docker single-image
    elif args.command == "build":
        img = get_image_by_branch(lineup_images, args.branch)
        if not img:
            sys.exit(f"Error: '{args.branch}' not found in lineup '{args.lineup}'")
        run_build(project_config, img, args.lineup, project_root,
                  dry_run=args.dry_run, arch=getattr(args, "arch", None))

    elif args.command == "buildx":
        img = get_image_by_branch(lineup_images, args.branch)
        if not img:
            sys.exit(f"Error: '{args.branch}' not found in lineup '{args.lineup}'")
        run_buildx(project_config, img, args.lineup, project_root,
                   push=False, dry_run=args.dry_run)

    elif args.command == "publishx":
        img = get_image_by_branch(lineup_images, args.branch)
        if not img:
            sys.exit(f"Error: '{args.branch}' not found in lineup '{args.lineup}'")
        run_buildx(project_config, img, args.lineup, project_root,
                   push=True, release=args.release, dry_run=args.dry_run)

    elif args.command == "publish":
        img = get_image_by_branch(lineup_images, args.branch)
        if not img:
            sys.exit(f"Error: '{args.branch}' not found in lineup '{args.lineup}'")
        run_publish(project_config, img, args.lineup, project_root,
                    dry_run=args.dry_run, arch=getattr(args, "arch", None))

    # Docker batch
    elif args.command == "build-all":
        arch = getattr(args, "arch", None)
        run_batch(project_config, lineup_images, args.lineup, project_root,
                  lambda pc, img, ln, pr, dry_run=False: run_build(pc, img, ln, pr, dry_run=dry_run, arch=arch),
                  parallel=args.parallel, continue_on_error=args.continue_on_error,
                  dry_run=args.dry_run)

    elif args.command == "buildx-all":
        run_batch(project_config, lineup_images, args.lineup, project_root,
                  lambda pc, img, ln, pr, dry_run=False: run_buildx(pc, img, ln, pr, push=False, dry_run=dry_run),
                  parallel=args.parallel, continue_on_error=args.continue_on_error,
                  dry_run=args.dry_run)

    elif args.command == "publishx-all":
        release = args.release
        run_batch(project_config, lineup_images, args.lineup, project_root,
                  lambda pc, img, ln, pr, dry_run=False: run_buildx(pc, img, ln, pr, push=True, release=release, dry_run=dry_run),
                  parallel=args.parallel, continue_on_error=args.continue_on_error,
                  dry_run=args.dry_run)

    elif args.command == "publish-all":
        arch = getattr(args, "arch", None)
        run_batch(project_config, lineup_images, args.lineup, project_root,
                  lambda pc, img, ln, pr, dry_run=False: run_publish(pc, img, ln, pr, dry_run=dry_run, arch=arch),
                  parallel=args.parallel, continue_on_error=args.continue_on_error,
                  dry_run=args.dry_run)

    # Stable tag
    elif args.command == "tag-stable":
        img = get_image_by_branch(lineup_images, args.branch)
        if not img:
            sys.exit(f"Error: '{args.branch}' not found in lineup '{args.lineup}'")
        run_tag_stable(project_config, img, args.lineup, project_root, dry_run=args.dry_run)

    # Manifest
    elif args.command == "manifest":
        img = get_image_by_branch(lineup_images, args.branch)
        if not img:
            sys.exit(f"Error: '{args.branch}' not found")
        run_manifest(project_config, img, args.lineup, project_root,
                     release=args.release, dry_run=args.dry_run)

    elif args.command == "manifest-all":
        for img in lineup_images:
            run_manifest(project_config, img, args.lineup, project_root,
                         release=args.release, dry_run=args.dry_run)

    elif args.command == "rmmanifest":
        img = get_image_by_branch(lineup_images, args.branch)
        if not img:
            sys.exit(f"Error: '{args.branch}' not found")
        run_rmmanifest(project_config, img, args.lineup, project_root, dry_run=args.dry_run)

    elif args.command == "rmmanifest-all":
        for img in lineup_images:
            run_rmmanifest(project_config, img, args.lineup, project_root, dry_run=args.dry_run)

    # QEMU
    elif args.command == "qemu-prepare":
        qemu_prepare(project_root, dry_run=args.dry_run)

    elif args.command == "qemu-build-tools":
        qemu_build_tools(project_root, dry_run=args.dry_run)

    elif args.command == "qemu-build-root":
        qemu_build_root(project_root, args.branch, dry_run=args.dry_run)

    elif args.command == "qemu-build":
        qemu_build(project_root, args.branch, dry_run=args.dry_run)

    elif args.command == "qemu-publish":
        qemu_publish(project_root, args.branch, dry_run=args.dry_run)

    elif args.command == "qemu-manifest":
        qemu_manifest(project_root, args.branch, dry_run=args.dry_run)

    elif args.command == "qemu-build-all":
        qemu_build_all(all_images, project_root, publish=False, dry_run=args.dry_run)

    elif args.command == "qemu-publish-all":
        qemu_build_all(all_images, project_root, publish=True, dry_run=args.dry_run)

    # Info
    elif args.command == "ci-matrix":
        result = ci_matrix(lineup_images, project_config, args.lineup)
        indent = 2 if args.pretty else None
        print(json.dumps(result, indent=indent))

    elif args.command == "qemu-ci-matrix":
        result = qemu_ci_matrix(all_images)
        indent = 2 if args.pretty else None
        print(json.dumps(result, indent=indent))

    elif args.command == "list":
        tiers = resolve_dag(lineup_images)
        for i, tier in enumerate(tiers):
            print(f"Tier {i}: {', '.join(tier)}")

    elif args.command == "deps":
        chain = get_dep_chain(all_images, args.branch)
        print(" -> ".join(chain))


if __name__ == "__main__":
    main()
