from __future__ import annotations

import base64
import struct
from pathlib import Path

try:
    import patch_render as _patch_render

    _USE_EXTENSION = True
except ImportError:
    _USE_EXTENSION = False

from ...config.schema import CLAPSourceConfig
from ._base import PluginAdapterBase


def _strip_fxp_header(data: bytes) -> bytes:
    # VST2 FXP preset-with-chunk: "CcnK" + ... + "FPCh" at offset 8.
    # CLAP state->load() wants only the inner chunk, not the FXP wrapper.
    if len(data) >= 60 and data[:4] == b"CcnK" and data[8:12] == b"FPCh":
        chunk_size = struct.unpack(">I", data[56:60])[0]
        return data[60 : 60 + chunk_size]
    return data


class CLAPAdapter(PluginAdapterBase[CLAPSourceConfig]):
    def __init__(
        self,
        config: CLAPSourceConfig,
        state_map: dict[str, CLAPSourceConfig] | None = None,
    ):
        if state_map is None and not ((config.raw_state or config.preset_path) and config.preset):
            raise ValueError(
                "CLAPAdapter requires either state_map or a config with preset and raw_state/preset_path"
            )
        super().__init__(config, state_map)

    def _single_preset_state_map(self, config: CLAPSourceConfig) -> dict[str, CLAPSourceConfig]:
        return {config.preset: config}

    def _raw_state_for(self, config: CLAPSourceConfig) -> str:
        """Return base64 state string: from raw_state directly, or encoded from preset_path."""
        if config.raw_state:
            return config.raw_state
        if config.preset_path:
            data = _strip_fxp_header(Path(config.preset_path).read_bytes())
            return base64.b64encode(data).decode("ascii")
        return ""

    def _render_payload_extra(self) -> dict:
        return {"plugin_id": self._config.plugin_id}

    def _invoke_render(self, payload: dict, output_path: str) -> None:
        if _USE_EXTENSION:
            _patch_render.render_clap(payload)
        else:
            raise RuntimeError("patch_render extension not found; install patch-render to use CLAP sources")

    def _extra_source_metadata(self) -> dict:
        return {"plugin_id": self._config.plugin_id}
