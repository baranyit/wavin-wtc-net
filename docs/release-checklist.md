# Release checklist (prepared, NOT executed)

Decision 18/21: documentation is ready, publication is postponed. Nothing below has been done.

Before tagging the first release (the version in `manifest.json`, see AGENTS.md section 14):

- [ ] Decide the cooling position: release as beta with cooling marked experimental (README and
      `supported-devices.md` already say so), or wait for the cooling-season read-only run.
- [ ] Confirm `manifest.json`, `pyproject.toml`, README status and the top CHANGELOG heading agree
      (a test enforces it), and the CHANGELOG heading changes from "unreleased" to the release date.
- [x] Brand icon: replaced on 2026-11-08 with an icon supplied by the maintainer
      (`custom_components/wavin_wtc_net/brand/`). The maintainer states that it is **not** the Wavin
      logo, only similar in style, so no Wavin trademark permission is needed. (Not independently
      verified; revisit if anyone objects.)
- [ ] CI green on `main` (ruff, mypy, pytest matrix, hassfest, HACS validation).
- [ ] README links work; the repo topics (`home-assistant`, `hacs`, `homeassistant-integration`,
      `modbus`, `wavin`) are set.
- [ ] Re-run the read-only hardware validation once on the exact release commit.
- [x] (passed 2026-11-09, `docs/hardware-debounce-write-test-2026-11-09.md`) Approved one-zone hardware check of the debounced immediate write (decision 22): Dolgozó, one wheel step and restore, confirm the write lands between two reads, no read follows it, and the next cycle confirms.
- [ ] Tag `v<version>` and create a GitHub release (pre-release) with the CHANGELOG entry.
- [ ] Optional: submit to the HACS default repository list after a stable period.

After release: watch issues for new installations and firmware; revisit decisions 6/11 (cooling)
in the cooling season.
