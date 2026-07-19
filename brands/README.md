# Brand assets

The integration's brand icon (device-page header + integration card). Since
**HA 2026.3** the PNGs in `custom_components/glowrium/brand/` are served locally —
no PR to `home-assistant/brands` needed. Requirements: PNG, square, 256×256 + 512×512.

## Current icon — official Glowrium app icon
`icon.png` / `icon@2x.png` are the official Glowrium app icon (blue **GLOWRIUM**
badge) taken from glowrium.com and upscaled to 256/512.

- `icon-source-144.png` — the untouched 144×144 source (the CDN's master size).

Refresh from the site:

```bash
curl -sSL "https://www.glowrium.com/cdn/shop/files/APP-ICON_144-144.png" \
  -o brands/icon-source-144.png
magick brands/icon-source-144.png -filter Lanczos -resize 256x256 brands/icon.png
magick brands/icon-source-144.png -filter Lanczos -resize 512x512 "brands/icon@2x.png"
cp brands/icon.png "brands/icon@2x.png" custom_components/glowrium/brand/
```

## backup/
The earlier hand-made "sprout under sun" icon (`icon.svg` source + rendered PNGs).
Restore it by copying `backup/icon.png` + `backup/icon@2x.png` back into
`custom_components/glowrium/brand/`.

Browsers cache brand images ~7 days — hard-refresh after changing.
