# Fonts

`Montserrat-SemiBold-latin1.ttf` is a subset of Montserrat SemiBold
(https://github.com/JulietaUla/Montserrat, SIL Open Font License 1.1, see `OFL.txt`).
It is rendered on the device with LVGL's Tiny TTF engine so German umlauts work at any size.

Subset command (Latin-1 + a few symbols, keeps kerning):

```bash
uv run --no-project --with fonttools python -m fontTools.subset Montserrat-SemiBold.ttf \
  --unicodes="U+0020-007E,U+00A0-00FF,U+2013,U+2014,U+2022,U+2026,U+20AC,U+2190-2193" \
  --layout-features='kern' --output-file=Montserrat-SemiBold-latin1.ttf
```
