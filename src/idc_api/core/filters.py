"""Compile structured cohort filters into a parameterized SQL WHERE clause.

Attribute *names* are validated against a fixed allow-list (they can't be parameterized, so
we whitelist + double-quote them); attribute *values* are always passed as bound parameters
— the OWASP-recommended primary defense for the inputs we control.
"""

from __future__ import annotations

from typing import Any

from . import schema
from .errors import InvalidQueryError
from .models import CohortFilters


def compile_filters(filters: CohortFilters) -> tuple[str, list[Any]]:
    """Return ``(where_sql, params)``. ``where_sql`` is ``TRUE`` when no filters are given."""
    clauses: list[str] = []
    params: list[Any] = []

    for attr, values in (filters.terms or {}).items():
        if attr not in schema.TERM_ATTRIBUTES:
            raise InvalidQueryError(
                f"Unknown or non-term filter attribute: {attr!r}. "
                "Use list_attributes to see valid attributes."
            )
        values = [v for v in (values or []) if v is not None]
        if not values:
            continue
        placeholders = ", ".join(["?"] * len(values))
        clauses.append(f'"{attr}" IN ({placeholders})')
        params.extend(values)

    for attr, rng in (filters.ranges or {}).items():
        if attr not in schema.RANGE_ATTRIBUTES:
            raise InvalidQueryError(
                f"Unknown or non-range filter attribute: {attr!r}. "
                "Use list_attributes to see valid attributes."
            )
        if rng.gte is not None:
            clauses.append(f'"{attr}" >= ?')
            params.append(rng.gte)
        if rng.lte is not None:
            clauses.append(f'"{attr}" <= ?')
            params.append(rng.lte)

    where = " AND ".join(clauses) if clauses else "TRUE"
    return where, params
