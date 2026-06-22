# UX Enhancement Plan — "The Gossip" 3× Trash Amplification (V2)
**STATUS: LOCKED — Ready for Implementation**
**Version: v2.0 · Date: 2026-06-22**
**Review Pipeline: Opus V1 Generate → Hermes Review → Opus Adversarial → Synthesis → Opus Rewrite → Dual Final Review → Lock**
**Cost: $2.01 total across 3 Opus sessions**

**Target:** Astro v6.4.8 site, 4 source files. Amplify the tabloid/gossip aesthetic **3×** without changing content data or the build.
**Grounded in the actual current code:** `BaseLayout.astro` (352 lines), `index.astro` (296 lines), `articles/[slug].astro` (506 lines), `disclosure.ts` (19 lines). Content collection holds **1 article, 5 `claim_evidence` entries, 5 `implications`** — every derived widget below is sized against those real counts.

---

## 1. CONCEPT & DIRECTION

**Style name: "WET CHROME TABLOID" — Frutiger Aero meets Industrial Bauhaus meets 2007 TMZ.**

The unified thesis: a glossy, wet, candy-glass interface (Frutiger Aero — think Windows Vista Aero, iPod nano gloss, tropical-sky gradients) violently bolted onto a brutal black-rule grid (Bauhaus industrial dividers, 4–6px black bars), then loaded with screaming trash-tabloid signifiers (TMZ red/yellow EXCLUSIVE stamps, Perez Hilton scribble energy, National Enquirer "YOU WON'T BELIEVE" clickbait, paparazzi camera-flash).

The current site is a *well-mannered tech blog wearing gossip clothes*. The fix is **density + gloss + aggression + raw mess**: heavier glass with specular highlights, thicker black structure, hotter accent colors, animated urgency (scrolling ticker, flashing BREAKING bar, pulsing live dot), trash UI furniture (scandal heat meters, EXCLUSIVE stamps, hot-or-not voting, "#7 WILL SHOCK YOU" numbering), **image energy** (hero photo frame, paparazzi grid), and at least one **deliberately ugly, hand-crude element** so the page doesn't read as too-tasteful "designer trash."

Real references to channel: **TMZ.com circa 2008** (red header, dense card grid, yellow flags), **Perez Hilton** (MS-Paint scribble crudeness, all-caps, crude stamps scrawled on photos), **The National Enquirer / Star** (gloss-stock cover, starburst flashes, blind items), **Frutiger Aero** (Vista glass, aqua buttons, lens-flare highlights).

**The 3× lever, concretely:** borders 2–3px → 5–6px; glass 82% flat → layered gloss with `::before` specular sheen; one accent red → red+yellow+hot-magenta+cyan-aqua system; static ticker → multi-item marquee animation; polite badges → rotated stamps + starbursts; clean single grid → multi-zone dense layout with a "BREAKING" hero band, a photo grid, and reaction widgets; zero images → a framed hero photo and a paparazzi wall.

**Guardrail:** the CASE FILE / evidence section is the brand's credibility engine ("every claim hash-locked, verify the receipts"). It gets *louder* (stamps, dossier vibe) but stays **legible and serious**, never cartoonish. Trash the chrome; respect the evidence.

---

## 2. DESIGN TOKENS (`:root` in `BaseLayout.astro`, lines 86–109)

Replace the current `:root` block. Keep existing variable **names** (referenced across all 3 files) and add new ones.

```css
:root {
  /* ── Aero background — hotter, more saturated tropical sky ── */
  --bg-top:    #7FC4E8;   /* was #B8D8E8 — deepen */
  --bg-mid:    #B6E0F0;   /* was #D4E8F2 */
  --bg-bot:    #E9F6FB;   /* was #E8EEF4 — keep light floor */
  --bg-aqua:   #00C2FF;   /* aqua accent for gloss/lens-flare */
  --bg-sky2:   #5AB0E0;   /* secondary sky band */

  /* ── Glass — wetter, layered ── */
  --surface:   rgba(255,255,255,0.72);
  --surface-2: rgba(255,255,255,0.42);
  --gloss:     linear-gradient(180deg, rgba(255,255,255,0.95) 0%, rgba(255,255,255,0.35) 48%, rgba(255,255,255,0.08) 49%, rgba(255,255,255,0.22) 100%);
  --blur:      22px;

  /* ── Ink ── */
  --ink:       #0A0A0A;
  --ink-soft:  #2E2E2E;
  --ink-faint: #6B6B6B;
  --line:      #C8C8C8;
  --line-dark: #000000;   /* pure black for industrial rules */

  /* ── Tabloid color system ── */
  --red:       #FF0033;   /* hotter TMZ red */
  --red-deep:  #C00018;
  --red-soft:  #FFE3E8;
  --yell:      #FFE600;   /* punchier */
  --yell-soft: #FFF7C2;
  --magenta:   #FF2D95;   /* hot-gossip pink */
  --aqua:      #00C2FF;   /* aero cyan */
  --lime:      #B6FF00;   /* rare "shock" accent */

  /* ── Structure / industrial ── */
  --rule:      6px;       /* master black-rule width */
  --rule-mid:  4px;       /* secondary rule */
  --radius:    0px;       /* hard-edged Bauhaus for structure */
  --radius-glass: 10px;   /* rounded only for aqua glass buttons */
  --maxw:      1100px;

  /* ── Shadows ── */
  --shadow-hard: 5px 5px 0 var(--line-dark);   /* brutalist offset */
  --shadow-glow: 0 0 0 3px var(--yell), 0 0 22px rgba(255,0,51,0.45);
  --shadow-soft: 0 8px 24px rgba(0,40,80,0.18);

  /* ── Type ── */
  --font:      'Inter', ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
  --mono:      'JetBrains Mono', ui-monospace, 'SF Mono', 'Cascadia Code', Menlo, Consolas, monospace;
  --display:   'Archivo Black', 'Inter', sans-serif;

  /* ── Motion ── */
  --tick-dur:  32s;
}
```

