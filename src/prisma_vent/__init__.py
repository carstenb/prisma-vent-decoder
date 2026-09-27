"""Unofficial decoder for personal prisma VENT50 SD-card data.

Not medical software. See the README: events are reported by numeric id and
are never named, and no clinical index is derived from them.
"""

#: Kept as a literal rather than read from installed metadata, so that running
#: from a source checkout — which the test suite does, deliberately — reports
#: the same version as an installed wheel instead of whatever happens to be in
#: the environment.
#:
#: **It is duplicated from pyproject.toml, and that duplication is checked.**
#: The 0.1.0 release was almost cut with the two disagreeing: the artefact was
#: named 0.1.0 while this said 0.1.0.dev0, and since this value is what lands
#: in ``adapter.package_version``, every export and every copy manifest would
#: have recorded a version that did not exist. A decoder whose whole claim is
#: traceable provenance cannot be vague about which build produced a reading.
#: ``test_the_version_matches_pyproject`` fails if the two drift again.
__version__ = "0.1.0"

# Bumped when the meaning of decoded output changes, independently of the
# package version. Provenance records both.
DECODER_SCHEMA_VERSION = 1
