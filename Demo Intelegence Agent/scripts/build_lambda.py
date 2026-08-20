"""Build the Lambda deployment ZIP and enforce the direct-upload size limit.

Demo may not create an S3 bucket, so the package is uploaded straight to Lambda.
That puts a hard ceiling on it, and this script's real job is to **fail** when
the package outgrows the ceiling rather than quietly switching to S3.

The limit is AWS's documented direct-upload maximum for a zipped deployment
package: 50 MB. It is a constant here rather than a lookup because the build
must fail closed if it cannot be checked — a build that cannot confirm the
limit must not decide it is fine.

    python scripts/build_lambda.py                # build and check
    python scripts/build_lambda.py --check-only   # check an existing ZIP
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

#: AWS's direct-upload ceiling for a zipped deployment package, in bytes.
#: Exceeding it is a design failure (too many dependencies), not a reason to
#: introduce S3.
DIRECT_UPLOAD_LIMIT = 50 * 1024 * 1024

#: Warn well before the wall, so growth is noticed while it is still cheap.
WARN_AT = int(DIRECT_UPLOAD_LIMIT * 0.7)

#: Never shipped. `boto3` and `botocore` are already in the Lambda runtime and
#: are the single biggest avoidable contributor to package size.
EXCLUDE_PREFIXES = (
    "boto3",
    "botocore",
    "pip",
    "setuptools",
    "wheel",
    "pkg_resources",
)

EXCLUDE_PATTERNS = (
    "__pycache__",
    ".dist-info/RECORD",
    ".pyc",
    ".pyo",
    "tests/",
    ".pytest_cache",
)

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
PACKAGE = DIST / "demo_command_center.zip"
BUILD = DIST / "build"


def _should_include(relative: Path) -> bool:
    text = relative.as_posix()
    if any(text.startswith(prefix) for prefix in EXCLUDE_PREFIXES):
        return False
    return not any(pattern in text for pattern in EXCLUDE_PATTERNS)


def build() -> Path:
    """Assemble the ZIP: source, policies, and runtime dependencies."""
    if BUILD.exists():
        shutil.rmtree(BUILD)
    BUILD.mkdir(parents=True)
    DIST.mkdir(exist_ok=True)

    print("installing runtime dependencies ...")
    subprocess.run(  # noqa: S603 - fixed argument list, no shell
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--quiet",
            "--target",
            str(BUILD),
            # The project's own runtime dependencies only. Dev extras and the
            # `aws` extra (boto3) are deliberately excluded.
            "-r",
            str(ROOT / "requirements.lambda.txt"),
        ],
        check=True,
        cwd=ROOT,
    )

    shutil.copytree(ROOT / "src" / "demo_command_center", BUILD / "demo_command_center")
    # Policies ship inside the package: `bootstrap._policy_dir` resolves them
    # relative to the package root, because a Lambda's working directory is not
    # the project root.
    shutil.copytree(ROOT / "config", BUILD / "config")

    if PACKAGE.exists():
        PACKAGE.unlink()

    print("zipping ...")
    with zipfile.ZipFile(PACKAGE, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(BUILD.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(BUILD)
            if _should_include(relative):
                archive.write(path, relative.as_posix())

    return PACKAGE


def check(package: Path) -> int:
    """Enforce the ceiling. Returns a process exit code."""
    if not package.exists():
        print(f"FAIL: no package at {package}")
        return 1

    size = package.stat().st_size
    megabytes = size / 1024 / 1024
    limit_mb = DIRECT_UPLOAD_LIMIT / 1024 / 1024

    with zipfile.ZipFile(package) as archive:
        entries = len(archive.namelist())
        uncompressed = sum(info.file_size for info in archive.infolist())

    print(f"package     : {package}")
    print(f"size        : {megabytes:.2f} MB  (limit {limit_mb:.0f} MB, direct upload)")
    print(f"uncompressed: {uncompressed / 1024 / 1024:.2f} MB")
    print(f"entries     : {entries}")

    smuggled = _smuggled_dependencies(package)
    if smuggled:
        print(f"FAIL: excluded dependencies present in the package: {sorted(smuggled)}")
        return 1

    if size > DIRECT_UPLOAD_LIMIT:
        print(
            f"FAIL: {megabytes:.2f} MB exceeds the {limit_mb:.0f} MB direct-upload limit.\n"
            "      Do NOT switch to an S3 artifact bucket — Demo may not create one.\n"
            "      Remove a dependency, or move a rarely-used capability to its own package."
        )
        return 1

    if size > WARN_AT:
        print(f"WARN: {megabytes:.2f} MB is above {WARN_AT / 1024 / 1024:.0f} MB. Watch this.")

    print("OK: within the direct-upload limit.")
    return 0


def _smuggled_dependencies(package: Path) -> set[str]:
    with zipfile.ZipFile(package) as archive:
        names = archive.namelist()
    return {
        prefix
        for prefix in EXCLUDE_PREFIXES
        if any(name.startswith(f"{prefix}/") for name in names)
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build and size-check the Lambda package")
    parser.add_argument("--check-only", action="store_true", help="check an existing ZIP")
    args = parser.parse_args()

    package = PACKAGE if args.check_only else build()
    return check(package)


if __name__ == "__main__":
    sys.exit(main())