**New font load:** add to `<head>` (after line 18) — `Archivo Black` (free Google font, single 900 weight, perfect tabloid scream). Keep Inter/JetBrains Mono.
```html
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Archivo+Black&family=Inter:wght@400;600;700;800;900&family=JetBrains+Mono:wght@600;700;800&display=swap" rel="stylesheet">
```
(If Inter/JetBrains are already bundled, add only the `Archivo+Black` family to avoid double-loading.)

---

## 3. COMPONENT-BY-COMPONENT CHANGES

### 3.1 Header (`BaseLayout.astro` `.site-header`, lines 138–183)
- `.site-header` border-bottom `2px` → `var(--rule)` (6px) solid black. Add a red underline stripe via `box-shadow: 0 6px 0 0 var(--red)` for a black+red double rule.
- Keep frosted glass; add `--gloss` sheen via `.site-header::after` (top 50% white-to-transparent). Bump `blur(20px)` → `blur(var(--blur))`.
- Height `52px` → `58px` for a fatter brand.
- `.brand-mark` font-size `22px` → `26px`, `font-family: var(--display)`. `.brand .x` (the red "GOSSIP") gets `background: var(--red); color:#fff; padding:0 6px;` — a **red highlighter block**, not just red text.
- `.site-nav a` `11px` → `12px`, hover underline animation (`::after` width 0→100%). Add a fifth nav item **SCANDALS**; make the active/first one red.

**New element — LIVE pulse dot** next to the brand:
```html
<span class="live-dot" aria-label="Live feed"><i aria-hidden="true"></i>LIVE</span>
```
```css
.live-dot{display:inline-flex;align-items:center;gap:5px;font:800 11px/1 var(--font);color:var(--red);letter-spacing:.08em;}
.live-dot i{width:8px;height:8px;border-radius:50%;background:var(--red);animation:pulse 1.4s ease-in-out infinite;}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
```

**Visual goal:** a broadcast chyron — black rule, red highlight brand, blinking LIVE.

### 3.2 Ticker (`BaseLayout.astro` `.ticker`, lines 185–212)
Make it actually **scroll** AND carry real multi-item content. Hardcode **6–8 sensational items** directly in the template (the current single duplicated string breaks the loop and reads as a bug). Wrap items in a `.ticker-track` and **duplicate the entire item list exactly twice** for a seamless `translateX(-50%)` loop.

```html
<div class="ticker" aria-label="Breaking gossip headlines">
  <span class="ticker-label" aria-hidden="true">🔥 HOT NOW</span>
  <div class="ticker-viewport">
    <div class="ticker-track">
      <!-- copy 1 -->
      <span class="ticker-item">A-LIST COUPLE SPOTTED LEAVING DIVORCE LAWYER <b>◆</b></span>
      <span class="ticker-item">POP STAR'S "WELLNESS RETREAT" IS ACTUALLY REHAB, SOURCES SAY <b>◆</b></span>
      <span class="ticker-item">REALITY VILLAIN CAUGHT IN $4M TAX MESS <b>◆</b></span>
      <span class="ticker-item">WHICH STREAMER QUIETLY DELETED 200 POSTS OVERNIGHT? <b>◆</b></span>
      <span class="ticker-item">OSCAR WINNER'S SECRET WEDDING — GUEST LIST LEAKED <b>◆</b></span>
      <span class="ticker-item">BILLIONAIRE'S YACHT "MYSTERY GUEST" IDENTIFIED <b>◆</b></span>
      <span class="ticker-item">CHART-TOPPER ACCUSED OF STEALING THE HOOK <b>◆</b></span>
      <!-- copy 2 (identical) -->
      <span class="ticker-item">A-LIST COUPLE SPOTTED LEAVING DIVORCE LAWYER <b>◆</b></span>
      <span class="ticker-item">POP STAR'S "WELLNESS RETREAT" IS ACTUALLY REHAB, SOURCES SAY <b>◆</b></span>
      <span class="ticker-item">REALITY VILLAIN CAUGHT IN $4M TAX MESS <b>◆</b></span>
      <span class="ticker-item">WHICH STREAMER QUIETLY DELETED 200 POSTS OVERNIGHT? <b>◆</b></span>
      <span class="ticker-item">OSCAR WINNER'S SECRET WEDDING — GUEST LIST LEAKED <b>◆</b></span>
      <span class="ticker-item">BILLIONAIRE'S YACHT "MYSTERY GUEST" IDENTIFIED <b>◆</b></span>
      <span class="ticker-item">CHART-TOPPER ACCUSED OF STEALING THE HOOK <b>◆</b></span>
    </div>
  </div>
  <span class="ticker-time" aria-hidden="true">UPDATED HOURLY</span>
</div>
```
```css
.ticker{height:34px;display:flex;align-items:stretch;}
.ticker-label{display:flex;align-items:center;padding:0 10px;font:800 12px/1 var(--font);color:#fff;background:var(--red);animation:blink-bg 1.2s steps(1) infinite;}
.ticker-viewport{flex:1;overflow:hidden;position:relative;}
.ticker-track{display:flex;gap:0;white-space:nowrap;animation:ticker-scroll var(--tick-dur) linear infinite;}
.ticker-item{display:inline-flex;align-items:center;gap:14px;padding:0 14px;font:700 13px/34px var(--font);}
.ticker-item b{color:var(--yell);}
.ticker-time{display:flex;align-items:center;padding:0 10px;border-left:var(--rule-mid) solid var(--line-dark);background:#fff;font:800 11px/1 var(--mono);}
.ticker:hover .ticker-track{animation-play-state:paused;}
@keyframes ticker-scroll{from{transform:translateX(0)}to{transform:translateX(-50%)}}
@keyframes blink-bg{50%{background:var(--red-deep)}}
```
The pinned right `UPDATED HOURLY` stamp sits over the viewport edge so the crawl disappears under it.

