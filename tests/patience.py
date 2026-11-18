"""How long a test waits for something before it gives up on it."""

# Nothing waits this long unless what it waits for never happens: every wait is over as soon as it has. A few
# seconds weren't enough on a busy machine, where a test then failed with nothing wrong.
PATIENCE = 60.0
