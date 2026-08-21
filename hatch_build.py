from __future__ import annotations

import sys
import sysconfig

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict[str, object]) -> None:
        if sys.platform != "linux":
            raise RuntimeError("vegavisuals wheels can only be built for Linux")
        platform_tag = sysconfig.get_platform().replace("-", "_").replace(".", "_")
        build_data["tag"] = f"py3-none-{platform_tag}"
        build_data["pure_python"] = False
