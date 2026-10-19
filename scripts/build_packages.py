#!/usr/bin/env python3
"""
Builds the deployment zips for SHIP's four Python Lambdas, from the repository alone.

    python scripts/build_packages.py                 # all four, into build/
    python scripts/build_packages.py relay gateway   # some of them

Each zip holds the function's third-party dependencies, installed for the Lambda runtime (Linux,
arm64, Python 3.12) whatever machine this runs on, and exactly the first-party files its handler
imports, found by following the imports from the handler. A module a handler needs can therefore
never be left out, which is how a stale package used to fail.

    relay      relay_lambda_handler.py          requirements-relay.txt
    webhook    lambda_handler.py (+ the fragment handler, and the Gate console templates)
                                                requirements-lambda.txt
    fragment   fragment_lambda_handler.py       requirements-lambda.txt
    gateway    device_gateway_handler.py        (boto3 only, which the runtime already has)

The Alexa skill is not built here: `ask deploy` packages it.
"""

from __future__ import annotations

import argparse
import ast
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"
PLATFORM = ["--platform", "manylinux2014_aarch64", "--platform", "manylinux_2_28_aarch64",
            "--python-version", "3.12", "--implementation", "cp"]

TARGETS = {
    "relay": {"entries": ["relay_lambda_handler.py"], "requirements": "requirements-relay.txt",
              "pure": ["pywebpush", "http-ece", "py-vapid"], "templates": False},
    "webhook": {"entries": ["lambda_handler.py", "fragment_lambda_handler.py"], "requirements": "requirements-lambda.txt",
                "pure": [], "templates": True},
    "fragment": {"entries": ["fragment_lambda_handler.py"], "requirements": "requirements-lambda.txt",
                 "pure": [], "templates": False},
    "gateway": {"entries": ["device_gateway_handler.py"], "requirements": None, "pure": [], "templates": False},
}


def first_party_closure(entries: list[str]) -> list[str]:
    """Every repository file reachable from the handlers by import, as paths relative to ROOT.
    Function-level imports count: they are found because the whole syntax tree is walked."""
    seen: set[str] = set()
    todo = list(entries)
    while todo:
        rel = todo.pop()
        if rel in seen:
            continue
        seen.add(rel)
        for module in _imports(ROOT / rel):
            todo.extend(f for f in _files_for(module) if f not in seen)
    return sorted(seen)


def _imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] == "src":
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            found.update(a.name for a in node.names if a.name.split(".")[0] == "src")
    return found


def _files_for(module: str) -> list[str]:
    base = module.replace(".", "/")
    files = []
    if (ROOT / f"{base}.py").exists():
        files.append(f"{base}.py")
    if (ROOT / base / "__init__.py").exists():
        files.append(f"{base}/__init__.py")
    parts = module.split(".")
    for i in range(1, len(parts)):                       # the package files above it
        init = "/".join(parts[:i]) + "/__init__.py"
        if (ROOT / init).exists():
            files.append(init)
    return files


def pip(*args: str) -> None:
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", *args], check=True)


def install_dependencies(target: dict, into: Path) -> None:
    requirements = target["requirements"]
    if not requirements:
        return
    lines = [ln.strip() for ln in (ROOT / requirements).read_text().splitlines()]
    pure = {p.lower() for p in target["pure"]}
    keep = [ln for ln in lines if ln and not ln.startswith("#") and ln.split("==")[0].split(">")[0].lower() not in pure]
    listing = into.parent / f"{into.name}-requirements.txt"
    listing.write_text("\n".join(keep) + "\n")
    # Compiled packages: wheels for the Lambda platform, whatever this machine is.
    pip("--target", str(into), "--upgrade", *PLATFORM, "--only-binary=:all:", "-r", str(listing))
    listing.unlink()
    # Pure-Python packages that publish no wheel (http-ece): the build runs on this machine, and
    # nothing in them is platform specific.
    if pure:
        wanted = [ln for ln in lines if ln and not ln.startswith("#") and ln.split("==")[0].split(">")[0].lower() in pure]
        pip("--target", str(into), "--no-deps", "--upgrade", *wanted, *[p for p in target["pure"] if not any(p in w for w in wanted)])


def build(name: str) -> Path:
    target = TARGETS[name]
    staging = BUILD / name
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    install_dependencies(target, staging)

    files = first_party_closure(target["entries"])
    if target["templates"]:
        files += [str(p.relative_to(ROOT)) for p in (ROOT / "src" / "dashboard_ui").rglob("*")
                  if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"]
    for rel in sorted(set(files)):
        dest = staging / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, dest)

    out = BUILD / f"{name}.zip"
    out.unlink(missing_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(staging.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                zf.write(path, path.relative_to(staging))
    shutil.rmtree(staging)
    print(f"{name:9} {out.stat().st_size / 1e6:6.1f} MB  {len(set(files))} first-party files  -> {out.relative_to(ROOT)}")
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("names", nargs="*", choices=[*TARGETS, []], default=list(TARGETS), help="which packages (default: all)")
    args = parser.parse_args(argv)
    BUILD.mkdir(exist_ok=True)
    for name in args.names or list(TARGETS):
        build(name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
