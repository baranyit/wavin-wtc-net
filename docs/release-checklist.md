# Release checklist (prepared, NOT executed)

Decision 18/21: documentation is ready, publication is postponed. Nothing below has been done.

Before tagging 0.1.0:

- [ ] Decide the cooling position: release as beta with cooling marked experimental (README and
      `supported-devices.md` already say so), or wait for the cooling-season read-only run.
- [ ] Confirm `manifest.json` version (0.1.0) and `pyproject.toml` version agree, and the
      CHANGELOG heading changes from "unreleased" to the release date.
- [ ] Brand icon: replaced on 2026-11-08 with the Wavin logo supplied by the maintainer
      (`custom_components/wavin_wtc_net/brand/`). Wavin trademark permission for a public
      repository is **not confirmed**: confirm it, or switch back to a neutral icon, before the
      release.
- [ ] CI green on `main` (ruff, mypy, pytest matrix, hassfest, HACS validation).
- [ ] README links work; the repo topics (`home-assistant`, `hacs`, `homeassistant-integration`,
      `modbus`, `wavin`) are set.
- [ ] Re-run the read-only hardware validation once on the exact release commit.
- [ ] Tag `v0.1.0` and create a GitHub release (pre-release) with the CHANGELOG entry.
- [ ] Optional: submit to the HACS default repository list after a stable period.

After release: watch issues for new installations and firmware; revisit decisions 6/11 (cooling)
in the cooling season.
