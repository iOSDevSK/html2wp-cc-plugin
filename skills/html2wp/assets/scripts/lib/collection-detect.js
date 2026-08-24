// In-page collection detector — the ONE implementation shared by
// detect-collections.py (conversion time, against the built dist) and
// verify-wp.py's collections gate (against the live WordPress render).
//
// This is a faithful port of the Visual Edit plugin's own rules
// (wp-plugin/visual-edit/assets/bridge.js: shapeSignature,
// congruentSiblings, isContiguousCollectionRun, diffCollectionMembers).
// The point of running it at conversion time is to PROVE the plugin will
// offer every repeating group — so the rules must be the plugin's rules,
// not an approximation. If bridge.js's rules change, change this with them.
//
// Evaluated with page JavaScript DISABLED, so the DOM is the parsed markup
// itself — the same thing the plugin's path stamping addresses (bridge.js
// runs first and snapshots pristine classes for exactly this reason).
//
// Called as: fn({ excludeSelectors: [...] }) → [ { parentTag,
// parentClasses, memberShape, count, slots } ] — one entry per congruent,
// contiguous sibling run with 1..8 editable slots, in document order.
(config) => {
  var COLLECTION_ATTR_WHITELIST = { src: 1, srcset: 1, href: 1, alt: 1 };
  var COLLECTION_ATTR_IGNORE = {
    id: 1, 'aria-controls': 1, 'aria-labelledby': 1, 'aria-describedby': 1, for: 1,
    'aria-expanded': 1, 'aria-selected': 1, 'aria-hidden': 1, 'data-state': 1,
    hidden: 1, tabindex: 1,
    // A theme may mark only the first card in a dark section with this
    // render-state attribute; it is not item content or structure.
    'data-dark': 1,
    // The OPEN item of an accordion/tab set carries this and the closed ones
    // do not — same family as aria-expanded (bridge.js keeps this in step).
    'aria-disabled': 1,
    // WordPress core's wp_filter_content_tags() auto-injects these onto
    // rendered images ASYMMETRICALLY: fetchpriority="high" on the page's
    // first image, loading="lazy" only past the omit threshold. Without
    // ignoring them, any live-rendered image collection of 4+ items — or
    // any group containing the page's first image — fails congruence on
    // the WP side only, while the dist side (no injection) still detects.
    // Verified live on a 2-image gallery and a 5-badge strip.
    loading: 1, decoding: 1, fetchpriority: 1,
      // A designed list staggers itself. AOS's delay/duration and Swiper's
      // frozen slide index are values the DESIGN varies on purpose across
      // members of ONE set — the same statement the style normalisation
      // already makes — so reading them as design differences refuses the
      // owner every list that animates in. Measured: creative's 9-card
      // inspiration grid and 3-card blog strip (data-aos-delay 100/200/300),
      // dexler's two 4-slide testimonial carousels (data-swiper-slide-index).
      // bridge.js carries the same three since 1.19.8, and
      // test-congruence-parity.sh fails the moment the two lists drift.
      'data-aos-delay': 1, 'data-aos-duration': 1, 'data-swiper-slide-index': 1,
      // The same stagger, hand-rolled. A site with no reveal library writes
      // its own index — nothing on the first member, then 1, 2, 3 — and
      // styles the delay from it. It cost a whole site rather than one list:
      // on a photographer's pages the service cards, process steps, add-ons
      // and testimonials all carried it, so every repeated section on every
      // page was refused at once and the owner would have been offered none
      // of them. bridge.js carries it since 1.20.0; the parity test fails the
      // moment the two lists drift.
      'data-d': 1,
      // Image geometry and responsive plumbing — width/height differ the
      // moment a strip mixes portrait and landscape, and `sizes` sits only
      // on the images that earned a srcset variant. None is item content;
      // src and alt stay whitelisted slot values. Mirrors bridge.js 1.20.1.
      width: 1, height: 1, sizes: 1,
  };
  var MAX_SLOTS = 8;
  var excluded = (config.excludeSelectors || []).filter(Boolean).join(', ');

  function classBaseForCongruence(cls) {
    var base = cls;
    var start = 0;
    while (start < base.length) {
      var depth = 0;
      var separator = -1;
      for (var i = 0; i < base.length; i++) {
        if ('[' === base[i]) {
          depth++;
        } else if (']' === base[i] && depth > 0) {
          depth--;
        } else if (':' === base[i] && 0 === depth) {
          separator = i;
          break;
        }
      }
      if (separator < 0) {
        break;
      }
      base = base.slice(separator + 1);
      start++;
    }
    return base;
  }
  function isPositionInSetUtility(cls) {
    var base = classBaseForCongruence(cls).replace(/!$/, '');
    return /^-?(?:col|row)-(?:span|start|end)(?:-|$)/.test(base)
      || /^-?order(?:-|$)/.test(base)
      || /^z-(?:auto|0|10|20|30|40|50)(?:-|$)/.test(base)
      || /^overflow-(?:auto|hidden|clip|visible|scroll)(?:-|$)/.test(base)
      || /^rounded-(?:t|r|b|l|s|e|tl|tr|br|bl|ss|se|ee|es)(?:-|$)/.test(base)
      || /^border(?:-|$)/.test(base)
      || /^p(?:t|r|b|l|s|e)(?:-|$)/.test(base)
      || /^\[(?:animation-delay|animation-duration):[^\]]+\]$/.test(base);
  }
  function classListSorted(el) {
    var c = el.hasAttribute('data-cve-class')
      ? el.getAttribute('data-cve-class')
      : (el.className && typeof el.className === 'string' ? el.className : '');
    c = (c || '').trim();
    if (!c) {
      return [];
    }
    // Swiper writes its CURRENT position onto whichever slides happen to be
    // active/prev/next when the page is exported, so one member of an
    // otherwise identical set carries a class the others do not. That is
    // runtime state frozen into markup, not a different design. Bare
    // `swiper-slide` stays — it IS the structure.
    var classes = c.split(/\s+/).filter(function (cls) {
        return !/^swiper-slide-(active|prev|next|duplicate)/.test(cls)
          && !isPositionInSetUtility(cls);
      });
    return classes.sort();
  }
  function childTagSequence(el) {
    var out = [];
    for (var i = 0; i < el.children.length; i++) {
      out.push(el.children[i].tagName.toLowerCase());
    }
    // Comparison tables use one tbody per feature group, with a different
    // number of feature rows in each group. The body remains the repeatable
    // unit, so its row count is not part of its design shape.
    if ('TBODY' === el.tagName && out.length && out.every(function (tag) {
      return 'tr' === tag;
    })) {
      return ['tr*'];
    }
    return out;
  }
  function shapeSignature(el) {
    return el.tagName.toLowerCase() + '|' + classListSorted(el).join(',') + '|' + childTagSequence(el).join(',');
  }
  // Mirrors bridge.js 1.20.2: a decorative separator carries nothing an owner
  // could edit — punctuation, not content. See the bridge for the full
  // reasoning; the prose protection (headings, images between paragraphs)
  // lives in the tests on both sides.
  function isDecorativeSeparator(el) {
    if (el.children.length || (el.textContent || '').trim()) {
      return false;
    }
    if (/^(IMG|VIDEO|IFRAME|EMBED|OBJECT|INPUT|SVG|PICTURE|SOURCE|CANVAS)$/.test(el.tagName)) {
      return false;
    }
    return !el.hasAttribute('src') && !el.hasAttribute('href');
  }
  function collectionSeparators(parent, members) {
    var seps = [];
    for (var i = 0; i < parent.children.length; i++) {
      if (members.indexOf(parent.children[i]) === -1) {
        seps.push(parent.children[i]);
      }
    }
    if (!seps.length) {
      return null;
    }
    var sig = shapeSignature(seps[0]);
    for (var s = 0; s < seps.length; s++) {
      if (!isDecorativeSeparator(seps[s]) || shapeSignature(seps[s]) !== sig) {
        return null;
      }
    }
    return seps;
  }
  function collectionRuns(parent, members) {
    var kids = parent.children;
    var homogeneous = true;
    for (var tagIndex = 1; tagIndex < kids.length; tagIndex++) {
      if (kids[tagIndex].tagName !== kids[0].tagName) {
        homogeneous = false;
        break;
      }
    }
    var runs = [];
    var run = [];
    for (var i = 0; i < kids.length; i++) {
      if (members.indexOf(kids[i]) !== -1) {
        run.push(kids[i]);
      } else if (run.length) {
        runs.push(run);
        run = [];
      }
    }
    if (run.length) {
      runs.push(run);
    }
    if (homogeneous) {
      return runs;
    }
    if (runs.length > 1 && members.length >= 2 && collectionSeparators(parent, members)) {
      var merged = [];
      for (var r = 0; r < runs.length; r++) {
        merged.push.apply(merged, runs[r]);
      }
      return [merged];
    }
    return 1 === runs.length ? runs : [];
  }
  // An inline style compared for CONGRUENCE, with the component's own runtime
  // bookkeeping removed. A JS-driven widget writes measured sizes into CSS
  // custom properties and flips animation flags on whichever item is open, so
  // a static export freezes those onto ONE member of an identical set
  // (`--radix-collapsible-content-height: 72px; animation-name: none` on the
  // open FAQ answer, nothing on the closed four). Compared verbatim that reads
  // as a design difference and the group stops being a collection — which is
  // how a five-question FAQ was never offered for editing. Authored inline
  // style still counts; only custom properties and animation/transition state
  // are dropped. Mirrors bridge.js's normalizeStyleForCongruence.
  function normalizeStyleForCongruence(value) {
    return String(value || '').split(';').map(function (decl) {
      decl = decl.trim();
      var colon = decl.indexOf(':');
      if (colon < 0) {
        return decl;
      }
      return decl.slice(0, colon).trim().toLowerCase() + ':' + decl.slice(colon + 1).trim();
    }).filter(function (decl) {
      if (!decl) {
        return false;
      }
      var prop = decl.split(':')[0].trim().toLowerCase();
      return 0 !== prop.indexOf('--')
        && 'opacity' !== prop
        && 'transform' !== prop
        && 'animation-name' !== prop
        && 'animation-duration' !== prop
        && 'transition-duration' !== prop;
      }).sort().join(';');
  }
  function collectionAttrValue(el, name) {
    if ('class' === name) {
      return classListSorted(el).join(' ');
    }
    if ('style' === name) {
      return normalizeStyleForCongruence(el.getAttribute('style'));
    }
    return el.getAttribute(name) || '';
  }
  function collectionAttrsCongruent(a, b) {
    var seen = {};
    var i;
    for (i = 0; i < a.attributes.length; i++) {
      seen[a.attributes[i].name] = 1;
    }
    for (i = 0; i < b.attributes.length; i++) {
      seen[b.attributes[i].name] = 1;
    }
    for (var name in seen) {
      // `data-spa-*` is the prerenderer's bookkeeping, the same category as
      // the editor's own `data-cve-*`: a converted SPA carries one
      // `data-spa-toggle`/`data-spa-panel` id PER disclosure, so an
      // eight-question accordion has eight members differing only in that
      // id. Compared verbatim that reads as eight different designs and the
      // group is offered to nobody — verified live on a converted React FAQ,
      // where every answer was editable as text and the list was not a list.
      // (bridge.js carries the identical clause; change them together.)
      if (COLLECTION_ATTR_WHITELIST[name] || COLLECTION_ATTR_IGNORE[name]
        || 0 === name.indexOf('data-cve-') || 0 === name.indexOf('data-spa-')) {
        continue;
      }
      if (collectionAttrValue(a, name) !== collectionAttrValue(b, name)) {
        return false;
      }
    }
    return true;
  }
  function ownText(el) {
    var out = '';
    for (var i = 0; i < el.childNodes.length; i++) {
      if (3 === el.childNodes[i].nodeType) {
        out += el.childNodes[i].textContent;
      }
    }
    return out.trim();
  }
  function collectionValuesVary(vals) {
    for (var i = 1; i < vals.length; i++) {
      if (vals[i] !== vals[0]) {
        return true;
      }
    }
    return false;
  }
  function diffCollectionMembers(members, path, slots) {
    var first = members[0];
    var i;
    for (i = 1; i < members.length; i++) {
      if (!collectionAttrsCongruent(first, members[i])) {
        return;
      }
    }
    if (0 === first.children.length) {
      var tag = first.tagName.toLowerCase();
      if ('img' === tag) {
        // alt variance counts too, not only src: content-based media dedup
        // legitimately collapses byte-identical images to ONE attachment
        // URL on the live site, so five design-distinct badges can share a
        // src there while their alts still differ — src-only detection then
        // drops the whole group on the live side only. Verified live on a
        // 5-badge strip of identical stock photos.
        var srcs = members.map(function (m) { return m.getAttribute('src') || ''; });
        var alts = members.map(function (m) { return m.getAttribute('alt') || ''; });
        if (collectionValuesVary(srcs) || collectionValuesVary(alts)) {
          slots.push({ type: 'image', path: path });
        }
        return;
      }
      if (first.hasAttribute('href')) {
        var texts = members.map(function (m) { return (m.textContent || '').trim(); });
        var hrefs = members.map(function (m) { return m.getAttribute('href') || ''; });
        if (collectionValuesVary(texts) || collectionValuesVary(hrefs)) {
          slots.push({ type: 'link', path: path });
        }
        return;
      }
      var textVals = members.map(function (m) { return (m.textContent || '').trim(); });
      if (!collectionValuesVary(textVals)) {
        return;
      }
      var maxLen = Math.max.apply(null, textVals.map(function (v) { return v.length; }));
      slots.push({ type: maxLen <= 60 ? 'text-short' : 'text-long', path: path });
      return;
    }
    var sig = shapeSignature(first);
    for (i = 1; i < members.length; i++) {
      if (shapeSignature(members[i]) !== sig) {
        return;
      }
    }
    var ownVals = members.map(ownText);
    if (collectionValuesVary(ownVals)) {
      slots.push({ type: 'text-own', path: path });
    }
    // Table bodies can have different row counts. Diff the shared prefix;
    // each body's extra rows remain part of that body during a save.
    var commonChildren = first.children.length;
    for (i = 1; i < members.length; i++) {
      commonChildren = Math.min(commonChildren, members[i].children.length);
    }
    for (var c = 0; c < commonChildren; c++) {
      var kids = members.map(function (m) { return m.children[c]; });
      diffCollectionMembers(kids, path.concat([c]), slots);
    }
  }

  var out = [];
  var all = document.body ? document.body.querySelectorAll('*') : [];
  for (var p = 0; p < all.length; p++) {
    var parent = all[p];
    if (excluded && parent.closest && parent.closest(excluded)) continue;
    var kids = [], k;
    for (k = 0; k < parent.children.length; k++) kids.push(parent.children[k]);
    if (kids.length < 2) continue;
    var seenSigs = {};
    for (k = 0; k < kids.length; k++) {
      var s2 = shapeSignature(kids[k]);
      if (seenSigs[s2]) continue;
      seenSigs[s2] = 1;
      var members = kids.filter(function (x) { return shapeSignature(x) === s2; });
      if (members.length < 2) continue;
      // A run whose MEMBERS are excluded zones is not a collection either: a
      // footer's three link columns are three navigation groups, and those
      // get the menu panel, never the collection popup. The parent test above
      // cannot see it — the run's parent is the footer row, which is no zone.
      // Left in, the group is also recorded asymmetrically, because the
      // conversion-time pass reads dist where nothing is stamped yet while
      // the live render carries data-ve-nav="3"/"4"/"5" — three different
      // values, so the members stop being congruent there and the gate
      // reports as lost a group that was never the editor's to offer.
      if (excluded && members.some(function (x) { return x.closest && x.closest(excluded); })) continue;
      var runs = collectionRuns(parent, members);
      for (var runIndex = 0; runIndex < runs.length; runIndex++) {
        var currentRun = runs[runIndex];
        if (currentRun.length < 2) continue;
        // Cells in a table row are columns, not a repeatable list. The row
        // itself remains eligible when the walk reaches its tbody.
        if (parent.tagName === 'TR' && (currentRun[0].tagName === 'TD' || currentRun[0].tagName === 'TH')) continue;
        var slots = [];
        diffCollectionMembers(currentRun, [], slots);
        if (slots.length < 1 || slots.length > MAX_SLOTS) continue;
        out.push({
          parentTag: parent.tagName.toLowerCase(),
          parentClasses: classListSorted(parent).join(' '),
          memberShape: s2,
          count: currentRun.length,
          slots: slots.length,
        });
      }
    }
  }
  return out;
}
