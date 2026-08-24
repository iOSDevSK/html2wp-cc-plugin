# conversion-manifest.json — the contract every stage reads

Written once at the end of Stage 0 (script proposes via `analysis.json`, AI
finalizes with judgment, user confirms the summary). Every later script takes
`--manifest <path>` and trusts it. Nothing downstream re-derives what the
manifest already decided — one source of truth, or stages drift.

All paths are relative to the manifest's own directory (the conversion
workspace) unless absolute.

```jsonc
{
  "schema": "html2wp/1",

  "site": {
    "name": "Clara Hayes",             // display name → theme Name, wizard copy
    "slug": "clara-hayes",             // theme dir + textdomain + pattern namespace
    "prefix": "clara_hayes",           // PHP function prefix (slug with _)
    "home": "https://clarahayes.co",   // "" if unknown; feeds --home portability token
    "description": "…"                 // optional exact source copy; front SEO fallback when its head has no description
  },

  "input": {
    "dir": "/abs/path/to/html",        // the flat HTML directory being converted
    "type": "static-html",             // static-html | built-dist | astro-project
    "devHosts": ["clarahayes.test:8081"] // absolute-URL hosts to rewrite to relative
  },

  "workspace": "/abs/path/workspace",  // where astro-project/, theme/, reports/ land

  // One entry per page. `key` follows the plugin convention:
  // index.html → front-page, everything else → slugified stem.
  "pages": [
    {
      "file": "index.html",
      "key": "front-page",
      "kind": "front",                 // front | page | listing | article | shop | product | utility | fragment
                                       // `fragment` is the one kind that does NOT count against the
                                       // page allowance — it is markup the site reuses, not a page.
      // listing/article belong to the BLOG; shop/product to the SHOP. They are
      // separate kinds because they become different things: an article
      // becomes a Post, a product becomes a WooCommerce product, and a page
      // marked "listing" hosts [wp-posts] while "shop" hosts [wp-products].
      // Calling a product page "article" imports a jumper as a blog post.
      "title": "Clara Hayes | …",      // full <title>
      "chrome": "self-contained"       // consensus | self-contained
      // self-contained ⇒ stage 1 copies the page into public/ verbatim and
      // stage 3 gives it templates/page-{key}.html with NO header/footer
      // part; consensus ⇒ head/body split with chrome markers.
    },
    { "file": "about.html", "key": "about", "kind": "page", "title": "About | …", "chrome": "consensus" }
  ],

  // Chrome = everything outside <main> that repeats across consensus pages.
  // ONE merged object — every consumer reads its own field from the same
  // block, so `selector` and `canonicalFrom` COEXIST on header/footer. This
  // used to be shown without the selectors, and following that example
  // verbatim made capture-chrome.py fall back to bare element defaults.
  // Who reads what:
  //   header.selector        capture-chrome.py (default: "header")
  //   header.canonicalFrom   verify-parity.mjs (default: pages[0].file) —
  //                          a capture hint, never a reason to flatten
  //   footer.selector        make-theme.mjs (when trailing[] is absent) AND
  //                          the service, which derives
  //                          stripInFront: [chrome.footer.selector] whenever
  //                          frontOwnsFooter === false
  //   trailing[].selectors   capture-chrome.py + make-theme.mjs (the full
  //                          ordered form; footer.selector is its shorthand)
  //   frontOwnsFooter        make-theme.mjs + the service
  "chrome": {
    // Leading body nodes (usually one <header>).
    "header": {
      "selector": "header.site-header",// what the capture addresses
      "canonicalFrom": "about.html",   // which file supplies the markup
      "variants": 2,                   // how many distinct versions existed
      "variance": [                    // REPORTED, never silently flattened
        "is-sticky present on 12/17 pages — canonical keeps it",
        "404.html footer blurb truncated — canonical uses the full sentence"
      ]
    },
    // Trailing body nodes IN ORDER — can be several siblings.
    "trailing": [
      { "component": "SiteFooter", "selectors": ["footer.site-footer"] },
      { "component": "SiteDrawer", "selectors": ["div.drawer-veil", "aside.drawer"] }
    ],
    "footer": { "selector": "footer.site-footer", "canonicalFrom": "about.html", "variants": 4, "variance": ["…"] },
    "frontOwnsFooter": true
  },

  // NOTE: recorded for the report only — no script reads these. Heads are
  // per-page verbatim now (there is no shared layout and no head
  // parameterisation), which is what makes byte-exact output possible.
  "head": {
    "props": ["title", "description"],
    "sharedRaw": true
  },

  "blog": {
    "present": true,
    "listing": "journal.html",         // the page that becomes the [wp-posts] host
    "articles": ["hook-comes-first.html", "…"], // pages that become WP Posts
    "cardContainer": ".journal-grid",  // where the [wp-posts] token goes
    // The repeating card INSIDE that container. Defaults to `a.post-card`,
    // which is one site's class name: a design spelling its card anything else
    // yields zero cards, the listing is left static, and nothing catches it
    // until make-zip refuses the theme at the very end. Name it here — a
    // utility-class design (Tailwind, Lovable, v0) always needs to.
    "cardSelector": "a.post-card",
    // The category chip INSIDE that card, when the design does not spell it
    // the one way this stage recognises (a `p.meta` with a `span.dot` beside
    // it). Named rather than guessed: "the first word-bearing element before
    // the dateline" is the category on some cards and the author, a card
    // number or a reading time on others, and replacing one of those turns a
    // visible defect into a quieter one. Unnamed and unrecognised, stage 4.5
    // WARNS and every card prints the specimen article's category.
    "cardCategory": "span.chip",
    // Optional: name the article page's layout region and prose host when
    // they are not <main>/<article>. Selector grammar: "tag", "tag.class"
    // (several classes per segment), or a direct `>` path; articleBody is
    // resolved RELATIVE to the selected articleMain. make-theme (the derived
    // part) and dist-to-bundle (the imported post content) read the SAME
    // region — naming it here fixes both at once. Mandatory in practice on a
    // site whose <main> wraps the whole document (header and footer
    // included): the <main> fallback then imports the entire page into every
    // post and WordPress renders the site a second time inside the prose.
    "articleMain": "main > div.article-shell",
    "articleBody": "article.prose",
    // The category chip in the byline, when the generator cannot recognise
    // it — its built-in test is two Tailwind utilities, which describes one
    // design rather than a chip. Named here it becomes a per-post fragment
    // ({name}/{url}); unnamed and unrecognised, make-theme WARNS, because the
    // silent outcome is every post publishing the source article's category.
    "articleCategory": "a.dl-cat",
    // The prev/next pair, when it is not the ordinary shape (a container of
    // exactly two links, both to pages this manifest marks kind:"article").
    // Left unplaced, every post offers the source article's two neighbours.
    "articleNav": "nav.article-nav"
  },
  // blog.present=false ⇒ no home/index/search templates, article part derived
  // from the subpage design as a plain default. It also REQUIRES a reason:
  //   "blog": { "present": false, "reason": "the listing is a changelog" }
  // A listing that IS a blog gets wired however few articles it has — one
  // card is truer than six leading nowhere — so the reason must say why this
  // listing is not a blog, never how many articles it has. make-theme
  // refuses both a missing reason and a count-based one.

  // The shop is to WooCommerce what `blog` is to Posts, and it is deliberately
  // the SAME SHAPE: a listing whose own card markup becomes a [wp-products]
  // token, product pages that stop being pages and become records, and a
  // single-product part DERIVED from the site's own product page. Everything
  // a shopper does after "add to cart" — cart, checkout, stock, payment,
  // e-mail, accounts — belongs to WooCommerce and is not converted.
  //
  // ABSENT OR present:false ⇒ NOTHING about the theme changes. No shop
  // runtime class is emitted, no importer, no woocommerce theme support, no
  // single-product templates, no products.json, and stage 4.6 does not run.
  // A non-shop conversion's output is byte-identical to one produced before
  // the shop stage existed — enforced by test-shop-noop.sh.
  "shop": {
    "present": true,
    "listing": "shop.html",            // the page that becomes the [wp-products] host
    // Every page that renders the shop listing, the host included — page 2, a
    // category tab with its own address. Mirrors blog.listingPages, which
    // verify-wp.py reads to know how many listings a gate must find.
    "listingPages": ["shop.html"],
    // Pages that become WooCommerce products. Marked kind:"product", excluded
    // from the bundle (a product that is ALSO a page makes two things claim
    // one slug), and redirected to the product permalink.
    "products": ["product-cable-knit-sweater.html", "…"],
    "cardContainer": ".product-grid",  // where the [wp-products] token goes
    // The repeating card INSIDE that container. The blog stage hardcodes
    // `a.post-card` and pays for it on every site that spells it otherwise;
    // name it here and the tokenizer finds the design's real card. Falls back
    // to the container's first element child when omitted.
    "cardSelector": "a.product-card",
    // The product page's layout region and its prose host — same contract and
    // same failure as blog.articleMain/articleBody: make-theme (the derived
    // part) and build-products (the imported description) read the SAME
    // region, and a <main> that wraps the whole document imports the header
    // and footer into every product.
    "productMain": "main > div.product-shell",
    "productBody": "div.product-description",
    // Where the money, the pictures and the buy button are. Each becomes a
    // [wp-product field="…"] in the derived part; each left unnamed and
    // unrecognised is WARNED about, because the silent outcome is every
    // product publishing the specimen product's price.
    // RESOLVED INSIDE productMain, and the region's own tag+class is NOT
    // available to them: make-theme demotes productMain's classes onto a
    // wrapper <div> (WordPress renders its own <main> around the part), so a
    // selector that names the region again — "section.grid > div" when
    // productMain IS section.grid — matches nothing, the field is silently
    // unplaced, and every product page publishes the specimen's value. Address
    // the region's CONTENT: "div > div" for its first column, or a class that
    // only the target carries.
    "productPrice": "span.price",
    "productGallery": "div.product-gallery",
    "productCategory": "p.product-cat",
    // The buy region: whatever wraps the size/variant chooser, the quantity
    // stepper and the button. It becomes [wp-product field="add-to-cart"],
    // which emits a REAL WooCommerce form wearing this markup. Unnamed, the
    // derivation looks for a button whose text matches /add to (cart|bag)/i
    // and takes its nearest form-ish ancestor; failing that the product page
    // renders beautifully and sells nothing, which is why it warns loudly.
    "addToCart": "div.product-buy",
    "variantAttribute": "Size",        // the label of the chooser's attribute
    // The bag/cart affordance in the CHROME (header). Its count becomes
    // [wp-cart-count], so the number moves when the cart does.
    "cartLink": "a.cart-link",
    "cartCount": "span.cart-count",
    // Pages that become WooCommerce's own cart/checkout. Excluded from the
    // bundle and redirected — deliberately NOT converted 1:1, because the
    // machinery behind them is Woo's and a pixel-perfect copy of a demo
    // checkout is a checkout that takes no money.
    // The cart/checkout pages ALSO appear in pages[] as kind:"utility" (the
    // shop-site fixture is the reference), and utility pages COUNT against
    // the page allowance — only kind:"fragment" does not. checkoutPage is
    // optional: a cart-only site simply omits it, every consumer tolerates
    // its absence.
    "cartPage": "cart.html",
    "checkoutPage": "checkout.html",
    // Query-string category links (/shop?category=Knitwear) cannot be
    // redirected — redirects.json is keyed by PATH. Named here, build-products
    // rewrites them in the stored sources to the real archive address
    // (/product-category/knitwear/) instead.
    "categoryQueryParam": "category"
  },
  // shop.present=false ⇒ REQUIRES a reason, and the reason must say why this
  //   is not a shop, never how many products it has:
  //   "shop": { "present": false, "reason": "the catalogue is a lookbook — no prices, no basket" }
  // A catalogue that IS a shop gets wired however few products it has.

  "forms": [
    { "page": "contact.html", "selector": "form.editorial-form", "purpose": "contact" }
  ],

  "collections": [                     // repeating card sets worth noting in the report
    { "page": "index.html", "selector": ".service-index", "count": 3 }
  ],

  // Repeated groups a REVIEWER confirmed are one editable list after the
  // editor's own congruence rules refused them (one member is an authored
  // design variant — the highlighted pricing tier). make-theme and
  // dist-to-bundle stamp `data-cve-class` (the shared classes) onto the
  // members in every GENERATED artifact — theme parts, front-page pattern,
  // stored page sources — via lib/collection-stamp.mjs, so the editor offers
  // the group while each member keeps its own class and renders as designed.
  // A group that cannot be located, or shares too little, is reported and
  // the theme still ships.
  "declaredCollections": [
    { "page": "pricing.html", "parentTag": "div", "parentClasses": "tiers",
      "tag": "div", "count": 3 }
  ],

  "design": {
    "tokenSource": "subpages.css",     // file whose :root {--*} feeds the palette
    "palette": [ { "slug": "ink", "color": "#1a1a16", "var": "--ink" } ],
    "fonts": [                         // families only; loading stays as the source does it
      { "slug": "heading", "family": "\"Noto Serif Display\", Georgia, serif" },
      { "slug": "body",    "family": "\"DM Sans\", sans-serif" }
    ],
    "contentWidth": "1320px",          // the design's real container width → contentSize decision
    // Informational: stage 3 re-derives head externals from a real built
    // page rather than trusting this list. Keep it accurate anyway — it is
    // what a reviewer checks the generated enqueue against.
    "headExternals": [
      "https://fonts.googleapis.com/css2?…",
      "subpages.css"
    ]
  },

  // Shape anchors: per-key substrings validate_shape() will enforce.
  // MUST be picked from the site's real markup and verified to exist —
  // the plugin refuses saves without them.
  "anchors": {
    "front-page": "class=\"hero\"",
    "header": "site-header",
    "footer": "site-footer",
    "404": "utility"
  },

  // Menu management: ONE ENTRY PER NAVIGATION GROUP, however many the site
  // has and wherever they sit — header nav, mobile drawer, footer columns, a
  // sidebar. Finalized by the AI from analysis.json's navGroups (which is
  // structural detection: <nav> elements, link lists, runs of sibling links
  // — never class names). Reject non-menus (an instagram grid is links but
  // not a menu; navGroups' allTextual:false is the usual tell). Each entry:
  //
  //   selector — "tag.class" (or bare "tag"), matching EXACTLY ONE element
  //              per page it appears on; the plugin's zone matcher and the
  //              bridge's el.closest() both address the zone by it
  //   region   — header | footer | body (informational + label derivation)
  //   label    — the WP menu's human name
  //   links    — the group's own links, in document order ({ text, href });
  //              hrefs as written in the source (about.html), the bundler
  //              resolves them to page keys
  //
  // Groups with IDENTICAL link targets (the desktop nav and its drawer)
  // stay separate entries — each needs its own zone selector — and the
  // bundler collapses them into one WP menu assigned to all their
  // locations. The menu location slug is derived per entry as
  // "{site.prefix}_nav_{index+1}" by both make-theme.mjs and
  // dist-to-bundle.mjs; order in this list is therefore contract, not
  // cosmetics. An entry the theme cannot wire fails the build; a group
  // detected but deliberately not managed belongs in the conversion report.
  //
  // The selector is BEST EFFORT (real sites' classes are often shared
  // utilities or not valid CSS): make-theme locates each group (selector
  // first, link sequence second — lib/nav-stamp.mjs), stamps it with
  // data-ve-nav="{index+1}" in every generated artifact and bundle source,
  // declares `[data-ve-nav="{index+1}"]` in the theme contract, and writes
  // that back here as `zoneSelector` — the field the gates then use.
  "nav": [
    { "selector": "nav.nav-links",  "region": "header", "label": "Main navigation",
      "links": [ { "text": "Home", "href": "index.html" }, { "text": "About", "href": "about.html" } ] },
    { "selector": "nav.drawer-nav", "region": "body",   "label": "Main navigation (drawer)",
      "links": [ { "text": "Home", "href": "index.html" }, { "text": "About", "href": "about.html" } ] }
  ],

  "utilityPages": { "404": "404.html", "thanks": "form-submitted.html" }
}
```

