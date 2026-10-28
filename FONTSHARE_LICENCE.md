# Fontshare fonts — licence note

This repository self-hosts the following Fontshare fonts as WOFF2 files under
`fastapi_app/static/fonts/`:

- **Cabinet Grotesk** ExtraBold (800) — `CabinetGrotesk-Extrabold.woff2`
- **Satoshi** Medium (500) — `Satoshi-Medium.woff2`

These fonts are free under the [Fontshare Font Licence (FFL)](https://www.fontshare.com/licenses/ffl)
from [Fontshare](https://www.fontshare.com/) (Indian Type Foundry).

We load them only via local `@font-face` rules. There are **no** runtime
requests to `fontshare.com`, `cdn.fontshare.com`, or any other font CDN.

Redistribution of the font files follows the Fontshare Font Licence terms.
