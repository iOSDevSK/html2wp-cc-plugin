"""What a blog's pages SHOW, measured the same way on the source and on WordPress.

The pixel gate cannot see a blog: the listing is driven by posts
(dynamic-listing) and a post renders through the single template
(post-via-single-template), so both are excluded from the pixel comparison,
and the semantic check (C3) only matched titles and counts. A conversion whose
images were never fetched therefore passed: every post without its hero, every
listing card an empty frame with a title in it.

This measures, per page, the article's images and text, and per listing card
its image, title and excerpt. verify-wp.py compares the source's numbers with
WordPress's: what the source shows, WordPress must show too.
"""

# An image that counts as CONTENT: visible and big enough to be a picture
# rather than an icon, avatar or logo mark, and not in the page's chrome.
# "Broken" is an image the page shows a frame for but has no pixels for: its
# source failed, or it has no source at all. A PICTURE also has pixels of its
# own: a 1x1 spacer stretched to the frame decodes fine and paints nothing —
# it is what a theme renders for a post with no featured image, so it is
# exactly W's case (images never fetched) and must not count as the image.
ARTICLE_JS = r"""() => {
  const chrome = (e) => !!e.closest('nav, footer, [data-ve-nav]')
    || (() => { const h = e.closest('header'); return !!h && !h.closest('main, article'); })();
  const scope = document.querySelector('main') || document.body;
  const imgs = [...scope.querySelectorAll('img')].filter((i) => !chrome(i));
  const box = (i) => i.getBoundingClientRect();
  const shown = imgs.filter((i) => { const r = box(i); return r.width >= 120 && r.height >= 80; });
  const picture = (i) => i.complete && i.naturalWidth > 4 && i.naturalHeight > 4;
  const loaded = shown.filter(picture);
  const broken = imgs.filter((i) => { const r = box(i); return r.width > 0 && r.height > 0 && i.complete && i.naturalWidth === 0; });
  const text = [...scope.querySelectorAll('p, li, blockquote, h2, h3')].filter((e) => !chrome(e))
    .map((e) => (e.innerText || '').trim()).join(' ').replace(/\s+/g, ' ');
  return { images: loaded.length, shown: shown.length, broken: broken.map((i) => i.currentSrc || i.getAttribute('src') || '(no src)').slice(0, 5),
           textLength: text.length };
}"""

CARDS_JS = r"""([container, card]) => {
  let grid = null;
  try { grid = (document.querySelector('main') || document).querySelector(container) || document.querySelector(container); } catch (e) { return null; }
  if (!grid) return null;
  let cards = [];
  try { cards = card ? [...grid.querySelectorAll(card)] : [...grid.children]; } catch (e) { cards = [...grid.children]; }
  if (card && !cards.length) cards = [...grid.children];
  const said = (e) => (e && (e.innerText || '').trim()) || '';
  return cards.map((c) => {
    const imgs = [...c.querySelectorAll('img')];
    const pictured = imgs.filter((i) => { const r = i.getBoundingClientRect(); return r.width >= 60 && r.height >= 40; });
    const heading = c.querySelector('h1, h2, h3, h4, h5, h6');
    const title = said(heading) || [...c.querySelectorAll('a')].map(said).sort((a, b) => b.length - a.length)[0] || '';
    const excerpt = [...c.querySelectorAll('p')].map(said).filter((t) => t.length >= 25 && t !== title)[0] || '';
    return {
      image: pictured.some((i) => i.complete && i.naturalWidth > 4 && i.naturalHeight > 4),
      broken: imgs.some((i) => { const r = i.getBoundingClientRect(); return r.width > 0 && r.height > 0 && i.complete && i.naturalWidth === 0; }),
      title: title.slice(0, 80),
      excerpt: excerpt.length > 0,
    };
  });
}"""


def article_media(page):
    return page.evaluate(ARTICLE_JS)


def listing_cards(page, container, card_selector=None):
    """One row per card, or None when the container is not on the page."""
    return page.evaluate(CARDS_JS, [container, card_selector or ""])


def article_problems(source, live, featured_media=None):
    """What the live post lost against its source article. [] when nothing."""
    problems = []
    if live["broken"]:
        problems.append(f"broken image(s): {', '.join(live['broken'])}")
    if source["images"] and not live["images"]:
        problems.append(f"the source article shows {source['images']} image(s), the post shows none"
                        + (" (and has no featured image)" if featured_media == 0 else ""))
    if source["textLength"] >= 200 and live["textLength"] < source["textLength"] * 0.5:
        problems.append(f"the post shows {live['textLength']} characters of text where the source article has {source['textLength']}")
    return problems


def card_problems(source_cards, live_cards):
    """What the live listing's cards lost against the source's. [] when nothing.

    A feature is required of every live card when at least half the source's
    cards carried it: a designed grid shows the same shape of card throughout,
    and one odd source card must not make the rule."""
    if not source_cards or live_cards is None:
        return []
    problems = []
    for feature, label in (("image", "an image"), ("title", "a title"), ("excerpt", "an excerpt")):
        had = sum(1 for c in source_cards if c[feature])
        if had * 2 < len(source_cards):
            continue
        missing = [i + 1 for i, c in enumerate(live_cards) if not c[feature]]
        if missing:
            problems.append(f"card(s) {missing} show no {feature} where {had} of {len(source_cards)} source cards had {label}")
    broken = [i + 1 for i, c in enumerate(live_cards) if c["broken"]]
    if broken:
        problems.append(f"card(s) {broken} show a broken image")
    return problems
