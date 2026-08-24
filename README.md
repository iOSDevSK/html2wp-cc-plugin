# html2wp

**Turn a static site into a real WordPress theme — pixel-identical, and still editable.**

A Codex and Claude Code plugin that converts a static HTML site, or anything that builds
to one (Lovable, Bolt, v0, Claude artifacts, Next.js export, hand-written
pages), into a standard WordPress block theme. Pages, blog, forms, menus, SEO
and redirects work with **no plugin required**. The markup stays the design's
own — no page builder, no rebuild.

> **Beta.** This converts real sites and is used on real work, but the
> gates catch most fidelity drift, not all of it. The last stage of every
> conversion is your own page-by-page review, and the skill walks you
> through it. Do not promise a client a result you have not looked at.

**Full documentation: [html2wp.dev/docs](https://html2wp.dev/docs)**

---

## How it works

Your machine does the browser work — prerendering, screenshot and
structural comparison, and a throwaway WordPress in Docker for the final
checks. The html2wp service generates the theme: your **built site is
uploaded to it**, because that is what the conversion is performed on, and
the theme comes back. (Exactly which files travel is listed in `SKILL.md`
under *What goes up*; stage 1 keeps credential-shaped files such as `.env`
out of that payload and names every one it dropped.) What comes back is
yours: readable PHP, CSS and JS, no lock-in, and a theme that calls nothing
at runtime — the delivered site talks to no server of ours, ever.

Every stage ends in a **gate**, and a failed gate stops the pipeline. The
conversion is compared against the original at three viewport widths, and the
result is installed into a real WordPress and driven before it is handed over.

### What the skill reports back

This section is about the **verdicts**, which travel separately from the
upload above and long after it. Everything it says is scoped to that payload.

The gates run on your machine, against a WordPress the service never sees, so
without you it would never learn whether a conversion was actually *correct* —
only that it did not crash. So the last step sends the gate results back, and
this is **required**: the next conversion is refused until the previous one
has reported.

What that means in practice is worth being precise about, because "required
telemetry" deserves the detail:

- **What goes**: gate names, pass/fail, page counts, the worst fidelity
  percentage, and the page *keys* that failed — the short names you chose
  yourself, like `about` or `pricing`.
- **What does not**: no URL, no domain, no markup, no copy, no screenshots, no
  file paths, no licence key, no site name. The payload is a whitelist, not a
  filter, so a field nobody anticipated cannot leak through it.
- **See for yourself**: `send-verdicts.sh <workspace> --dry-run` prints the
  exact payload and sends nothing — it is one readable shell script.

No other *telemetry* exists — this is the only thing the skill reports about a
conversion, and the theme you receive reports nothing at all. The conversion
upload described above is not telemetry and is not covered by this list: it is
the site itself, sent to be converted.

## Install

The same plugin is published twice, because the two hosts read different
manifests. **Take the repository for the tool you use** — they carry identical
scripts and the same version number, and the wrong one simply will not be
found.

| | Repository |
|---|---|
| **Codex** | `iOSDevSK/html2wp-codex-plugin` |
| **Claude Code** | `iOSDevSK/html2wp-cc-plugin` |

### Codex

```bash
codex plugin marketplace add iOSDevSK/html2wp-codex-plugin
codex plugin add html2wp@html2wp
```

Then ask Codex, in the directory you want converted:

```
Convert this project into a verified WordPress block theme.
```

Codex finds the bundled `html2wp` skill through `.agents/plugins/marketplace.json`,
which is why this is the `-codex` repository: the Claude one is laid out for
Claude Code and carries no `.agents/` manifest for Codex to read. No Claude
namespace and no Claude environment variable is involved.

To update, refresh the marketplace snapshot and re-add:

```bash
codex plugin marketplace upgrade
codex plugin add html2wp@html2wp
```

`codex plugin marketplace list` shows what Codex is currently reading, if the
plugin does not appear.

### Claude Code

```
/plugin marketplace add iOSDevSK/html2wp-cc-plugin
/plugin install html2wp@html2wp
```

### Keeping Claude Code current

```
/plugin marketplace update html2wp
```

**Worth doing once instead:** open `/plugin`, find html2wp under Marketplaces,
and turn auto-update on. Claude Code leaves auto-update **off by default for
marketplaces that are not its own**, so without that step this plugin only
changes when you ask it to — and some releases are security fixes to code that
runs on your machine and reads your project directory. The changelog for each
version is the commit history; releases are tagged.

Half of html2wp is the service, and that half updates itself: a fix there
reaches your next conversion with nothing to install. What waits for you is
the part running locally — the payload filter, the gates, the scripts.

<details>
<summary>Without either plugin system</summary>

**Claude Code** — the plugin is the repository root, so the skill is `skills/html2wp/`:

```bash
git clone --depth 1 https://github.com/iOSDevSK/html2wp-cc-plugin /tmp/html2wp \
  && cp -R /tmp/html2wp/skills/html2wp ~/.claude/skills/ \
  && rm -rf /tmp/html2wp
```

**Codex** — that repository is a marketplace wrapping the plugin, so the skill
sits one level in, at `plugins/html2wp/skills/html2wp/`:

```bash
git clone --depth 1 https://github.com/iOSDevSK/html2wp-codex-plugin /tmp/html2wp \
  && cp -R /tmp/html2wp/plugins/html2wp/skills/html2wp ~/.codex/skills/ \
  && rm -rf /tmp/html2wp
```

Copy the inner skill directory rather than cloning a repository over
`~/.codex/skills/html2wp` or `~/.claude/skills/html2wp` — the repository root
carries the plugin manifests, not the skill.

Installed this way it is a plain skill rather than a plugin skill, so it is
invoked directly by name (or `/html2wp` in Claude Code).
</details>

## Convert a Lovable project

**If you have a licence key, set it up first** — once per machine, from any
directory. On the free tier skip this and change nothing else:

```bash
mkdir -p ~/.config/html2wp
printf '%s' 'YOUR-KEY' > ~/.config/html2wp/licence
chmod 600 ~/.config/html2wp/licence
```

In your terminal (use `claude` instead if that is your host):

```bash
git clone https://github.com/YOU/YOUR-LOVABLE-APP
cd YOUR-LOVABLE-APP
codex
```

In Codex — one line, one enter:

```
convert this project into a verified WordPress block theme
```

In Claude Code the equivalent namespaced command is:

```
/html2wp:html2wp convert this project
```

That is the whole of it — no `npm install`, no `npm run build`, no
configuration. Bolt, v0, shadcn and Next.js export projects take the same
line.

Asking Codex to use `html2wp`, or invoking `/html2wp:html2wp` on its own in
Claude Code, works too; it then asks what to convert.

## Using it

### What it does with a Lovable project

A Lovable app is React: one `index.html` with an empty mount node and a
script. There is nothing 1:1 to convert in that, so the skill builds the
project, opens the built app in a real browser, and records each route as flat
HTML — including the state that only exists after JavaScript has run: open
accordions, scroll-triggered classes, dropdown contents. Those become the
pages it converts.

### Pointing it at something else

`convert this project` covers the common case. If the files live elsewhere,
say where — a path is a complete answer:

```
convert ./dist
```

| What you have | What to say |
|---|---|
| a project that builds to a site — Lovable, Bolt, v0, Vite, Astro, Next export | `convert this project` |
| a folder of finished `.html` files with their assets | `convert ./that-folder` |

The input is always something on your disk. A live URL is not an input —
convert what the site is built from, not what a browser happens to render.

### What the first minutes look like

```
> convert this project

  Checking this machine first…
    Node.js     ok    v22.14.0
    Playwright  MISSING
  Two Python packages are missing. Shall I install them? (they go in your
  user directory, no root)

> yes

  … installed. Building the project, then prerendering it.
  7 routes found: /, /about, /pricing, /blog, /blog/launch, /contact, /faq
  Before I go on: is /pricing a normal page, or part of the blog?
```

That last question is the one that matters — see below.

### What you will be asked

**Which page is which.** This is the one place your answers change the result:
which page is the home page, which one is the blog listing, which are articles,
which are products. The coding agent proposes an answer from the markup and you correct
it. Getting this wrong is also the cheapest thing to fix — correcting it and
running again is a **re-run**, and re-runs do not spend a conversion.

**Then it runs mostly unattended** — 30 to 90 minutes. The best predictor is
not page count but how many *different* header and footer designs the site
has. It builds the site, checks the build against the original, sends it for
conversion, installs the theme into a throwaway WordPress in Docker on your
machine, and drives that WordPress to test it.

### The part you cannot skip

Near the end you are shown, for every page, the original and the converted
page side by side in one image. **You read each one and say what you see.**

The numeric gates pass things a person would reject — a dropped
below-the-fold section once scored 0.4% on the pixel comparison, comfortably
green. Scripts measure; this step judges. It is the last stage for a reason,
and the skill will not call a conversion finished until it is done.

### What you end up with

- **the theme, as an installable `.zip`** — WordPress → Appearance → Themes →
  Add New → Upload. It refuses to build a broken one: PHP that does not lint,
  a missing content bundle, a wrong-sized screenshot, or a shop you could not
  actually buy from;
- **a conversion report** — pages converted, menus wired, every finding from
  your review, every warning from every stage including the ones judged
  acceptable, and anything left for you to do;
- **`visual-edit.zip`**, if the conversion was licensed. A free conversion
  ships the theme alone and it is fully usable — the editor is an extra, not
  a missing piece.

## Requirements

| | |
|---|---|
| Node.js | 20 or newer |
| Python 3 | with Playwright (chromium) and Pillow |
| Docker | plus `docker compose` — used for the verification WordPress |
| Also | `php-cli`, `jq`, `curl`, `bash` |
| Target | WordPress 6.6 or newer |

You do not have to work that list out yourself. Ask for a conversion and the
first thing it does is check the machine and tell you exactly what is missing:

```
  Node.js                ok        v22.14.0
  Python                 ok        3.12.4
  Playwright             MISSING   mirroring, prerendering and every screenshot
  Docker                 NOT RUNNING  installed, but the daemon is not up
```

Anything user-local — the Python packages, the chromium download — it offers
to install for you, one command at a time, each one asking first. Anything
that changes the machine — Docker Desktop, a Node upgrade — it tells you about
and then waits, because that is your call and not its.

If you would rather do it by hand:

```bash
python3 -m pip install playwright pillow && python3 -m playwright install chromium
```

## What is free

**Three conversions per machine, up to 5 pages each**, plus five re-runs.
A re-run is the same built site uploaded again — so fixing how a page was
described and trying again does not spend a conversion. That is enough to
convert a real site and judge the result properly.

The free tier is for **evaluation, personal sites and other non-commercial
work**. Converting a site for a client, or for a business you run, is
commercial use and needs a licence key — see [LICENSE](LICENSE).

| | Free | With a licence key |
|---|---|---|
| Commercial use, client work | no | yes |
| Pages per conversion | 5 | 20 (agency licences: no limit) |
| Conversions | 3 per machine | 1 new site / 30 days, or an agency pool |
| Re-runs | 5 in total | unlimited |
| Blog → WordPress Posts | yes | yes |
| Shop → WooCommerce | no | yes |
| Visual Edit Pro plugin | — | delivered with every conversion |
| The theme itself | identical | identical |

**The licence never buys fidelity.** The theme a free conversion produces is
the same theme a licensed one produces — it buys page headroom, re-runs,
WooCommerce and the editor.

Licences: **[html2wp.dev/licenses](https://html2wp.dev/licenses)**

### Using a licence key

Written once per machine, before you start — see [Convert a Lovable
project](#convert-a-lovable-project) above. `H2WP_KEY` works too and takes
precedence over the file; the file exists so the key never lands in your
shell history.

The key is sent when the conversion reaches the service, which is well into
the run — but set it up beforehand anyway. The page allowance is checked at
the very first stage, so without a key in place the conversion is *planned*
for five pages, and finding the key later does not unplan that.

## Editing the converted site

The theme is **standalone**: pages, blog, forms, menus, SEO and redirects are
the theme's own code and work with no plugin at all.

For point-and-click editing there is **Visual Edit**, a WordPress plugin that
lets you (or your client) edit text, images, links, forms and menus by
clicking them on the page, while keeping the markup 1:1 with the design.

- **Free edition** — click-to-edit content editing, forms, menus, SEO fields.
- **Pro** — adds AI chat editing (with your own API key), theme export,
  Turnstile spam protection, and a 300-step edit history instead of 10.

Pro is delivered with every licensed conversion, and activating its Pro
features on a site needs a Visual Edit Pro licence.

Details: **[html2wp.dev/visualedit](https://html2wp.dev/visualedit)**

## Status

**Beta.** The gates catch most fidelity drift, not all of it — the last stage
of every conversion is your own page-by-page review, and the skill walks you
through it. When the generator itself is wrong, report it:

```bash
curl -sS -X POST https://api.html2wp.dev/v1/report \
  -H 'content-type: application/json' \
  -d '{"subject":"what went wrong","body":"what you saw","evidence":"page keys, warnings"}'
```

A person reads those, and fixes ship as service updates — so a defect you
report is fixed for everyone.

**Found a security problem instead?** That one does not go here and does not
go in a GitHub issue — see [SECURITY.md](SECURITY.md).

What is uploaded, what is reported back, and how long either is kept:
[PRIVACY.md](PRIVACY.md).

## Licence

Source-available, not open source — the full text is in [LICENSE](LICENSE).
The short version:

- **Free without a key for anything you are not paid for** — trying it,
  personal sites, learning, teaching.
- **Commercial use needs a key.** Converting for a client, or for a business
  you run, is commercial. With a key that is fully permitted: charge what you
  like, deliver what you like, run an agency on it — the agency licences exist
  for exactly that.
- **Your output is yours, unconditionally.** A theme this produces from your
  design carries no obligation to us — use it, sell it, hand it to a client,
  publish it. That does not depend on holding a key and it survives everything
  else in the licence. What we cannot hand you is what was never ours: a
  licensed font, a stock photo or a third-party library that came in with your
  input keeps its own terms on the way out.
- **What you may not do** is republish the skill, or put the conversion
  technology into someone else's hands — a service, SaaS, API, plugin, skill,
  CLI or platform built out of it, by hand or by pointing an AI at it. The
  line is between the work and the tool: handing a client the finished theme
  is yes, handing them something that does the converting is no.

Reading and running this with an AI assistant is not the restricted case —
that is what it is written for.

If you want to do something the licence does not cover, ask —
hello@html2wp.dev. Non-profits, schools and open source projects: say what you
are doing — a complimentary licence is usually available. Ask rather than
assume: a non-profit can still carry on economic activity, so the definition
can catch one, and the answer is a decision rather than a reading of it.

## Links

- Documentation — [html2wp.dev/docs](https://html2wp.dev/docs)
- Visual Edit plugin — [html2wp.dev/visualedit](https://html2wp.dev/visualedit)
- Licences — [html2wp.dev/licenses](https://html2wp.dev/licenses)
- Done-for-you conversions — [24design.eu](https://24design.eu/)
