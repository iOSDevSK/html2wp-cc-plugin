"""How many cards a live shop listing renders, counted the way it was built.

The generator puts [wp-products] into the FIRST element the manifest's
cardContainer names in the page's content. A gate that counts the children of
EVERY match counts other grids too — a footer drawn as `div.grid` columns made
a 12-product listing read as 16 cards and failed a correct shop.
"""


def count_listing_cards(page, container):
    """Children of the first `container` inside <main>, else in the page; 0 if none."""
    scope = page.locator("main").first
    if scope.count() and scope.locator(container).count():
        grid = scope.locator(container).first
    else:
        grid = page.locator(container).first
    return grid.locator(":scope > *").count() if grid.count() else 0


def count_after_paging(page, container, settle_ms=1200, max_pages=20):
    """(first page, whole catalogue): a listing that keeps the design's own
    pager (data-cve-load-kind="products") is paged until its button hides,
    then counted — it shows a page at a time, as the design did, and must
    still reach every product."""
    first = count_listing_cards(page, container)
    pager = page.locator('[data-cve-load-kind="products"]').first
    if not pager.count():
        return first, first
    for _ in range(max_pages):
        if not pager.is_visible():
            break
        pager.click()
        page.wait_for_timeout(settle_ms)
    return first, count_listing_cards(page, container)
