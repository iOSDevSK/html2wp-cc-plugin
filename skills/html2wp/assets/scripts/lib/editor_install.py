"""Idempotent editor setup through WordPress admin UI, without CLI writes."""

def install(page, wp, archive, step):
    def row(slug):
        return page.locator('tr[data-slug="' + slug + '"]')
    def active(slug):
        return row(slug).locator('a[href*="action=deactivate"]').count() > 0
    page.goto(wp + '/wp-admin/plugins.php')
    if active('visual-edit') or active('visual-edit-lite'):
        step(page, 'editor-activated', True, note='installed editor already active')
        return
    if not row('visual-edit-lite').count():
        if not archive:
            return False
        page.goto(wp + '/wp-admin/plugin-install.php?tab=upload')
        page.set_input_files('#pluginzip', str(archive))
        with page.expect_navigation(timeout=180_000):
            page.click('#install-plugin-submit')
        step(page, 'editor-uploaded', 'installed successfully' in page.content().lower(),
             'WordPress did not confirm editor installation')
        page.goto(wp + '/wp-admin/plugins.php')
    link = row('visual-edit-lite').locator('a[href*="action=activate"]').first
    step(page, 'editor-activation-available', bool(link.count()), 'No Activate action for installed Visual Edit Lite')
    with page.expect_navigation():
        link.click()
    page.goto(wp + '/wp-admin/plugins.php')
    step(page, 'editor-activated', active('visual-edit-lite'), 'Visual Edit Lite is not active after activation')
    return True
