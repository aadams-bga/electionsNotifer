# Design system — Illinois Answers / BGA aesthetic

How to make this app (and anything built alongside it) look like an Illinois Answers
Project product. Every value below was extracted from illinoisanswers.org's live
stylesheets (Newspack theme + site custom CSS + Adobe Fonts kit) in July 2026, so
these are the real brand tokens, not approximations.

The aesthetic in one sentence: **a flat, square, high-contrast news brand — navy and
gold on white, sans-serif UI with serif display headlines, uppercase labels, no
rounded corners, no shadows, no gradients.**

---

## 1. Color

Their theme defines exactly this palette (from `theme.json` presets plus custom CSS):

| Token | Hex | Role on illinoisanswers.org |
|---|---|---|
| `primary` (navy) | `#003282` | Main brand color: nav bars, link hovers, social buttons, section headers, solid backgrounds behind featured stories |
| `primary-variation` | `#000a5a` | Darker navy for hover/depth on primary |
| link navy | `#003a63` | Body-copy link color (`.entry-content a`) |
| `secondary` (gold) | `#fac346` | The accent: CTA/highlight buttons, button hover states, kickers on photos, small badges. Always paired with **black or #111 text**, never white |
| `secondary-variation` | `#d29b1e` | Darker gold (outline-button hover borders) |
| `dark-gray` (text) | `#111111` | Default text and nav-link color |
| meta gray | `#464b50` | Bylines, dates, subtitles, figcaptions |
| `medium-gray` | `#767676` | De-emphasized UI text |
| `light-gray` | `#eeeeee` / `#f3f5f6` | Subtle panel backgrounds |
| white | `#ffffff` | Default page background — the site is overwhelmingly white |

Rules of use:
- Navy is the workhorse; gold is the exclamation point. If everything is gold,
  nothing is.
- Gold **always carries dark text** (`#111` or black). White-on-gold fails contrast
  and is off-brand.
- Text is `#111`, not pure black, on white. Muted text is `#464b50`.
- No other hues. No greens, reds, purples except functional states (errors may use
  a plain red like `#c0392b`).

## 2. Typography

The real brand fonts (Adobe Fonts kit `use.typekit.net/hid5kve.css`, domain-locked
to illinoisanswers.org — **cannot be hotlinked from other domains**):

| Font | Class | Where they use it |
|---|---|---|
| **Halyard Display** | sans | ALL headings h1–h6, forms, nav, subtitles, UI labels |
| **Halyard Text** | sans | Body copy, homepage paragraphs, author bios |
| **IvyPresto Display** | serif | Big feature headlines only (story titles, tight `letter-spacing: -0.01em`–`-0.02em`, `line-height: 1`) |
| **IBM Plex Serif** | serif | Article excerpt paragraphs in story listings |

Free substitutes for domains without the Adobe license (what this app does):

