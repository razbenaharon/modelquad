# Third-party dependencies

ModelQuad is distributed as source code only. It bundles no third-party code:
`pip` installs the packages below from their own distributions, under their
own licences, when you install ModelQuad. This list is for information.

**Engineering inventory, not legal advice.** Each row records the licence a
project declares in its own distribution metadata, as written. Whether a
particular use satisfies a licence is for you and your counsel to decide.

The list covers a source install with the runtime extra
(`pip install ".[service]"`): the direct dependencies in `pyproject.toml`
and everything they require at runtime, including the extras one dependency
requests of another (for example `mcp` requires `pyjwt[crypto]`, which brings
in `cryptography`). No dependency in it declares a
copyleft licence. Requirements are evaluated for the generating environment,
so a backport only older Python versions need is not listed; a row reading
`not installed here` is a requirement that applies but was not installed.

## How to regenerate

    python scripts/third_party_inventory.py --notice

Run it in an environment with the runtime extra installed, and replace the
table below with its output. Regenerated 2026-11-08 on Windows, Python 3.11.

## Python dependencies

| Package | Version | Declared licence | Depth | Present here |
|---|---|---|---|---|
| `annotated-doc` | 0.0.5 | MIT | transitive | yes |
| `annotated-types` | 0.8.0 | MIT | transitive | yes |
| `anyio` | 4.16.0 | MIT | direct | yes |
| `attrs` | 26.1.0 | MIT | transitive | yes |
| `cffi` | 2.1.1 | MIT-0 | transitive | yes |
| `click` | 8.5.1 | BSD-3-Clause | transitive | yes |
| `cryptography` | 50.0.2 | Apache-2.0 OR BSD-3-Clause | transitive | yes |
| `fastapi` | 0.141.1 | MIT | direct | yes |
| `h11` | 0.16.0 | MIT | transitive | yes |
| `httpcore2` | 2.16.0 | BSD-3-Clause | transitive | yes |
| `httpx2` | 2.16.0 | BSD-3-Clause | transitive | yes |
| `idna` | 3.20 | BSD-3-Clause | transitive | yes |
| `jsonschema` | 4.26.0 | MIT | transitive | yes |
| `jsonschema-specifications` | 2025.9.1 | MIT | transitive | yes |
| `mcp` | 2.3.0 | MIT | direct | yes |
| `mcp-types` | 2.3.0 | MIT | transitive | yes |
| `opentelemetry-api` | 1.45.2 | Apache-2.0 | transitive | yes |
| `pycparser` | 3.12 | BSD-3-Clause | transitive | yes |
| `pydantic` | 2.14.3 | MIT | direct | yes |
| `pydantic_core` | 2.50.4 | MIT | transitive | yes |
| `PyJWT` | 2.15.1 | MIT | transitive | yes |
| `python-multipart` | 0.0.33 | Apache-2.0 | transitive | yes |
| `pywin32` | 312 | PSF | transitive | yes |
| `PyYAML` | 6.0.3 | MIT | direct | yes |
| `referencing` | 0.37.0 | MIT | transitive | yes |
| `rpds-py` | 2026.9.1 | MIT | transitive | yes |
| `sse-starlette` | 3.7.0 | BSD-3-Clause | transitive | yes |
| `starlette` | 1.3.1 | BSD-3-Clause | direct | yes |
| `truststore` | 0.10.5 | MIT | transitive | yes |
| `typing_extensions` | 4.16.0 | PSF-2.0 | transitive | yes |
| `typing-inspection` | 0.4.5 | MIT | transitive | yes |
| `uvicorn` | 0.52.4 | BSD-3-Clause | direct | yes |
