# Themes

The dashboard includes all 22 stock themes in the latest released Omarchy version,
[v4.0.4](https://github.com/omacom/omarchy/releases/tag/v4.0.4), checked on October 11, 2026,
plus the ChatGPT-inspired light theme and a T3-inspired dark theme. Gruvbox remains the default.

The list and palettes come from the release's
[`themes/` directory](https://github.com/omacom/omarchy/tree/c668141e9c42b13c80c9ca4ea108e11708c5e8a5/themes),
commit `c668141e9c42b13c80c9ca4ea108e11708c5e8a5`. Each theme's `colors.toml`
defines its colors and light/dark mode. This uses the released set; the development
branch's additional Starship theme is not part of v4.0.4.

| Theme | Appearance | Official palette |
| --- | --- | --- |
| Gruvbox | Dark | [colors.toml](https://github.com/omacom/omarchy/blob/c668141e9c42b13c80c9ca4ea108e11708c5e8a5/themes/gruvbox/colors.toml) |
| Catppuccin Latte | Light | [colors.toml](https://github.com/omacom/omarchy/blob/c668141e9c42b13c80c9ca4ea108e11708c5e8a5/themes/catppuccin-latte/colors.toml) |
| Catppuccin | Dark | [colors.toml](https://github.com/omacom/omarchy/blob/c668141e9c42b13c80c9ca4ea108e11708c5e8a5/themes/catppuccin/colors.toml) |
| Ethereal | Dark | [colors.toml](https://github.com/omacom/omarchy/blob/c668141e9c42b13c80c9ca4ea108e11708c5e8a5/themes/ethereal/colors.toml) |
| Everforest | Dark | [colors.toml](https://github.com/omacom/omarchy/blob/c668141e9c42b13c80c9ca4ea108e11708c5e8a5/themes/everforest/colors.toml) |
| Flexoki Light | Light | [colors.toml](https://github.com/omacom/omarchy/blob/c668141e9c42b13c80c9ca4ea108e11708c5e8a5/themes/flexoki-light/colors.toml) |
| Hackerman | Dark | [colors.toml](https://github.com/omacom/omarchy/blob/c668141e9c42b13c80c9ca4ea108e11708c5e8a5/themes/hackerman/colors.toml) |
| Kanagawa | Dark | [colors.toml](https://github.com/omacom/omarchy/blob/c668141e9c42b13c80c9ca4ea108e11708c5e8a5/themes/kanagawa/colors.toml) |
| Last Horizon | Dark | [colors.toml](https://github.com/omacom/omarchy/blob/c668141e9c42b13c80c9ca4ea108e11708c5e8a5/themes/last-horizon/colors.toml) |
| Lumon | Dark | [colors.toml](https://github.com/omacom/omarchy/blob/c668141e9c42b13c80c9ca4ea108e11708c5e8a5/themes/lumon/colors.toml) |
| Lupine | Light | [colors.toml](https://github.com/omacom/omarchy/blob/c668141e9c42b13c80c9ca4ea108e11708c5e8a5/themes/lupine/colors.toml) |
| Matte Black | Dark | [colors.toml](https://github.com/omacom/omarchy/blob/c668141e9c42b13c80c9ca4ea108e11708c5e8a5/themes/matte-black/colors.toml) |
| Miasma | Dark | [colors.toml](https://github.com/omacom/omarchy/blob/c668141e9c42b13c80c9ca4ea108e11708c5e8a5/themes/miasma/colors.toml) |
| Nord | Dark | [colors.toml](https://github.com/omacom/omarchy/blob/c668141e9c42b13c80c9ca4ea108e11708c5e8a5/themes/nord/colors.toml) |
| Osaka Jade | Dark | [colors.toml](https://github.com/omacom/omarchy/blob/c668141e9c42b13c80c9ca4ea108e11708c5e8a5/themes/osaka-jade/colors.toml) |
| Retro 82 | Dark | [colors.toml](https://github.com/omacom/omarchy/blob/c668141e9c42b13c80c9ca4ea108e11708c5e8a5/themes/retro-82/colors.toml) |
| Ristretto | Dark | [colors.toml](https://github.com/omacom/omarchy/blob/c668141e9c42b13c80c9ca4ea108e11708c5e8a5/themes/ristretto/colors.toml) |
| Rose Pine | Light | [colors.toml](https://github.com/omacom/omarchy/blob/c668141e9c42b13c80c9ca4ea108e11708c5e8a5/themes/rose-pine/colors.toml) |
| Solitude | Dark | [colors.toml](https://github.com/omacom/omarchy/blob/c668141e9c42b13c80c9ca4ea108e11708c5e8a5/themes/solitude/colors.toml) |
| Tokyo Night | Dark | [colors.toml](https://github.com/omacom/omarchy/blob/c668141e9c42b13c80c9ca4ea108e11708c5e8a5/themes/tokyo-night/colors.toml) |
| Vantablack | Dark | [colors.toml](https://github.com/omacom/omarchy/blob/c668141e9c42b13c80c9ca4ea108e11708c5e8a5/themes/vantablack/colors.toml) |
| White | Light | [colors.toml](https://github.com/omacom/omarchy/blob/c668141e9c42b13c80c9ca4ea108e11708c5e8a5/themes/white/colors.toml) |

These are dashboard adaptations of the color palettes, without desktop wallpapers
or application-specific configuration. The existing Gruvbox, Flexoki Light, and
Retro 82 adaptations are preserved. The added themes use the official background,
foreground, accent, selection, and surface colors. Small accent, secondary, warning,
and error text blends toward white or black where needed to reach 4.5:1 contrast
against dashboard surfaces. Pale accent backgrounds use a 12% tint. White uses its
`dark_background` for cards rather than its selection gray. Rose Pine is the light
palette shipped by Omarchy.

The theme catalog in `static/index.html` supplies both the picker options and saved
preference validation. The browser theme-color comes from the active CSS page color.
Switching themes recolors the allowance chart as well as the dashboard, and the
selected theme is restored before the page is first painted. Unrecognized saved
values fall back to Gruvbox; unavailable browser storage does not prevent switching.
