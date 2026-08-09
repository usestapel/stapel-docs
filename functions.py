"""comm surface of stapel-docs.

Every Function/Action carries a JSON schema in ``schemas/`` — tests run
with ``VALIDATE_SCHEMAS`` on, so a payload drifting from its schema fails
loudly. Registration happens on import from ``apps.py:ready()``; re-imports
are no-ops.

Provided: ``docs.create_document`` (the ingest seam — ironmemo dumps
transcripts/summaries through it). Emitted actions live in ``events.py``.
"""
