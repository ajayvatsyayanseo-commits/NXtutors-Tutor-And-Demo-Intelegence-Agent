"""Pure domain logic: normalisation, parsing, distance, identity.

Everything here is deterministic, dependency-free and side-effect-free. No I/O,
no configuration lookups, no clock reads that are not passed in. That is what
makes the matching layer testable without a database.
"""
