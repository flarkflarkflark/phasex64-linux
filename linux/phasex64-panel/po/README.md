# Translating phasex64-panel

The panel UI is translated via Python's `gettext`, domain `phasex64-panel`.
English is the source language (every `_("...")` call in `phasex64-panel`
is an English string) and the fallback when no translation is available or
the system locale doesn't match one.

**Translation boundary** (see the i18n note at the top of `phasex64-panel`
for the full rationale): visible UI labels, status words, hints, dialog
text and tooltips are translated. ALSA control names (`"In 1/2 Capture
Source"`, `RIAA_NAMES`, `PEAK_NAMES`, `CLOCK`, `LOCK`, ...) and
`ADC_SOURCE_ITEMS`' enum item identifiers (`"Phono"`, `"Aux"`,
`"Mic-HiZ-Line"`, ...) are API strings sent to `amixer` by name, or
internal identity keys - never translated, and never wrapped in `_()`.
Built-in presets keep a fixed internal id (`"vinyl"`, `"monitor"` -
`BUILTIN_PRESET_IDS`); only their *displayed* label
(`builtin_preset_label()`) is translated, so a saved reference to a
built-in preset, or the "is this name a built-in" collision check, never
depends on the current locale.

## Updating the template (`.pot`)

Whenever `_("...")`-wrapped strings in `phasex64-panel` change:

```sh
cd linux/phasex64-panel
xgettext --language=Python --from-code=UTF-8 --keyword=_ \
  --package-name=phasex64-panel --package-version=0.9.2 \
  -o po/phasex64-panel.pot phasex64-panel
```

Then merge the updated template into each translation so existing work is
preserved and new strings show up untranslated (`msgmerge --update
po/nl.po po/phasex64-panel.pot`).

## Updating a translation

Edit `po/nl.po` directly (any PO editor works - Poedit, Lokalize, or a text
editor), or regenerate it from the template if it doesn't exist yet
(`msginit --input=po/phasex64-panel.pot --locale=nl --output=po/nl.po`).
Leave `msgstr ""` empty for anything genuinely untranslated (it falls back
to the English source) rather than guessing.

## Building a .mo for local testing

```sh
po/build.sh
```

Builds every `po/*.po` into `locale/<lang>/LC_MESSAGES/phasex64-panel.mo`
next to the script (gitignored - a build artifact, not source). The panel
looks there first, before `/usr/share/locale` (see
`phasex64-panel`'s `_locale_search_dirs()`), so this is enough to test a
translation without installing anything:

```sh
LANG=nl_NL.UTF-8 ./phasex64-panel --demo
```

The PKGBUILD builds and installs its own copy of every `.mo` under
`/usr/share/locale/<lang>/LC_MESSAGES/` at package-install time - `po/
build.sh` is for development only, never committed output.

## Adding a new language

1. `msginit --input=po/phasex64-panel.pot --locale=<ll> --output=po/<ll>.po`
2. Translate every `msgstr`.
3. `po/build.sh` and test with `LANG=<ll>_<CC>.UTF-8 ./phasex64-panel --demo`.
4. No other code change needed - the PKGBUILD picks up every `po/*.po` it
   finds (see `../PKGBUILD`).

## Testing completeness

`test_i18n.py` (same directory as `test_panel.py`) checks that regenerating
the `.pot` from the current source finds no `msgid` missing from the
committed one (i.e. the committed `.pot` is up to date), and that every
`msgid` in the `.pot` has a non-empty `msgstr` in `po/nl.po` (no silently
untranslated strings). Run it the same way as `test_panel.py`:

```sh
python3 test_i18n.py   # run from linux/phasex64-panel/, same as test_panel.py
```
