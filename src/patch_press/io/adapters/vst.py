from __future__ import annotations

import json
import subprocess

try:
    import patch_render as _patch_render

    _USE_EXTENSION = True
except ImportError:
    _USE_EXTENSION = False

from ...config.schema import VSTSourceConfig
from ._base import PluginAdapterBase


class VSTAdapter(PluginAdapterBase[VSTSourceConfig]):
    def __init__(
        self,
        config: VSTSourceConfig,
        state_map: dict[str, VSTSourceConfig] | None = None,
    ):
        if state_map is None and not (config.raw_state and config.preset):
            raise ValueError("VSTAdapter requires either state_map or a config with both raw_state and preset")
        super().__init__(config, state_map)

    def _single_preset_state_map(self, config: VSTSourceConfig) -> dict[str, VSTSourceConfig]:
        return {config.preset: config}

    def _raw_state_for(self, config: VSTSourceConfig) -> str:
        return config.raw_state or ""

    def _invoke_render(self, payload: dict, output_path: str) -> None:
        if _USE_EXTENSION:
            _patch_render.render(payload)
        else:
            result = subprocess.run(
                ["patch-render"],
                input=json.dumps(payload),
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise RuntimeError(f"patch-render failed (exit {result.returncode}): {result.stderr}")
