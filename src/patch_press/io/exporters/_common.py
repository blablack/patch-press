"""Path helpers shared by all exporters."""


def safe_component(name: str) -> str:
    """One path component with the SD-card path separators neutralised."""
    return name.strip().replace("/", "_").replace("\\", "_")


def subfolder_parts(subfolder: str) -> list[str]:
    """Sanitised components of an output subfolder tree (see OutputConfig.subfolder).

    Drops '', '.', '..' so a config-supplied subfolder can never climb out of the
    collection directory.
    """
    parts = []
    for comp in (subfolder or "").replace("\\", "/").split("/"):
        c = safe_component(comp)
        if c and c not in (".", ".."):
            parts.append(c)
    return parts
