#!/bin/bash

set -e

PYTHON=.venv/bin/python

$PYTHON -m patch_press.cli --debug classify \
    "output/Configs/Dexed/*.yaml" \
    --workers 8 \
    --save-path output/Captured

