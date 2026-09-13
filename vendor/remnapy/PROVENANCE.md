# Vendored Remnapy wheel

- Upstream: `https://github.com/snoups/remnapy`
- Source commit: `06802538e9f7671d4387c597b5f1434557ca3dc9`
- Wheel version: `2.7.1.dev7+cleanpay.g06802538`
- Wheel SHA-256: `f293db6ad658b948cef792fe0afb9b4c06fdc4a2d533038f45897b24baa01e61`
- Upstream license: MIT; the exact upstream `LICENSE` is stored beside the wheel.

The Python package files in the wheel are byte-identical to the files at the
source commit. Only build metadata was changed: the dynamic SCM version was
replaced with the static wheel version above, Remnapy's `cryptography`
requirement was corrected to `>=50.0.0,<51.0.0`, and the build backend was
pinned to `setuptools==84.0.0`.

The wheel was built on Python 3.12 with `build==1.3.0`, `setuptools==84.0.0`,
and `wheel==0.48.0` using:

```text
SOURCE_DATE_EPOCH=1782840709
PYTHONHASHSEED=0
TZ=UTC
python -m build --wheel --no-isolation
```

Four independent builds produced the same SHA-256 listed above. The wheel has
91 entries and the universal `py3-none-any` tag.
