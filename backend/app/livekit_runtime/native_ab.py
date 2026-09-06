"""Explicit, fail-closed isolation of provider-native turn handling for QA only."""

from collections.abc import Mapping
from typing import Any

NATIVE_AB_FLAG = "provider_native_turns_qa"


def native_ab_enabled(
    config: Mapping[str, Any], metadata: Mapping[str, Any], *, voice_runtime: str
) -> bool:
    if config.get(NATIVE_AB_FLAG) is not True:
        return False
    if (
        metadata.get("qa_ab_run") != "native-ab-20260906"
        or metadata.get("qa_ab_lane") != "B"
        or voice_runtime != "inworld_realtime"
        or config.get("inworld_single_pass") is not False
    ):
        raise ValueError("Provider-native turn comparison is restricted to the isolated B QA agent")
    return True