**Visual goal:** classic cable-news crawl. Motion = urgency, but the content reads as real headlines.

### 3.3 Article cards (`index.astro` `.card`, lines 131–221)
- `.card` border `2px solid var(--line)` → `var(--rule-mid) solid var(--line-dark)` (4px black). Add `box-shadow: var(--shadow-hard)`; on `:hover` shift `translate(-2px,-2px)` with `box-shadow: 8px 8px 0 var(--red)` and red border. Add `transition: transform .12s, box-shadow .12s`.
- `--gloss` sheen via `.card::before` (top highlight) for wet glass.
- `.card h2` `font-family: var(--display)`, size `20px` → `22px`, `text-transform:uppercase`. First-card `26px` → `clamp(28px,4vw,38px)`.
- `.receipts` (red mono count) → receipt-tape style: monospace, red, prefix 🧾 (`aria-hidden`).
- `.flag--lead` / `.flag--new`: enlarge to `11px`, rotate `.flag--new` `-4deg`, give `--shadow-glow`.

**New elements per card:**
- **Rank number** (`<span class="card-rank" aria-hidden="true">#{i+1}</span>`): huge outlined `var(--display)` number top-right. Items 2+ get it.
- **Scandal heat meter** — deterministic, NOT random. `<div class="heat" data-heat="N" aria-label="Scandal heat: N of 5">`, where `N = Math.min(5, Math.ceil(receiptCount/2))`. Renders 5 blocks, `N` filled. Lives in `.card-foot`.
```css
.heat{display:inline-flex;gap:3px;}
.heat-block{width:14px;height:8px;background:#ddd;border:1px solid var(--line-dark);}
.heat-block.on{background:var(--red);}
```
- **Reaction stamp** (crude, raw — satisfies the "needs ugly" note): one of `LOL` / `OMG` / `SHOCKING` slapped on each card at a careless angle. Pick deterministically by index so it's stable: `['OMG','LOL','SHOCKING'][i % 3]`.
```html
<span class="react-stamp" aria-hidden="true">OMG</span>
```
```css
.react-stamp{position:absolute;top:8px;left:-6px;z-index:3;font:900 15px/1 var(--display);color:var(--red);
  background:var(--yell);padding:3px 7px;border:2px solid var(--line-dark);transform:rotate(-7deg);
  box-shadow:2px 2px 0 var(--line-dark);text-transform:uppercase;letter-spacing:.02em;}
.card:nth-child(3n) .react-stamp{transform:rotate(5deg);color:#fff;background:var(--red);}
```
- **EXCLUSIVE stamp** on the lead card — border-based starburst (see §3.5 for the shared technique).

**Visual goal:** cards stop being polite rectangles — they pop, scream in caps, quantify scandal, and carry a scrawled reaction.

### 3.4 Sidebar (`index.astro` `.side-col`, lines 223–286)
- `.side-box` border `2px solid var(--line)` → `var(--rule-mid) solid var(--line-dark)`, add `--shadow-hard`.
- `.side-box h3` → black fill tab: `background:var(--line-dark);color:#fff;display:inline-block;padding:3px 8px;`.
- `.tag` → pill with aqua glass gradient + black border; hover → yellow.
- `.side-box--cta` ("GOT A TIP?") keep black, add a yellow **border-based starburst** `::before`, make `.side-btn` an **aqua aero glass button** (`--radius-glass`, gradient `--bg-aqua`→white, inset highlight).

**New widgets:**

**(a) HOT OR NOT poll** — hardcode **3 real-sounding celebrity names** in HTML (no backend; localStorage tally, try/catch guarded):
```html
<div class="side-poll">
  <h3>HOT OR NOT</h3>
  <ul>
    <li data-poll="celeb-a"><span>Dahlia Voss</span>
      <button class="vote" data-v="hot">🔥 HOT</button><button class="vote" data-v="not">🗑️ NOT</button>
      <div class="bar"><i></i></div></li>
    <li data-poll="celeb-b"><span>Rex Camden</span>
      <button class="vote" data-v="hot">🔥 HOT</button><button class="vote" data-v="not">🗑️ NOT</button>
      <div class="bar"><i></i></div></li>
    <li data-poll="celeb-c"><span>Mireille Sang</span>
      <button class="vote" data-v="hot">🔥 HOT</button><button class="vote" data-v="not">🗑️ NOT</button>
      <div class="bar"><i></i></div></li>
  </ul>
</div>
```

**(b) SCANDAL-O-METER** — simple **5-bar block meter** (no conic-gradient dial; browser-safe and unambiguous), labelled "TODAY'S DRAMA LEVEL: CRITICAL", 4 of 5 lit:
```html
<div class="scandal-meter" aria-label="Scandal level: critical, 4 of 5">
  <h3>SCANDAL-O-METER</h3>
  <div class="meter-bars">
    <span class="on"></span><span class="on"></span><span class="on"></span><span class="on"></span><span></span>
  </div>
  <p class="meter-cap">TODAY'S DRAMA LEVEL: <b>CRITICAL</b></p>
</div>
```
```css
.meter-bars{display:flex;gap:4px;margin:6px 0;}
.meter-bars span{flex:1;height:18px;background:#ddd;border:2px solid var(--line-dark);}
.meter-bars span.on{background:var(--red);}
.meter-cap b{color:var(--red);font-family:var(--display);}
```

**(c) TRENDING NOW** — numbered list built from the existing tag-cloud data (reuse `tags`, no new data).

