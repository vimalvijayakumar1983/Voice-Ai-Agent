"""Run a bounded audio replay against an explicitly allowlisted QA voice agent."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from app.services.audio_replay_canary import (
    AudioReplayCanaryError,
    load_audio_replay_manifest,
    manifest_environment_names,
    run_live_audio_replay,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate a VAV audio-replay manifest. Live audio is published only with "
            "--confirm-live and an environment-supplied QA allowlist."
        )
    )
    parser.add_argument("manifest", type=Path, help="Path to a strict replay manifest JSON file")
    parser.add_argument(
        "--confirm-live",
        action="store_true",
        help="Explicitly authorize this one bounded replay against the allowlisted test target",
    )
    return parser


def _print_json(value: dict, *, stream=None) -> None:
    print(json.dumps(value, indent=2, sort_keys=True), file=stream or sys.stdout)


async def _run(arguments: argparse.Namespace) -> int:
    try:
        manifest = load_audio_replay_manifest(arguments.manifest)
        if not arguments.confirm_live:
            _print_json(
                {
                    "case_id": manifest.case_id,
                    "live_call_started": False,
                    "mode": "dry_run",
                    "required_environment_variables": manifest_environment_names(manifest),
                    "safety": {
                        "agent_allowlist_required": True,
                        "confirm_live_required": True,
                        "test_only": True,
                    },
                }
            )
            return 0
        report = await run_live_audio_replay(manifest)
        _print_json(report.public_dict())
        return 0 if report.passed else 1
    except AudioReplayCanaryError as exc:
        _print_json(
            {
                "error": type(exc).__name__,
                "message": str(exc),
                "passed": False,
            },
            stream=sys.stderr,
        )
        return 2
    except Exception:
        # Unexpected library/provider exceptions are deliberately not echoed:
        # their text can contain a URL, credential, transcript, or phone number.
        _print_json(
            {
                "error": "UnexpectedReplayFailure",
                "message": "Audio replay failed unexpectedly; inspect restricted service logs.",
                "passed": False,
            },
            stream=sys.stderr,
        )
        return 2


def main() -> int:
    return asyncio.run(_run(_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
