#!/usr/bin/env python3
"""Resolve release signing requirements for GitHub Actions and local tests."""

from __future__ import annotations

import argparse
import os
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SigningPolicy:
    apple_required: bool
    windows_required: bool


def _boolean(value: str, name: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"", "false"}:
        return False
    if normalized == "true":
        return True
    raise ValueError(f"{name} must be true or false, received {value!r}")


def resolve_policy(
    *,
    ref_type: str,
    ref_name: str,
    signed_test: str = "false",
    apple_signed_test: str = "false",
    require_windows_signing: str = "false",
) -> SigningPolicy:
    force_all = _boolean(signed_test, "SIGNED_TEST")
    force_apple = _boolean(apple_signed_test, "APPLE_SIGNED_TEST")
    windows_policy = _boolean(require_windows_signing, "REQUIRE_WINDOWS_SIGNING")

    apple_required = force_all or force_apple
    windows_required = force_all or windows_policy
    if ref_type == "tag":
        match = re.match(r"^v([0-9]+)\.", ref_name)
        if not match:
            raise ValueError(f"cannot determine major version from tag {ref_name!r}")
        apple_required = True
        windows_required = windows_required or int(match.group(1)) >= 1

    return SigningPolicy(
        apple_required=apple_required,
        windows_required=windows_required,
    )


def _github_boolean(value: bool) -> str:
    return "true" if value else "false"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--github-summary", type=Path)
    args = parser.parse_args()

    policy = resolve_policy(
        ref_type=os.environ.get("GITHUB_REF_TYPE", ""),
        ref_name=os.environ.get("GITHUB_REF_NAME", ""),
        signed_test=os.environ.get("SIGNED_TEST", "false"),
        apple_signed_test=os.environ.get("APPLE_SIGNED_TEST", "false"),
        require_windows_signing=os.environ.get("REQUIRE_WINDOWS_SIGNING", "false"),
    )
    apple = _github_boolean(policy.apple_required)
    windows = _github_boolean(policy.windows_required)
    print(f"Apple signing required: {apple}")
    print(f"Windows signing required: {windows}")

    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as output:
            output.write(f"apple_signing_required={apple}\n")
            output.write(f"windows_signing_required={windows}\n")
    if args.github_summary:
        windows_status = (
            "Authenticode required" if policy.windows_required else "unsigned"
        )
        with args.github_summary.open("a", encoding="utf-8") as summary:
            summary.write("## Release signing policy\n\n")
            summary.write("| Platform | Candidate policy |\n")
            summary.write("|---|---|\n")
            summary.write(
                "| macOS | "
                + (
                    "Developer ID and notarization required"
                    if policy.apple_required
                    else "unsigned"
                )
                + " |\n"
            )
            summary.write(f"| Windows | {windows_status} |\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
