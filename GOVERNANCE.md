# Governance

**Project owner and initial maintainer:** Mehdi Shams ([@mshamsk138](https://github.com/mshamsk138)).
The contact identifier provided by the owner is `m.shams3141`; it is **not a complete email address**.
A working private conduct contact must be confirmed before the repository is made public.

Until a fuller model is adopted:

* **Maintainers** review and merge pull requests, cut releases and handle security reports. The list of
  initial maintainer is **Mehdi Shams (@mshamsk138)**; additional maintainers will be listed here when appointed.
* **Decisions** are made by consensus of the maintainers in the issue or pull request concerned. When
  there is no consensus, the project owner decides and writes down why.
* **Safety-relevant changes** (order state machine, risk checks, clientOrderId handling, ledger,
  reconciliation, cloud protocol, STOP / kill switch) need a regression test and the review of a
  maintainer who did not write the change. Existing safety limits are never weakened silently: a change
  that relaxes one needs an issue that explains why, and a CHANGELOG entry under "Breaking" or
  "Security".
* **Releases** follow [docs/PUBLISHING_CHECKLIST.md](docs/PUBLISHING_CHECKLIST.md). A published tag is
  never moved or rewritten; a bad release is superseded by a new version.
* **License**: MIT. A change of license needs the explicit agreement of the copyright holders.
* **Code of conduct**: [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