**(d) BLIND ITEMS teaser** — one fake blind item to justify the `BLIND ITEMS` nav link (otherwise a dead link):
```html
<div class="side-box side-blind">
  <h3>BLIND ITEM</h3>
  <p class="blind-q">WHICH A-LIST COUPLE — BOTH OSCAR NOMINEES — IS QUIETLY HEADED FOR A <b>$200M DIVORCE</b> WHILE PLAYING HAPPY ON THE RED CARPET?</p>
  <p class="blind-foot">🔒 ANSWER REVEALED SOON…</p>
</div>
```
```css
.side-blind .blind-q{font:700 14px/1.35 var(--font);text-transform:uppercase;}
.side-blind .blind-q b{background:var(--yell);}
.side-blind .blind-foot{font:800 11px/1 var(--mono);color:var(--red-deep);margin-top:8px;}
```

**(e) DON'T MISS widget** — FOMO staple, reuses existing article headlines:
```html
<div class="side-box side-dontmiss">
  <h3>DON'T MISS</h3>
  <ul>{posts.slice(0,4).map(p => <li><a href={`/articles/${p.slug}`}>👉 {p.data.title}</a></li>)}</ul>
</div>
```

**(f) MOST READ widget** — reuses existing article data, ranked:
```html
<div class="side-box side-mostread">
  <h3>MOST READ</h3>
  <ol>{posts.slice(0,5).map((p,i) => <li><b aria-hidden="true">{i+1}</b><a href={`/articles/${p.slug}`}>{p.data.title}</a></li>)}</ol>
</div>
```
With only 1 article in the collection today, `DON'T MISS` / `MOST READ` will show that one repeated headline list-style — acceptable filler; both scale automatically as content grows.

**Visual goal:** the sidebar becomes a carnival of trash widgets, not three quiet boxes.

### 3.5 Article hero (`articles/[slug].astro` `.hero`, lines 166–214)
- `.hero` border-bottom `3px` → `var(--rule)` black + red shadow stripe.
- `.hero h1` `font-family: var(--display)`, `text-transform:uppercase`, clamp `30–44px` → `clamp(34px, 6vw, 60px)`, `line-height:1.02`, `overflow-wrap:anywhere`. Yellow text-shadow for tabloid pop-out (mobile-reduced — see Pitfalls):
```css
.hero h1{text-shadow:2px 2px 0 var(--yell);}
@media (max-width:480px){ .hero h1{text-shadow:1px 1px 0 var(--yell);} }
```
- `.kicker` (red-outline badge) → solid red fill, white text, rotate `-2deg`.
- `.receipts-hero` → bigger, prefix 🧾, red.

**New elements:**

**BREAKING banner** above `.hero-rail` — full-width black bar, blinking red "BREAKING" + the kicker type:
```html
<div class="breaking-bar"><span class="breaking-tag">BREAKING</span><span>{data.type}</span></div>
```
```css
.breaking-bar{display:flex;align-items:center;gap:10px;background:var(--line-dark);color:#fff;
  padding:6px 12px;font:800 13px/1 var(--font);text-transform:uppercase;letter-spacing:.05em;}
.breaking-tag{background:var(--red);padding:3px 8px;animation:blink 1s steps(1) infinite;}
@keyframes blink{50%{opacity:.25}}
```

**HERO IMAGE slot** — large framed photo with sensational overlay caption (the site currently ships zero images; this is the single biggest visual gap). Use a placeholder source; yellow border frame:
```html
<figure class="hero-photo">
  <img src="https://placehold.co/1200x675/111/FFE600?text=EXCLUSIVE+PHOTO" alt="Exclusive photo relating to the story" loading="eager" width="1200" height="675">
  <figcaption>📸 EXCLUSIVE: THE PHOTO THEY DIDN'T WANT YOU TO SEE</figcaption>
</figure>
```
```css
.hero-photo{position:relative;margin:0 0 18px;border:6px solid var(--yell);box-shadow:var(--shadow-hard);background:#000;}
.hero-photo img{display:block;width:100%;height:auto;}
.hero-photo figcaption{position:absolute;left:0;bottom:0;right:0;background:rgba(0,0,0,.78);color:#fff;
  font:800 14px/1.2 var(--font);text-transform:uppercase;padding:8px 12px;letter-spacing:.03em;}
```

**EXCLUSIVE starburst** — **border-based, rotated container** (NOT clip-path; border-trick is reliable across browsers and degrades gracefully). Two stacked squares rotated 45°/0° behind a centered label, absolute top-right of hero:
```html
<div class="starburst" aria-hidden="true"><span>EXCLUSIVE</span></div>
```
```css
.starburst{position:absolute;top:-14px;right:-10px;width:96px;height:96px;display:grid;place-items:center;z-index:4;}
.starburst::before,.starburst::after{content:'';position:absolute;inset:0;background:var(--red);
  border:3px solid var(--line-dark);box-shadow:var(--shadow-hard);}
.starburst::before{transform:rotate(0deg);}
.starburst::after{transform:rotate(45deg);}
.starburst span{position:relative;z-index:1;color:#fff;font:900 13px/1 var(--display);transform:rotate(-8deg);text-align:center;}
```

**Byline-of-shame** — mono strip: `BY THE GOSSIP DESK • {date} • {N} SOURCES` (`N = claim_evidence.length`).

**Visual goal:** the hero screams — and now *shows a photo* — before you read a word.

### 3.6 THE TEA section (`articles/[slug].astro` `.angle`, lines 216–259)
- `.angle` border `3px` → `var(--rule)` black, background `--yell-soft` → layered `--yell` gradient with `--gloss` sheen, add `--shadow-hard`.
- `.angle h2` ("THE TEA") → black fill tab + ☕ emoji + rotate `-1deg`, `font-family: var(--display)`.
- `.angle li::before` `▸` → `☕` or `🔥`; finding text uppercase bold.

**New element — SPILL-O-METER** at top-right of the box: the literal **☕ emoji repeated 1–5 times** (not an abstract "teacup rating"). Derive the count from `implications.length`: `cups = Math.min(5, implications.length)` (which is 5 today). Render the chosen number of ☕ plus a text label for accessibility:
```html
<div class="spillometer" aria-label="Spill level: 5 of 5">
  <span aria-hidden="true">☕☕☕☕☕</span>
</div>
```

