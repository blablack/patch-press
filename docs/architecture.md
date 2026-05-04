# Architecture

## Pipeline

```
YAML config → load_config() → [VSTAdapter | LibraryAdapter]
                                        ↓
                              analyze_sampleset()
                              (trim, envelope, pitch*, loop, normalize)
                                        ↓
                              [Exporter] → output files
```

*Pitch verification runs only for library sources, never for VST.

## Adding a New Output Format

Exporters are registered in `src/patch_press/runner/pipeline.py`:

```python
_EXPORTERS = {
    "deluge": DelugeExporter,
}
```

To add a new format (e.g. Bitwig):

1. Create `src/patch_press/io/exporters/bitwig.py` with a class implementing `export(sset: SampleSet, output: OutputConfig) -> Path`
2. Register it: `"bitwig": BitwigExporter` in `_EXPORTERS`
3. Use it in a config: `output.format: bitwig`

The `SampleSet` model is format-agnostic — it contains notes, audio buffers, and loop points only. Exporters are responsible for all format-specific serialisation.

## Adding a New Input Source

Adapters live in `src/patch_press/io/adapters/`. Each adapter implements `capture(...) -> SampleSet`. The pipeline selects the adapter based on the config source type (`VSTSourceConfig` vs `LibrarySourceConfig`). To add a new source, add a new config type and a corresponding branch in `runner/pipeline.py`.
