from pathlib import Path

import yaml

_PROFILES_DIR = Path(__file__).parent / "data"


def load_profile(name: str) -> dict:
    path = _PROFILES_DIR / f"{name}.yaml"
    if not path.exists():
        raise ValueError(f"Unknown profile: {name!r}. Available: {available_profiles()}")
    return yaml.safe_load(path.read_text())


def available_profiles() -> list[str]:
    return sorted(p.stem for p in _PROFILES_DIR.glob("*.yaml"))