**Visual goal:** the highlight box becomes the loudest yellow scandal panel on the page.

### 3.7 Evidence / CASE FILE (`articles/[slug].astro` `.evidence`, lines 330–454)
Keep the forensic credibility — louder, never cartoonish:
- `.evidence` top border `3px` → `var(--rule)` black.
- `.evidence h2` ("CASE FILE") → black tab + 🔒 + `var(--display)`.
- `.evidence-item` border `2px` → `var(--rule-mid) solid var(--line-dark)`; `[open]` adds `--shadow-hard` + a red left bar.
- `.ev-num` bigger (`22px`), red, mono.
- `.ev-stamp` "VERIFIED" (rotated `-3deg`) → proper red ink stamp: double border, slight opacity 0.85; add a second green "ON THE RECORD" variant. Keep rotation.
- `.ev-hash` keep monospace; tint background to read like a redaction/receipt strip.

**Visual goal:** evidence reads like a leaked dossier — stamps, hashes, redaction bars — reinforcing "we have RECEIPTS."

### 3.8 SOUND OFF reaction section (NEW — below article body, above footer in `articles/[slug].astro`)
A tabloid engagement loop. Four reaction buttons; cosmetic localStorage tally (try/catch guarded, shares the poll script):
```html
<section class="soundoff">
  <h2>SOUND OFF</h2>
  <div class="soundoff-grid">
    <button class="react" data-react="shocking"><span aria-hidden="true">😱</span> SHOCKING</button>
    <button class="react" data-react="outraged"><span aria-hidden="true">😡</span> OUTRAGED</button>
    <button class="react" data-react="canteven"><span aria-hidden="true">😂</span> CAN'T EVEN</button>
    <button class="react" data-react="whocares"><span aria-hidden="true">🥱</span> WHO CARES</button>
  </div>
</section>
```
```css
.soundoff{border:var(--rule) solid var(--line-dark);box-shadow:var(--shadow-hard);background:var(--surface);padding:18px;margin:28px 0;}
.soundoff h2{font:900 22px/1 var(--display);text-transform:uppercase;background:var(--line-dark);color:#fff;display:inline-block;padding:4px 10px;}
.soundoff-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-top:14px;}
.react{font:800 14px/1 var(--font);text-transform:uppercase;padding:14px 8px;border:3px solid var(--line-dark);
  background:#fff;cursor:pointer;transition:transform .1s,background .1s;}
.react:hover{background:var(--yell);transform:translate(-2px,-2px);box-shadow:var(--shadow-hard);}
.react.voted{background:var(--red);color:#fff;}
@media (max-width:560px){ .soundoff-grid{grid-template-columns:repeat(2,1fr);} }
```

### 3.9 Paparazzi photo grid (NEW — below the feed on `index.astro`)
A dense 3-column wall of placeholder photos, yellow borders, crude caption strips:
```html
<section class="papwall">
  <h2>CAUGHT ON CAMERA</h2>
  <div class="pap-grid">
    <figure><img src="https://placehold.co/400x400/222/FFE600?text=SPOTTED" alt="Paparazzi photo" loading="lazy" width="400" height="400"><figcaption>SPOTTED: 3AM TACO RUN</figcaption></figure>
    <figure><img src="https://placehold.co/400x400/222/FF2D95?text=BUSTED" alt="Paparazzi photo" loading="lazy" width="400" height="400"><figcaption>BUSTED: NO RING?!</figcaption></figure>
    <figure><img src="https://placehold.co/400x400/222/00C2FF?text=EXCLUSIVE" alt="Paparazzi photo" loading="lazy" width="400" height="400"><figcaption>EXCLUSIVE: WHO'S THAT?</figcaption></figure>
    <figure><img src="https://placehold.co/400x400/222/B6FF00?text=YIKES" alt="Paparazzi photo" loading="lazy" width="400" height="400"><figcaption>YIKES: OUTFIT FAIL</figcaption></figure>
    <figure><img src="https://placehold.co/400x400/222/FFE600?text=SHOCK" alt="Paparazzi photo" loading="lazy" width="400" height="400"><figcaption>SHOCK SPLIT?</figcaption></figure>
    <figure><img src="https://placehold.co/400x400/222/FF0033?text=LEAKED" alt="Paparazzi photo" loading="lazy" width="400" height="400"><figcaption>LEAKED DMs</figcaption></figure>
  </div>
</section>
```
```css
.pap-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;}
.pap-grid figure{position:relative;margin:0;border:5px solid var(--yell);box-shadow:var(--shadow-hard);background:#000;}
.pap-grid img{display:block;width:100%;height:auto;}
.pap-grid figcaption{position:absolute;left:0;right:0;bottom:0;background:rgba(0,0,0,.78);color:#fff;
  font:800 11px/1.1 var(--font);text-transform:uppercase;padding:5px 7px;}
@media (max-width:560px){ .pap-grid{grid-template-columns:repeat(2,1fr);} }
```

### 3.10 Footer (`BaseLayout.astro` `.site-footer`, lines 220–250)
- Top border `3px` → `var(--rule)` black + red shadow stripe.
- `.footer-brand .brand-mark` apply `var(--display)` + red highlight `.x`.
- `.footer-links a` → black-tab hover.
- Add a **disclaimer scroll**: tiny italic "All gossip hash-locked. Don't trust us, verify the receipts. Probably."

