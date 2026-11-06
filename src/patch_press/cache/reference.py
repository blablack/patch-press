"""The shared reference render: one note every plugin preset is rendered at, always.

patch-ear characterises a preset from one note (4 s hold, then a tail). A build's own
renders use the config's hold/velocities, and note-off is part of the render cache key, so
they can't serve patch-ear. Rather than have patch-ear render every plugin preset cold, the
build renders this note as well (cheap) and patch-ear's lookup then hits. Both sides import
these constants so they can't drift apart.
"""

NOTE = 45
VELOCITY = 100
HOLD_S = 4.0
TOTAL_S = 5.5