## Rules the schema encodes

- `pages[].key` collisions are a Stage-0 **error**, never resolved silently.
- Every `variance` entry is surfaced in the final conversion report.
- `shop.products[]` is the DECLARED catalogue, and every gate counts against
  it rather than against whatever the input happened to contain. A shop whose
  listing paginates client-side (a "Load more" button) prerenders with only
  the first page of products linked, so a stage that trusted the crawl would
  build eight products out of twelve, report eight, and pass — the shortfall
  is invisible downstream because nothing else knows the number. Every entry
  must exist in `pages[]` as `kind: "product"`; a declared product with no
  page fails `build-products.mjs`.
- `anchors` values are verified against the actual generated parts/pattern
  before the theme is written — a missing anchor fails `make-theme.mjs`,
  and an anchor that exists but is fragile (editorial copy rather than a
  class/id/data-attribute, too short, or non-unique) is warned about.

### What each field is actually used for

Consumed by a script:
`site.*` (incl. optional `author`, `authorUri`), `input.dir`, `workspace`,
`pages[].{file,key,chrome}`, `pages[].kind` (picks the head-sample page),
`chrome.header.canonicalFrom`, `chrome.footer.canonicalFrom`,
`chrome.trailing[]`, `chrome.frontOwnsFooter`, `design.{palette,fonts,
contentWidth}`, `blog.{present,listing,articles,cardContainer,cardSelector,cardCategory,articleMain,articleBody,articleCategory,articleNav}`,
`shop.{present,reason,listing,listingPages,products,cardContainer,cardSelector,
productMain,productBody,productPrice,productGallery,productCategory,addToCart,
variantAttribute,cartLink,cartCount,cartPage,checkoutPage,categoryQueryParam}`
(build-products.mjs → the token + products.json; make-theme.mjs → the derived
product part, the shop runtime class, the WooCommerce theme support;
dist-to-bundle.mjs → the product/cart/checkout exclusions and their redirects;
verify-wp.py → gate C6; make-zip.sh → the declared-vs-imported product count),
`declaredCollections` (make-theme.mjs + dist-to-bundle.mjs → data-cve-class
stamping), `anchors`,
`nav[]` (make-theme.mjs → theme contract + menu locations; dist-to-bundle.mjs
→ menus.json; verify-wp.py → the menus-wired gate), `utilityPages.404`.

`forms` is read by the VERIFIERS, not the generator: smoke-editor.py drives
the connect→submit→disconnect cycle from it, and verify-parity.mjs reads its
field names. No stage wires a form for you. A form ships as the design's own
markup and becomes live one of two ways: the owner clicks Connect in Visual
Edit (which writes the `[wp-form]` token into the stored source), or you wrap
it in `[wp-form]` by hand in `clara-content/sources/<page>.html` — the
standalone runtime renders and handles it with no plugin. The smoke test ends
DISCONNECTED on purpose (it restores the pristine state), so if the owner
wants the form live on day one, connect it as the last step before handover
and say so in the report.

Recorded for the report / the human, read by nothing today:
`input.type`, `input.devHosts`, `head.*`, `design.tokenSource`,
`design.headExternals`, `pages[].title`, `collections`.
Keep them truthful — the conversion report quotes them — but do not expect
a stage to act on them.
- `design.headExternals` exists because the old bundler silently dropped
  head `<link>`/`<script src>` (the font-drop bug) — anything listed here
  must be accounted for: kept in the fragment, or transplanted into the
  theme's enqueue, never lost.
- `input.devHosts` drives the host-rewrite pass (share links pointing at a
  dev WP host shipped once; never again).
