# What html2wp does with your data

Plain description of what leaves your machine, why, and how long it is kept.
The README says the same things in passing; this is the version you can point
a client at.

**BELNEM s.r.o.**, Beckovska 5, Bratislava, IČO 53713486 — hello@html2wp.dev

---

## The conversion itself

To convert a site, the service has to receive it. When you run stage 3, the
skill uploads **one archive** to `api.html2wp.dev`:

- `conversion-manifest.json` — the decisions you made about the site
- `astro-report.json` — the chrome inventory from stage 1
- `astro-project/`, including `dist/` — **the built site**
- `chrome-at-rest/`, `chrome-groups.json`, `style-specimens/` when they exist

That is your client's website: its markup, styles, scripts and images. There
is no way to convert a site without sending it, and any wording suggesting
otherwise would be false. `SKILL.md` lists the exact members under *What goes
up*, and `convert-remote.sh` is a readable shell script if you would rather
check than take our word for it.

**What is NOT in that archive:** your input directory (only the built output
travels), `node_modules`, and — since the payload filter — anything shaped
like a credential: `.env` and `.env.*`, `*.pem`, `*.key`, `id_rsa`,
`credentials.json`, `.npmrc`, `.netrc`, `.ssh/`, `.aws/`, plus any file whose
contents contain a private key block or an obvious secret assignment.
Everything the filter drops is listed by name in `astro-report.json`, so you
can see what did not travel.

**How long it is kept:** the workspace is deleted **7 days** after it stops
changing. What survives is a digest — a hash — which is what lets a re-run of
the same site count as a re-run rather than a second conversion.

## The gate results

After you verify a conversion locally, the skill reports the outcome. This is
required: the next conversion is refused until the previous one has reported.
It is also small, and it is a whitelist rather than a filter:

- **Sent:** gate names, pass / fail / not-run, page counts, the worst fidelity
  percentage, and the page *keys* that failed — the short names you chose,
  like `about` or `pricing`.
- **Not sent:** no URL, no domain, no markup, no copy, no screenshots, no file
  paths, no licence key, no site name.

`send-verdicts.sh <workspace> --dry-run` prints the exact payload and sends
nothing.

Why it is required: the gates run on your machine, against a WordPress the
service never sees. Without them the service can tell that a conversion did
not crash and nothing whatsoever about whether it was *correct*. Kept **365
days**.

## The conversion record

One row per conversion: how many pages, how many chrome variants, whether it
had a blog or a shop, whether it was a re-run, which stage refused if one did,
and the generator's own warnings about itself. The warnings are redacted —
page filenames and URLs are replaced before the row is written. Kept **365
days**. No content, no addresses, no identifiers for the site.

## Defect reports

If you send one with `POST /v1/report`, the body is stored as you wrote it and
read by a person. Kept **90 days**. Do not paste anything into it that you
would not want kept for that long.

## Identifying you

- **Free tier:** counted per network address, as seen by Cloudflare. Stored as
  a truncated hash, never as an address.
- **Licensed:** by the licence key, also stored as a hash. Validated against
  UpdatePulse (`updates.designready.studio`), which sees the key.

## Who else is involved

- **Cloudflare** — sits in front of the API.
- **UpdatePulse** on our own server — licence validation.
- **Coolify** on our own server — hosting.

No analytics, no advertising, no third-party trackers. **The theme you receive
contacts no server of ours at runtime, ever** — that claim is about the
delivered site, and it is exact.

## Your rights

Ask us to delete what relates to you and we will: hello@html2wp.dev. In
practice most of it has already aged out — the site itself is gone after 7
days, and what remains is hashes and counts.

## Security

Vulnerability reports go to the address in [SECURITY.md](SECURITY.md), not to
a public issue.
