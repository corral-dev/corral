#!/usr/bin/env python3
"""Append missing release packages and checksum the published bytes, never rebuilds."""

import argparse
import hashlib
import json
import pathlib
import subprocess
import tempfile


def release_assets(repo: str, tag: str) -> dict[str, dict]:
    result = subprocess.run(
        ["gh", "api", f"repos/{repo}/releases/tags/{tag}"],
        check=True, capture_output=True, text=True,
    )
    return {asset["name"]: asset for asset in json.loads(result.stdout)["assets"]}


def publish(repo: str, tag: str, dist: pathlib.Path) -> None:
    assets = release_assets(repo, tag)
    packages = sorted(p for p in dist.iterdir() if p.suffix in {".whl", ".gz"})
    for package in packages:
        if package.name in assets:
            print(f"Preserving published package: {package.name}")
            continue
        result = subprocess.run(
            ["gh", "release", "upload", tag, str(package), "--repo", repo],
            capture_output=True, text=True,
        )
        if result.returncode:
            # Another publisher can win between listing and uploading. Never delete it.
            if package.name not in release_assets(repo, tag):
                raise RuntimeError(f"Upload failed for {package.name}: {result.stderr.strip()}")
            print(f"Preserving concurrently published package: {package.name}")
    assets = release_assets(repo, tag)
    with tempfile.TemporaryDirectory(prefix="corral-release-sums-") as tmp:
        root = pathlib.Path(tmp)
        lines = []
        for name, asset in sorted(assets.items()):
            if not name.endswith((".whl", ".tar.gz")):
                continue
            if pathlib.Path(name).name != name:
                raise RuntimeError(f"Invalid release asset name: {name}")
            subprocess.run(
                ["gh", "release", "download", tag, "--repo", repo,
                 "--pattern", name, "--dir", str(root)], check=True,
            )
            actual = hashlib.sha256((root / name).read_bytes()).hexdigest()
            expected = asset.get("digest")
            if expected and expected != f"sha256:{actual}":
                raise RuntimeError(f"Published asset digest mismatch: {name}")
            lines.append(f"{actual}  {name}")
        if not lines:
            raise RuntimeError("No published packages to checksum")
        sums = root / "SHA256SUMS"
        sums.write_text("\n".join(lines) + "\n", encoding="utf-8")
        subprocess.run(
            ["gh", "release", "upload", tag, str(sums), "--repo", repo, "--clobber"],
            check=True,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--dist", type=pathlib.Path, required=True)
    args = parser.parse_args()
    publish(args.repo, args.tag, args.dist)


if __name__ == "__main__":
    main()
