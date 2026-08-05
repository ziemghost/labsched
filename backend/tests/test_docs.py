"""The taxonomy generator, which broke once on a catalog row that grew a
column and stayed broken because nothing ran it."""
from __future__ import annotations

from labsched import docs


def test_the_generator_runs_at_all():
    out = docs.taxonomy_markdown()
    assert "| Operation | Capability |" in out
    assert "`liquid_transfer`" in out


def test_every_operation_appears_in_the_table():
    out = docs.taxonomy_markdown()
    for op in docs.DEFAULT_OPERATIONS:
        assert f"`{op[0]}`" in out
