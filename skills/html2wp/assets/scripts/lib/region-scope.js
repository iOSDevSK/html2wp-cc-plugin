// Does the editor's stamping belong to the key it was opened with?
//
// The ONE implementation, shared by smoke-editor.py (live) and
// test-region-scope.sh (fixtures), so the live check and the test can never
// drift into disagreeing about what "correctly scoped" means.
//
// Why this exists: smoke-editor asserted only that SOME element carried
// data-cve-path. Opening a chrome VARIANT key loaded a page whose whole
// <main> was stamped — 56 paths, none of them the header — and the step
// reported PASS. Five variant parts per site were certified editable while
// the editor was in fact showing the page's content. The count was never the
// question; the REGION always was.
//
// Evaluated inside the preview iframe. Returns a verdict object rather than a
// boolean, because the caller has to be able to say WHY in a failure dump.
(() => {
  const AREA_ROOTS = {
    header: 'header.wp-block-template-part, header',
    footer: 'footer.wp-block-template-part, footer',
    article: 'article, main',
    page: 'main',
  };

  return function regionScope(area) {
    const stamped = [...document.querySelectorAll('[data-cve-path]')];
    const rootSel = AREA_ROOTS[area] || AREA_ROOTS.page;
    const roots = [...document.querySelectorAll(rootSel)];
    const inside = stamped.filter((el) => roots.some((r) => r.contains(el)));
    // For a chrome key, anything stamped inside <main> is the page's content
    // wearing the part's name — the exact shape of the false pass.
    const isChrome = area === 'header' || area === 'footer';
    const mains = [...document.querySelectorAll('main')];
    const inMain = isChrome ? stamped.filter((el) => mains.some((m) => m.contains(el))) : [];
    return {
      area,
      rootSelector: rootSel,
      rootsFound: roots.length,
      stamped: stamped.length,
      insideRegion: inside.length,
      insideMain: inMain.length,
      ok: stamped.length > 0 && inside.length > 0 && inMain.length === 0,
      // Message order is by DIAGNOSTIC VALUE, not by check order. When
      // nothing is inside the header AND everything is inside <main>, both
      // are true and only the second one names what actually happened.
      why: stamped.length === 0 ? 'nothing stamped at all'
        : inMain.length > 0 ? `${inMain.length} of ${stamped.length} path(s) stamped inside <main> — the editor is showing the PAGE, not this ${area} part`
        : roots.length === 0 ? `no element matches ${rootSel} — the part is probably wrapped in a bare <div> (missing wp:template-part tagName)`
        : inside.length === 0 ? `${stamped.length} path(s) stamped, none inside ${rootSel}`
        : 'scoped correctly',
    };
  };
})()
