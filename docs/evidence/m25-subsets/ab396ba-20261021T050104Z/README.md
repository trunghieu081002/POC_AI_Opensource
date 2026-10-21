Regression subset on ab396ba (seal-key change in deploy), `--scenarios "a2-promote-deploy-run late-deploy-failure
partial-deploy-failure correct-twice"`, profile core. **3/4 matched**; `a2-promote-deploy-run` raised before
running: it needs the bronze profile (SeaweedFS) - a wrong invocation, not a product result. leaks=0. Kept here so
the full 34/34 bronze evidence in `docs/evidence/m25-automated/` is not overwritten by a subset.