| Brand font | Free stand-in (Google Fonts) |
|---|---|
| Halyard Display / Text | **Public Sans** (current app choice) — Libre Franklin also works |
| IvyPresto Display | DM Serif Display or Playfair Display |
| IBM Plex Serif | **IBM Plex Serif** (it's free — use the real thing) |

Hierarchy recipe:
- **UI and headings: the sans.** Weights 400 / 600 / 700 only; their body weight for
  meta text is light (300), but 400 is fine when 300 isn't loaded.
- **A serif display face is optional garnish** for one big headline per page at most
  (hero title), tight letter-spacing, `line-height: 1`. Never for UI.
- Their homepage type scale for story titles: 16 / 21 / 28 / 38 / 51 / 67 px.
  Theme sizes: small 16, normal 20, large 36, x-large 42.
- Body copy ~16–20px, `line-height` ~1.4–1.55.

### Uppercase label pattern (very load-bearing)

The most recognizable IAP texture is small uppercase labels with letter-spacing:

```css
/* kickers / category labels ("EDUCATION", "CPS BOARD") */
font-size: 14px; text-transform: uppercase; letter-spacing: 0.05em; font-weight: 400;

/* main nav links */
font-size: 16px; text-transform: uppercase; letter-spacing: 0.08em; color: #111;

/* buttons */
font-size: 16px; text-transform: uppercase; letter-spacing: 0.05em;
```

Use these for nav, table headers, section kickers, and buttons. Kickers are navy on
white, **gold when sitting on a photo or navy background**.

## 3. Shape and surface

- `border-radius: 0` on **everything** — buttons, inputs, chips, cards. The single
  exception on their site is circular social icons.
- Flat color only: no gradients, no drop shadows (their one shadow is on dropdown
  menus: `2px 2px 12px rgba(0,0,0,0.25)`).
- Hairline borders: `1px solid rgba(0,0,0,0.1)` (header bottom) or a light gray.
  Accents are chunkier: `2px solid #003282` (menu toggles, dropdown top edges),
  or a 3–4px gold bar as a section/header accent.
- Generous white space; the page is white with content doing the work.

## 4. Components

**Buttons** — square, uppercase, letter-spaced, roomy (`padding: 16px 24px`; this
app uses a slightly tighter 12px 24px). Two variants:
- *Primary/CTA*: gold background, `#111` text. Hover: darker gold `#d29b1e`
  (their site sometimes inverts: navy button that hovers to gold + black).
- *Secondary*: navy background, white text. Hover: darker navy `#000a5a`.
- Transitions: `150ms ease-in-out`, usually on background or opacity only.

**Nav / header** — dark navy bar (`#003a63`–`#003282` range), white text at ~80%
opacity, uppercase links, **gold on hover**, 3px gold bottom border on the bar.
A single "highlight" nav item (their Donate) gets the gold-bg/black-text treatment.

**Links** — navy (`#003a63` in running text), hover to `#003282`. Underlines OK in
prose; nav and headline links are not underlined.

**Meta text** (bylines, dates, hints) — `#464b50`, 14px, weight 300–400.

**Tables** (this app's addition, styled to match) — navy header row with white
uppercase letter-spaced text; hairline row borders; no zebra striping.

**Chips/tags** — navy background, white text, square corners.

**Panels/sections** — white with a 1px `#dce1e9`-ish border; a hero section earns a
4px gold left border. Forms drop the boxes entirely and let headings carry
structure.

## 5. Motion

Almost none. `150ms ease-in-out` color/opacity transitions on interactive elements.
No animated entrances, parallax, or movement.

## 6. Email adaptation (digests)

Email clients can't load webfonts reliably, so digests use:
- `font-family: Arial, Helvetica, sans-serif`, text `#111111`, `line-height: 1.5`,
  `max-width: 640px`
- Section headings (race names) in navy `#003282`
- Hierarchy via `<strong>` and spacing — **no bullets, no indents** (house rule)
- Links left default-blue or navy; one "View the filing" link per item

## 7. Where this lives in the app

- Tokens: `src/isbe_notifier/web/static/style.css` `:root` block
  (`--navy`, `--navy-dark`, `--navy-header`, `--gold`, `--gold-dark`, `--bg-subtle`,
  `--border`, `--text`, `--text-muted`)
- Font loading: Google Fonts `<link>` in `web/templates/base.html` (Public Sans
  400/600/700)
- Email styles: inline in `notify/digest.py` (`_render_section`, `build_digest`)
  and the HTML footer in `notify/emailer.py`

If the app ever gets added to BGA's Adobe Fonts web project, switch the base.html
link to the kit URL and change the `body` stack to
`"halyard-text", "Public Sans", …` with headings in `"halyard-display", …` —
everything else stays the same.

## 8. Do / don't

**Do**: white pages, navy structure, one gold accent per view, uppercase
letter-spaced labels, square corners, hairline borders, real white space.

**Don't**: rounded corners, shadows, gradients, white text on gold, more than two
typefaces per page, colored backgrounds behind body copy, decorative animation,
off-palette colors.
