"""Things a browser check must not race: a login form, and a screenshot.

Both failed smoke-editor.py only under load (--jobs 3), which is when a page's
own timers and the browser's painting fall behind the script driving it.

fill_login(): WordPress's login page focuses #user_login from a 200 ms timer.
Playwright's fill() focuses the field and then inserts the text into whatever
has focus, so when the timer fired between the two, the PASSWORD went into
the user name field and the login failed on a correct password. The page's own
autofocus is waited out first, each field is filled by selector, and both
values are read back; a mismatch is cleared and filled again.

media_ready(): a screenshot taken after "the images decoded" can still be
taken before they are PAINTED — decode() resolving is not a frame — and a CSS
background photograph was never waited for at all. Before a capture: every
shown <img> has pixels of its own, every CSS background image has decoded,
fonts are ready, and two animation frames have passed. What is still pending
when the time runs out is returned, so the caller can recapture instead of
believing the shot.

reveal_all(): a page at rest has every reveal-on-scroll element revealed — a
visitor who scrolled it saw them all. A capture that scrolls through to load
images only triggers the ones the scroll happened to pass slowly enough, and a
full-page screenshot then catches a late one mid-fade: bruce-banner's last
blog card, on one capture in two, in the same 1.6% band every time. So before
a capture every recorded reveal ([data-spa-reveal], and a design's own
.reveal/.in hook) is put in its end state, on both sides of a comparison.
"""

MEDIA_READY_JS = r"""async (limit) => {
  const until = Date.now() + limit;
  const frame = () => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
  const shown = (e) => { const r = e.getBoundingClientRect(); const cs = getComputedStyle(e);
    return r.width > 1 && r.height > 1 && cs.visibility !== 'hidden' && cs.display !== 'none'; };
  const bgUrls = () => {
    const urls = new Set();
    for (const e of document.querySelectorAll('body *')) {
      const bg = getComputedStyle(e).backgroundImage;
      if (!bg || bg === 'none' || !shown(e)) continue;
      for (const m of bg.matchAll(/url\(["']?([^"')]+)["']?\)/g)) if (!m[1].startsWith('data:')) urls.add(m[1]);
    }
    return [...urls];
  };
  const bgDone = new Map();
  const loadBg = (u) => {
    if (!bgDone.has(u)) {
      const i = new Image(); i.src = u;
      bgDone.set(u, (i.decode ? i.decode() : Promise.resolve()).then(() => true, () => true));
    }
    return bgDone.get(u);
  };
  let pending = [];
  try { if (document.fonts && document.fonts.ready) await Promise.race([document.fonts.ready, new Promise((r) => setTimeout(r, limit))]); } catch (e) {}
  while (true) {
    const imgs = [...document.images].filter((i) => (i.getAttribute('src') || i.getAttribute('srcset')) && shown(i));
    imgs.forEach((i) => { if (i.loading === 'lazy') i.loading = 'eager'; });
    await Promise.race([
      Promise.all([...imgs.map((i) => (i.complete ? (i.decode ? i.decode().catch(() => null) : null)
        : new Promise((r) => { i.addEventListener('load', r, { once: true }); i.addEventListener('error', r, { once: true }); }))),
        ...bgUrls().map(loadBg)]),
      new Promise((r) => setTimeout(r, Math.max(0, Math.min(500, until - Date.now())))),
    ]);
    // An <img> whose load FAILED is complete with no pixels: that is the page,
    // not a race, and waiting will not change it — it is not "pending".
    pending = imgs.filter((i) => !i.complete).map((i) => (i.currentSrc || i.getAttribute('src') || '').slice(0, 120));
    if (!pending.length || Date.now() >= until) break;
  }
  await frame();
  return pending;
}"""

LOGIN_FOCUS_SETTLED_JS = r"""(sel) => new Promise((r) => {
  // WordPress focuses the user name field on a timer after load; wait for that
  // (or 600 ms) so it cannot move the focus while a field is being filled.
  const t0 = performance.now();
  const tick = () => {
    const done = document.activeElement && document.activeElement.matches(sel);
    if (done || performance.now() - t0 > 600) r(!!done); else setTimeout(tick, 25);
  };
  tick();
})"""


REVEAL_ALL_JS = r"""async () => {
  const els = [...document.querySelectorAll('[data-spa-reveal]')];
  els.forEach((e) => e.classList.add('spa-in', 'spa-done'));
  const own = [...document.querySelectorAll('.reveal:not(.in)')];
  own.forEach((e) => e.classList.add('in'));
  const running = document.getAnimations().filter((a) => a.effect && a.effect.getComputedTiming().iterations !== Infinity);
  await Promise.race([Promise.all(running.map((a) => a.finished.catch(() => null))), new Promise((r) => setTimeout(r, 3000))]);
  return els.length + own.length;
}"""


def reveal_all(page):
    """Every reveal-on-scroll element in its end state; returns how many were touched."""
    try:
        return page.evaluate(REVEAL_ALL_JS)
    except Exception:
        return 0


def media_ready(page, limit_ms=8000):
    """[] when every shown image and background has painted, else what is still loading."""
    try:
        return page.evaluate(MEDIA_READY_JS, limit_ms)
    except Exception as exc:  # a navigating page: report, never raise out of a capture
        return [f"(could not check: {type(exc).__name__})"]


def fill_login(page, user, password, timeout_ms=30000, attempts=3,
               user_sel="#user_login", pass_sel="#user_pass"):
    """Fill a login form so that each value lands in its own field, or raise."""
    page.wait_for_selector(pass_sel, timeout=timeout_ms)
    try:
        page.evaluate(LOGIN_FOCUS_SETTLED_JS, user_sel)
    except Exception:
        pass
    for attempt in range(1, attempts + 1):
        page.fill(user_sel, user, timeout=timeout_ms)
        page.fill(pass_sel, password, timeout=timeout_ms)
        got_user = page.input_value(user_sel, timeout=timeout_ms)
        got_pass = page.input_value(pass_sel, timeout=timeout_ms)
        if got_user == user and got_pass == password:
            return attempt
        page.fill(user_sel, "", timeout=timeout_ms)
        page.fill(pass_sel, "", timeout=timeout_ms)
        page.wait_for_timeout(150 * attempt)
    raise RuntimeError("login form: the values did not stay in their own fields after "
                       f"{attempts} attempts (a script on the page kept moving the focus)")
