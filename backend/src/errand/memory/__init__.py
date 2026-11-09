"""Memory: the registry, the evidence rules and every mutation of a stored preference
(architecture 12, gate 2 decisions 1 to 14).

`service` is the only module that writes. Context building and the model gateway read
through `selection`, `mask`, `scrub` and `registry`, never through `service`.
"""
