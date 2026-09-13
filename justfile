# patch-press dev commands. Run `just` to list them.
# See CLAUDE.md "Development" for the full rationale behind each one.

set shell := ["bash", "-uc"]

default:
    @just --list

# Install/refresh .venv from uv.lock (editable package + dev group: ruff, matplotlib)
sync:
    uv sync --all-groups

# Re-resolve uv.lock against current pyproject.toml constraints (no version bumps unless
# a constraint changed — uv keeps existing resolutions where still valid)
lock:
    uv lock

# Upgrade all deps to their latest allowed versions and update uv.lock
# e.g.: just upgrade            (everything)
#       just upgrade tqdm       (just one package)
upgrade *pkg:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ -z "{{pkg}}" ]; then
        uv lock --upgrade
    else
        uv lock --upgrade-package {{pkg}}
    fi

# Lint / format (ruff, configured in pyproject.toml: line-length 128, black-compatible)
lint:
    uv run ruff check src

fmt:
    uv run ruff format src

# patch-press CLI passthrough, e.g.:
#   just run scan-clap Diva.clap presets/ configs/
#   just run sample configs/foo.yaml -- --format bento
run *args:
    uv run patch-press {{args}}

# Fast "did I break something" check: one preset per source method into output/Smoke/
smoke:
    uv run python debug_scripts/build_smoke.py

# Real orchestrator over everything in input/: plan|scan|build|all|status|redo <name>
# e.g.: just presets scan Diva
#       just presets all --format deluge,bento
presets *args:
    uv run python debug_scripts/build_presets.py {{args}}

# Render an HTML page to judge a loop by ear/eye (Deluge XML+note, or raw WAV+loop points)
audition *args:
    uv run python debug_scripts/audition_preset.py {{args}}

# Bisect a Bento card that reboots while browsing (moves patch folders into/out of _HOLD/)
bisect *args:
    uv run python debug_scripts/bento_bisect.py {{args}}

# Dump a Markdown table of every generated config, for review
inventory *args:
    uv run python debug_scripts/preset_inventory.py {{args}}
