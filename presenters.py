"""Presenters for stapel-docs — the DTO-building layer (§55).

Presenter discipline (docs/pending/extensibility-presenters.md; enforced by
SWAP001/SWAP002 in `stapel-verify`): views NEVER instantiate a `dto.py`
dataclass directly — every DTO is built by a presenter resolved through
`get_presenter(KEY, default=...)`, so a host project can swap the
presentation of any endpoint via `STAPEL_SWAP` without forking this module.
Etalon: stapel_core/django/users/presenters.py.
"""