### 3.11 Subscribe (`BaseLayout.astro` `.subscribe`, lines 283–341)
- `.subscribe` top border `3px` → `var(--rule)`; background → aqua-sky gradient + `--gloss`.
- `.subscribe-copy h2` ("NEVER MISS THE TEA") → `var(--display)`, uppercase, `22px`, yellow text-shadow.
- `.subscribe-form input` border `2px` → `var(--rule-mid)`; button keep black, hover red, add `--shadow-hard`, uppercase wide tracking.
- **No fake counter.** Replace any number with a static, no-number badge: **"JOIN THE GOSSIP ADDICTS"** (a fabricated subscriber count would directly undermine the site's "verify the receipts" brand promise — it is cut entirely, no count-up, no IntersectionObserver).
```html
<p class="sub-badge">JOIN THE GOSSIP ADDICTS</p>
```

### 3.12 Deliberately ugly element — CRUDE STAR (NEW, global utility)
One intentionally hand-drawn, crooked star outline built from CSS border tricks (overlapping rotated bordered triangles), placed near the lead card or the BREAKING bar for raw Perez-Hilton scribble energy. It is *meant* to look crude:
```html
<span class="crude-star" aria-hidden="true"></span>
```
```css
.crude-star{position:relative;display:inline-block;width:0;height:0;
  border-left:18px solid transparent;border-right:18px solid transparent;border-bottom:13px solid transparent;
  transform:rotate(35deg);filter:none;}
.crude-star::before{content:'';position:absolute;top:3px;left:-18px;width:0;height:0;
  border-left:18px solid transparent;border-right:18px solid transparent;border-bottom:13px solid transparent;
  transform:rotate(-70deg);}
.crude-star,.crude-star::before{border-bottom-color:transparent;}
/* outline scribble look via stacked rotated bordered boxes */
.crude-star{outline:3px solid var(--red);outline-offset:-2px;}
```
Render at a careless angle (`transform:rotate(-9deg)`) with a slightly-too-thick uneven red outline so it reads hand-scrawled, not vector-clean.

---

## 4. NEW ELEMENTS TO ADD (master inventory)

| # | Element | File | Data source | Notes |
|---|---------|------|-------------|-------|
| 1 | Animated multi-item marquee ticker | BaseLayout | 6–8 hardcoded items | CSS only, reduced-motion guard |
| 2 | LIVE pulse dot | BaseLayout header | static | `@keyframes pulse` |
| 3 | BREAKING banner | [slug] hero | `data.type` | blink, reduced-motion guard |
| 4 | EXCLUSIVE starburst (border-based) | [slug] hero + lead card | static/lead flag | rotated containers, NOT clip-path |
| 5 | Scandal heat meter (5 blocks) | index card | `claim_evidence.length` (deterministic) | NOT random |
| 6 | Listicle rank number `#N` | index card | loop index `i` | outlined display font |
| 7 | SCANDAL-O-METER (5-bar block meter) | index sidebar | static/derived | NOT conic-gradient dial |
| 8 | HOT OR NOT poll (3 hardcoded celebs) | index sidebar | localStorage | cosmetic vote bars |
| 9 | SPILL-O-METER (☕ × 1–5) | [slug] TEA box | `implications.length` | literal repeated emoji |
| 10 | Paparazzi flash on hover | global (cards/stamps) | — | radial-gradient flash, reduced-motion guard |
| 11 | Subscribe badge "JOIN THE GOSSIP ADDICTS" | BaseLayout subscribe | static text | NO number, NO count-up |
| 12 | Black-tab section labels | all `h3`/section heads | — | reusable `.tab-label` class |
| 13 | Aero glass buttons | CTAs | — | reusable `.btn-aero` class |
| 14 | Starburst badge | sidebar CTA / hero | — | reusable `.starburst` (border-based) |
| 15 | Hero image (framed + caption) | [slug] hero | placeholder | yellow frame, overlay caption |
| 16 | Paparazzi photo grid (3-col) | index, below feed | placeholders | yellow borders, lazy-loaded |
| 17 | BLIND ITEMS sidebar teaser | index sidebar | static fake item | justifies nav link |
| 18 | SOUND OFF reaction section | [slug], below body | localStorage | 4 buttons, reduced-motion safe |
| 19 | Reaction stamps (LOL/OMG/SHOCKING) | index card | `i % 3` | crude, rotated |
| 20 | DON'T MISS widget | index sidebar | reuse headlines | FOMO list |
| 21 | MOST READ widget | index sidebar | reuse article data | numbered list |
| 22 | Yellow highlighter on key phrases | [slug] body | manual `<mark>` | see below |
| 23 | Crude star (deliberately ugly) | global | — | CSS border tricks |

**Paparazzi flash (signature effect)** — white radial bloom on card hover:
```css
.card::after{content:'';position:absolute;inset:0;background:radial-gradient(circle at 50% 0,rgba(255,255,255,0.9),transparent 60%);opacity:0;pointer-events:none;}
.card:hover::after{animation:flash .4s ease-out;}
@keyframes flash{0%{opacity:0}15%{opacity:1}100%{opacity:0}}
```

**Yellow highlighter-pen effect** — wrap key phrases in the article body in `<mark class="hl">`; uneven, slightly-rotated highlighter look:
```css
.prose mark.hl{background:linear-gradient(120deg,var(--yell) 0%,var(--yell) 100%);
  background-repeat:no-repeat;background-size:100% 60%;background-position:0 70%;
  color:inherit;padding:0 2px;box-decoration-break:clone;}
```
Apply manually to 3–5 sensational phrases (e.g. wrap them in `[slug].astro` body content or via the existing prose injection). Keep `<mark>` semantics for accessibility.

---

## 5. FILE MODIFICATION PLAN

```
BaseLayout.astro
  HEAD (≈line 18): add Archivo Black font <link> (+preconnect).
  HEADER (22–38):
    - add <span class="live-dot"> in .brand
    - add 5th nav item "SCANDALS" (BLIND ITEMS link already justified by sidebar teaser)
    - rebuild ticker → .ticker-viewport/.ticker-track with 6–8 hardcoded items duplicated ×2, ◆ separators, pinned "UPDATED HOURLY"
  SUBSCRIBE (44–63): add static "JOIN THE GOSSIP ADDICTS" badge (NO counter); restyle copy.
  FOOTER (65–83): add disclaimer scroll line; display-font brand.
  :ROOT (86–109): full token replacement per §2.
  GLOBAL CSS (110–349):
    - header: 6px rule + red stripe, gloss ::after, display brand, red-block .x, live-dot keyframes
    - ticker: marquee keyframes, blink-bg, hover-pause
    - badges (252–281): thicken to 3px, rotate human_reviewed, add stamp variants
    - subscribe/footer: thicker rules, gloss, aero button
    - ADD reusable utilities: .tab-label, .btn-aero, .starburst (border-based), .stamp, .crude-star, flash/pulse/blink keyframes
    - ADD the SINGLE global prefers-reduced-motion block (see §6) disabling ALL animations
  MEDIA QUERY (343–349): nav-collapse for 5 items, scale display headings down, ensure ticker still scrolls, mobile text-shadow reduction.

index.astro
  TEMPLATE (33–56): per card add card-rank #N (items 2+), reaction stamp (i%3), EXCLUSIVE stamp (lead only), heat meter in .card-foot (deterministic from receiptCount).
  SIDEBAR (59–84): add SCANDAL-O-METER (5-bar), HOT OR NOT (3 hardcoded celebs), TRENDING NOW (reuse tags), BLIND ITEM teaser, DON'T MISS, MOST READ.
  NEW SECTION below feed: paparazzi photo grid (.papwall).
  SCRIPT (NEW): localStorage vote logic for HOT OR NOT (try/catch guarded).
  STYLE (87–295):
    - .card: 4px black border, shadow-hard, hover translate+red shadow, gloss ::before, flash ::after, uppercase display h2
    - .flag--*: enlarge, rotate, glow
    - NEW: .card-rank, .react-stamp, .heat/.heat-block, .side-poll, .scandal-meter/.meter-bars, .trending, .side-blind, .side-dontmiss, .side-mostread, .papwall/.pap-grid
    - .side-box h3 → black tab; .side-btn → aero button
    - MOBILE: move HOT OR NOT (or TRENDING NOW) ABOVE the feed at ≤820px (see §6)
    - update 820px media query for new widgets + pap-grid columns.

articles/[slug].astro
  TEMPLATE HERO (34–51): add BREAKING banner + hero image figure + EXCLUSIVE starburst + byline strip.
  TEA (53–67): add SPILL-O-METER (☕ × implications.length).
  EVIDENCE (73–113): add second stamp variant; no markup restructure needed.
  NEW SECTION below body: SOUND OFF reaction section (.soundoff).
  BODY: wrap 3–5 key phrases in <mark class="hl">.
  SCRIPT (143–161): existing logic unchanged; add guarded SOUND OFF localStorage handler (share poll util).
  STYLE (163–505):
    - .hero h1 display font + uppercase + clamp 34–60 + yellow text-shadow (1px on <480px) + overflow-wrap
    - .kicker solid red rotated; .breaking-bar / .starburst / .hero-photo / byline styles
    - .angle: 6px rule, yellow gradient+gloss, display tab h2, .spillometer
    - .evidence: 6px top rule, black tab h2, 4px item borders, enhanced stamps
    - .soundoff / .react styles
    - update 720px media query for hero scale + banner wrap + hero-photo.

disclosure.ts
  NO CHANGE (logic/data only). Badge styling is purely CSS in BaseLayout.
```

---

## 6. IMPLEMENTATION ORDER (by dependency)

1. **Tokens first** (`:root` §2) + Archivo Black font load. *Everything downstream references these vars.*
2. **Global utilities & keyframes** in BaseLayout (`.tab-label`, `.btn-aero`, `.starburst`, `.stamp`, `.crude-star`, `pulse/blink/flash/ticker-scroll`, and the SINGLE global reduced-motion kill-switch). *Cards, hero, and reaction sections reuse these.*
3. **BaseLayout shell**: header (rules, brand block, LIVE dot), multi-item ticker marquee, footer, subscribe (no-number badge). *Shared chrome on every page; validates the token system.*
4. **Badges** (BaseLayout global) — used by both index cards and article meta.
5. **index.astro cards** (borders, gloss, flash, rank, heat meter, reaction stamp, EXCLUSIVE).
6. **index.astro sidebar widgets** (scandal 5-bar meter, hot-or-not, trending, blind item, don't-miss, most-read) + their script; **paparazzi grid** below feed.
7. **[slug].astro hero** (BREAKING, hero image, border-starburst, display title).
8. **[slug].astro TEA + evidence + stamps + SOUND OFF + highlighter marks.**
9. **Responsive pass** — re-check all three `@media` blocks; confirm a widget moves above the feed on mobile; confirm pap/soundoff grids reflow; confirm `1px` hero text-shadow under 480px.
10. **Motion/accessibility pass** — verify the single reduced-motion block, contrast, focus states, emoji `aria-hidden`.

**The single global reduced-motion block** (one place, covers every animation — ticker, blink, pulse, flash, breaking):
```css
@media (prefers-reduced-motion: reduce){
  .ticker-track, .ticker-label, .live-dot i, .breaking-tag,
  .card:hover::after, .starburst, .react, *[class*="anim"]{
    animation: none !important;
    transition: none !important;
  }
}
```

**Mobile widget reorder** — at least one sidebar widget surfaces above the feed on mobile (sidebar is otherwise hidden ≤820px). Render HOT OR NOT (or TRENDING NOW) a second time inside a `.mobile-first-widget` wrapper placed before the feed in source, shown only on mobile:
```css
.mobile-first-widget{display:none;}
@media (max-width:820px){
  .mobile-first-widget{display:block;margin:0 0 18px;}
  .side-col{display:none;} /* existing behavior */
}
```

Ship 1–4 as a "foundation" commit, 5–6 as "homepage trash," 7–8 as "article trash," 9–10 as "polish." Each is independently reviewable.

---

## 7. PITFALLS

**Performance**
- **`backdrop-filter` stacking.** `blur(22px)` on header + every card + sidebar + subscribe can tank scroll FPS on mid-tier laptops. Use heavy blur only on header/hero; cards use flat `--surface` + gloss `::before` (a gradient, not a filter). Cap concurrent `backdrop-filter` elements.
- **Images.** The hero photo and 6-tile paparazzi grid add real bytes. Use `loading="lazy"` on every paparazzi image, `loading="eager"` only on the hero, and always set `width`/`height` to prevent layout shift (CLS). Placeholder URLs are fine for the build; swap to real assets later without markup changes.
- **Animations.** Marquee, blink, pulse, flash run `transform`/`opacity` only (GPU-friendly). Avoid animating `width`/`box-shadow`/`background-position` in loops. The ticker `blink-bg` repaints a tiny element — acceptable.
- **Font weight.** Archivo Black is one weight; `display=swap` avoids FOIT. List only Inter 400/600/700/800/900.

**Mobile / responsive**
- Pure-black 6px rules + 5px offset shadows eat horizontal space; under 420px, drop `--rule` to 4px and `--shadow-hard` to `3px 3px` via media override, or content overflows `.wrap`'s 20px padding.
- 5 nav items won't fit at 720px — current code hides `.site-nav` at ≤720px. Consider a compact 2-row nav so SCANDALS / BLIND ITEMS stay reachable (the BLIND ITEMS link now resolves to real content via the sidebar teaser, so don't leave it dead).
- `clamp(34px,6vw,60px)` hero on a long single word can overflow — `overflow-wrap:anywhere`/`hyphens:auto` on `.hero h1` (already specced).
- Sidebar widgets hide at ≤820px; HOT OR NOT / scandal meter would vanish on mobile. The `.mobile-first-widget` reorder (§6) surfaces at least one above the feed — required, not optional.
- Marquee: exactly **two** identical copies in `.ticker-track` with `translateX(-50%)`, or the loop seams. The 6–8 hardcoded items make the seam test meaningful — verify with the longest item present.
- Hero text-shadow at full `2px 2px` smears on small screens; the `1px 1px` override under 480px is mandatory for legibility.
- SOUND OFF (4 cols) and paparazzi grid (3 cols) must reflow to 2 cols on narrow screens (specced at 560px).

**Accessibility**
- **Motion:** every animation sits behind the SINGLE global `prefers-reduced-motion: reduce` block (§6) — ticker, blink, pulse, flash, breaking. Non-negotiable; blinking BREAKING bars are a vestibular/seizure risk.
- **Contrast:** hot magenta `#FF2D95` and lime `#B6FF00` fail WCAG AA on white for body text — decorative/large use only. Red `#FF0033` on white is ~4.0:1 — keep red text bold/large. White-on-black caption strips and tabs are high-contrast and safe.
- **Emoji as UI (🔥🧾☕😱😡):** every decorative emoji needs `aria-hidden="true"`. The heat meter, scandal meter, spill-o-meter, and SOUND OFF buttons expose text labels / `aria-label` (e.g. `aria-label="Scandal heat: 4 of 5"`, `"Spill level: 5 of 5"`).
- **Images:** every `<img>` has a real `alt` (not empty), since these are content-bearing tabloid photos. Caption text is supplementary, in `<figcaption>`.
- **Reaction stamps / crude star:** purely decorative → `aria-hidden="true"`, no semantic value, must not be the only label on an interactive element.
- **Focus states:** the brutalist hover (`translate` + red shadow) needs a matching `:focus-visible` outline on the `.card` anchor and on every `.vote`/`.react` button — keyboard users currently get nothing extra.
- **Poll/SOUND OFF localStorage:** wrap in try/catch — Safari private mode throws on `localStorage`. Buttons must still toggle visually if storage fails.
- **Uppercase headings:** use CSS `text-transform`, not literal capitals in content, so screen readers and future edits keep clean source text. Avoid letter-spacing so wide it breaks word recognition.

**Brand-integrity risk**
- The credibility gimmick is "every claim hash-locked / verify the receipts." **No fabricated metrics** — the subscriber count is cut entirely and replaced with the no-number "JOIN THE GOSSIP ADDICTS" badge. The HOT OR NOT / SOUND OFF / scandal-meter widgets are obviously playful and clearly cosmetic, so they don't read as fake data claims.
- Keep the CASE FILE / evidence section **legible and serious** (dossier stamps, hashes, redaction bars), never cartoonish, or the "verify the receipts" value prop collapses. Trash the chrome; respect the evidence. The deliberately-ugly crude star and reaction stamps live in the *chrome* (cards, hero, banner) — never inside the evidence block.

**Build / Astro specifics**
- Component `<style>` blocks in `.astro` are scoped; global tokens and reusable utilities (`.tab-label`, `.btn-aero`, `.starburst`, `.stamp`, `.crude-star`, all keyframes, the reduced-motion block) MUST live in the `is:global` block in BaseLayout. Scoped templates can reference global CSS vars and global classes freely.
- `:global()` is already used for body prose; the new `mark.hl` highlighter and any injected `.cite-link` styling stay in that global prose scope.
- **No data-schema changes.** Heat meter, spill-o-meter, scandal meter, rank, byline source count, and the DON'T MISS / MOST READ lists all derive from existing fields (`claim_evidence.length` = 5, `implications.length` = 5, loop index, post list) — no content-collection edits, no `disclosure.ts` change. Placeholder images and hardcoded ticker/celeb/blind-item strings are static template content, not data.
- With only **1 article** in the collection today, MOST READ / DON'T MISS render a short repeated list — acceptable filler that scales automatically as articles are added; no special-casing needed.
