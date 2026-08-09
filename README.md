# stapel-docs

Documents: storage, revisions and per-type editors for the [Stapel framework](https://github.com/usestapel) —
composable Django apps that deploy as a monolith or as microservices
without changing module code.

## Install

```bash
pip install stapel-docs
```

```python
INSTALLED_APPS = [
    # ...
    "stapel_docs",
]

# urls.py
path("docs/", include("stapel_docs.urls"))
```

## Settings

All configuration lives in the `STAPEL_DOCS` namespace (dict setting,
flat setting, or env var — resolved lazily):

| Key | Default | Meaning |
|---|---|---|
| `GREETING` | `"pong"` | Scaffold example — replace. |

## comm surface

| Kind | Name | Contract |
|---|---|---|
| Function | `docs.ping` | `{}` -> `{"greeting": str}` |

## Extension points

See [MODULE.md](MODULE.md) — the agent-facing map of every fork-free seam
(settings, serializer seams, registries, comm surface).

## Development

```bash
pip install -e . && pip install pytest pytest-django ruff
./setup-hooks.sh
pytest tests/
```

## Checks

Install the pre-commit hooks once:

```bash
pip install pre-commit
pre-commit install
```

Every commit then runs `stapel-verify .` — R001-R008, SWAP001-004,
CFG000-005, URL001, ADO-codes, MIG-codes, DOC001. Run the full suite on
demand with `pre-commit run --all-files`.

## License

MIT
