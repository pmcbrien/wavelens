# Third-party code used by the Wavelens Scanner

Copies of the outside source the Pi installers build, pinned to the versions the Scanner is tested with, so an
install never depends on another project's repository being up or unchanged. Each folder keeps its project's
own licence and copyright files; `.vendored-commit` is the upstream commit it was copied from.

| Folder | What it is | Upstream | Licence |
|---|---|---|---|
| `mbelib` | Voice codecs for digital voice (branch `ambe_tones`) | https://github.com/lwvmobile/mbelib | ISC; patent notice in its README |
| `dsd-fme` | Digital voice decoder: DMR, P25 phase 1, NXDN, D-STAR | https://github.com/lwvmobile/dsd-fme | ISC, with some GPL files (see its COPYRIGHT) |
| `rtl-sdr-blog` | Driver for the RTL-SDR Blog V4 dongle | https://github.com/rtlsdrblog/rtl-sdr-blog | GPL-2.0 (COPYING) |
| `bluezero` | Python Bluetooth library the bridge uses (source release 0.9.1) | https://pypi.org/project/bluezero/ | MIT (LICENSE in the archive) |

The P25 trunking decoder, APCO25, is in `companion/apco25` with its own LICENSE and NOTICE.md.

Copied 22 November 2026, unmodified. To update one: replace its folder with the new upstream source, write the
commit in `.vendored-commit`, and test a fresh install before release.

Voice codecs in mbelib, dsd-fme and APCO25 are covered by patents in some countries; the installers ask before
building them.
