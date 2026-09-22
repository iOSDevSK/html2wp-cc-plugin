#!/usr/bin/env python3
"""Capture the real editor canvas, keeping CSS viewport units and responsive width.

Overlapping scroll captures retain native layout. Only already captured upper
pixels (including repeated sticky overlays) are omitted from subsequent tiles;
no content nodes/styles are hidden or replaced with frontend markup.
"""
import io
import json, re
import math
from pathlib import Path
from urllib.parse import quote, urlparse
import numpy as np
from PIL import Image
from playwright.sync_api import sync_playwright

WIDTHS=(1440,820,390)
ROOT='.is-root-container'
# children: measure the union of the element's children (a template part's
# blocks inside the full-width editor canvas root).
GEOMETRY="""(e,children)=>{
 const boxes=n=>{const r=n.getBoundingClientRect();if(r.width&&r.height)return [r];return [...n.children].flatMap(boxes)};
 const rs=children?[...e.children].flatMap(boxes):boxes(e);if(!rs.length)return {x:0,y:0,width:0,height:0,scroll:scrollY,viewport:innerHeight};
 const x=Math.min(...rs.map(r=>r.x)),y=Math.min(...rs.map(r=>r.y));
 return {x,y,width:Math.max(...rs.map(r=>r.right))-x,height:Math.max(...rs.map(r=>r.bottom))-y,scroll:scrollY,viewport:innerHeight};
}"""


def ready(frame):
    # Scroll through first: recorded reveals (data-spa-reveal) show as they
    # enter the viewport, as on the source.
    frame.evaluate('''async () => {
      // One image resource on both sides, as the frontend gate does: srcset
      // lets the frontend pick a smaller file than the editor preview.
      for(const i of document.images){i.loading='eager';i.removeAttribute('srcset');i.removeAttribute('sizes');}
      for(let y=0;y<document.documentElement.scrollHeight;y+=700){scrollTo(0,y);await new Promise(r=>setTimeout(r,80));}
      scrollTo(0,0);
      await new Promise(r=>setTimeout(r,400));
      await document.fonts.ready;
      await Promise.all([...document.images].map(i=>i.decode().catch(()=>{})));
    }''')
    frame.add_style_tag(content='*,*::before,*::after{animation:none!important;transition:none!important;caret-color:transparent!important}')


def canvas_image(page, frame, selector, iframe=False, children=False):
    root=frame.locator(selector).first
    # A box-less wrapper (display:contents template part) is measured by its children.
    root.wait_for(state='attached')
    frame.evaluate('scrollTo(0,0)')
    ready(frame)
    page.wait_for_timeout(500)
    initial=root.evaluate(GEOMETRY,children)
    initial['y']+=initial['scroll']
    if initial['width']<=0 or initial['height']<=0:
        raise RuntimeError('Empty editor/content canvas')
    width=round(initial['width']); height=round(initial['height'])
    output=Image.new('RGB',(width,height),'white')
    done=0; tiles=[]
    while done<height:
        # Start subsequent captures halfway down the native viewport. Sticky
        # content is preserved in the first tile, never duplicated in the join.
        desired=max(0,initial['y']+done-initial['viewport']/2)
        frame.evaluate('(y)=>scrollTo(0,y)',desired)
        page.wait_for_timeout(80)
        geometry=root.evaluate(GEOMETRY,children)
        if abs(geometry['height']-initial['height'])>1:
            raise RuntimeError('Canvas height changed while capturing; wait for dynamic content')
        top=geometry['y']+done
        take=min(height-done,math.floor(geometry['viewport']-top))
        # A region ending at the very bottom of the document can leave a
        # rounding remainder (its fractional height) below the last scroll.
        if take<=0 and top>=-.5 and height-done<=1:
            output=output.crop((0,0,width,done)); height=done
            break
        if take<=0 or top<-.5:
            raise RuntimeError('Canvas cannot be scrolled into view without clipping')
        origin={'x':0,'y':0}
        if iframe:
            handle=frame.frame_element(); origin=handle.bounding_box()
            if not origin: raise RuntimeError('Editor iframe is not visible')
        clip={'x':origin['x']+geometry['x'],'y':origin['y']+max(0,top),'width':initial['width'],'height':take}
        tile=Image.open(io.BytesIO(page.screenshot(clip=clip))).convert('RGB')
        output.paste(tile,(0,done));tiles.append({'offset':done,'height':take,'scrollY':geometry['scroll']})
        done+=take
    frame.evaluate('scrollTo(0,0)')
    return output,{'selector':selector,'iframe':iframe,'width':initial['width'],'height':initial['height'],'tiles':tiles}


def difference(a,b):
    shape=(max(a.height,b.height),max(a.width,b.width),3)
    aa=np.full(shape,255,dtype=np.uint8);bb=aa.copy()
    aa[:a.height,:a.width]=np.asarray(a);bb[:b.height,:b.width]=np.asarray(b)
    return float((np.max(np.abs(aa.astype(int)-bb.astype(int)),axis=2)>16).mean())


def masked_difference(a,b,split):
    """difference() with the rows from `split` down compared at the best of a
    -1/0/+1 row alignment (a masked block above them may end on a fraction)."""
    top=lambda image:image.crop((0,0,image.width,min(split,image.height)))
    low=lambda image,skip:image.crop((0,min(image.height,split+skip),image.width,image.height))
    rows=lambda image:max(1,image.height)
    head=difference(top(a),top(b));head_rows=min(split,max(a.height,b.height))
    tails=[difference(low(a,max(0,-shift)),low(b,max(0,shift))) for shift in (-1,0,1)]
    tail_rows=max(0,max(a.height,b.height)-split)
    total=head_rows+tail_rows
    return (head*head_rows+min(tails)*tail_rows)/total if total else head


def size_frame(page,width):
    # The administration shell stays desktop-sized so WordPress does not replace
    # the Site Editor with its mobile navigation screen. Only the real iframe's
    # viewport is resized; its content uses the requested responsive media queries.
    element=page.locator('iframe[name="editor-canvas"]')
    element.wait_for(state='visible',timeout=60000)
    for _ in range(8):
        element.evaluate('(e,w)=>{e.style.setProperty("width",w+"px","important");e.style.setProperty("height","900px","important");e.style.setProperty("max-width","none","important")}',width)
        frame=element.element_handle().content_frame()
        frame.locator(ROOT).wait_for(state='attached',timeout=60000)
        page.wait_for_timeout(250)
        actual=frame.evaluate('({width:innerWidth,height:innerHeight})')
        if actual=={'width':width,'height':900}: return frame
    raise RuntimeError(f'Editor iframe viewport differs: {actual}, expected {width}x900')


def case(args,item,width):
    row={k:item[k] for k in ('kind','id','path','region')};row.update(width=width,passed=False)
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch()
            context=browser.new_context(storage_state=args.auth_state,viewport={'width':max(1100,width+48),'height':1020},device_scale_factor=1,reduced_motion='reduce')
            page=context.new_page()
            page.goto(args.site+item['editorUrl'],wait_until='domcontentloaded')
            page.wait_for_function('window.wp && wp.data && wp.blocks',timeout=60000)
            page.wait_for_timeout(1200)
            # Native view mode: show the real assigned template around the
            # content. This action changes no post/template data or preference.
            if item['kind']!='template-parts' and item['editorUrl'].startswith('/wp-admin/site-editor.php') and 'postType=wp_template&' not in item['editorUrl']:
                page.wait_for_function("wp.data.dispatch('core/editor').setRenderingMode",timeout=60000)
                page.evaluate("wp.data.dispatch('core/editor').setRenderingMode('template-locked')")
            frame=size_frame(page,width)
            for _ in range(2):
                page.wait_for_timeout(500)
                for label in ('Close','Close dialog','Get started','Continue'):
                    button=page.get_by_role('button',name=label,exact=True)
                    for index in range(button.count()):
                        if button.nth(index).is_visible(): button.nth(index).click()
            close_settings=page.get_by_role('button',name='Close Settings',exact=True)
            if close_settings.count() and close_settings.first.is_visible():close_settings.first.click()
            if page.locator('.components-modal__screen-overlay:visible').count():
                raise RuntimeError('Editor welcome dialog still covers the real canvas')
            # Admin toolbar shadow extends one pixel over mobile iframe edge.
            # Keep its layout space but exclude this non-content chrome.
            page.add_style_tag(content='.interface-interface-skeleton__header{visibility:hidden!important}')
            if item['kind']=='templates':
                resolved=page.evaluate("wp.data.select('core/edit-site').getEditedPostId()")
                if str(resolved)!=str(item['id']):
                    raise RuntimeError(f'Representative post resolved template {resolved}, expected {item["id"]}')
                if 'This is the Content block' in frame.locator(ROOT).inner_text():
                    raise RuntimeError('Site Editor has no real representative post context')
            # The separate post-title input is editor chrome, not post content.
            # Remove its fractional vertical offset before rasterizing canvas.
            frame.add_style_tag(content='.editor-visual-editor__post-title-wrapper{display:none!important}')
            page.mouse.move(0,0)
            # Public frontend has no admin bar. Match only the fractional raster
            # origin on the editor's relative admin canvas wrapper before paint;
            # viewport and every content-relative position remain unchanged.
            front=browser.new_page(viewport={'width':width,'height':900},device_scale_factor=1,reduced_motion='reduce')
            # An empty basket sends the checkout back to the cart: put one
            # purchasable product in this visitor's basket first.
            if item.get('basket'): fill_basket(front,args.site)
            response=front.goto(args.site+item['path'],wait_until='networkidle')
            if not response or response.status!=200: raise RuntimeError('Frontend reference did not return HTTP200')
            if item['kind']=='template-parts':
                # The part canvas centres a body only as tall as the part in the
                # editor's grey canvas colour; a fixed or transparent header then
                # shows that editor chrome. Both sides get the page background.
                frame.evaluate("()=>{const h=document.documentElement;h.style.setProperty('background',getComputedStyle(document.body).backgroundColor,'important')}")
                # The part editor shows the part alone on the canvas; on the page
                # a transparent header shows whatever lies under it (a hero). Only
                # the part paints in the reference, over the page's own background.
                front.add_style_tag(content='body *{visibility:hidden!important}'+item['selector']+','+item['selector']+' *{visibility:visible!important}')
            ready(frame);ready(front)
            # WooCommerce renders these nodes differently in its editor preview
            # (React) and on the frontend (PHP); their boxes stay in the layout.
            for side in ([frame,front] if item.get('mask') else []):
                side.add_style_tag(content=','.join(item['mask'])+'{visibility:hidden!important}')
            if item.get('mask'): row['masked']=item['mask']
            ref=front.locator(item['selector']).first.evaluate(GEOMETRY,False)
            editor_selector=item.get('editorSelector',ROOT)
            editor_geometry=frame.locator(editor_selector).first.evaluate(GEOMETRY,bool(item.get('editorChildren')))
            handle=frame.frame_element();outer=handle.bounding_box()
            alignment={axis:(ref[axis]%1)-((editor_geometry[axis]+outer[axis])%1) for axis in ('x','y')}
            frame.locator(ROOT).evaluate('''(e,a)=>{
              const s=getComputedStyle(e);
              if(s.position!=='relative')throw new Error('Editor canvas lacks a relative admin positioning container');
              e.style.top=(parseFloat(s.top)||0)+a.y+'px';
              e.style.left=(parseFloat(s.left)||0)+a.x+'px';
            }''',alignment)
            row['adminCanvasAlignment']=alignment
            actual,geometry=canvas_image(page,frame,editor_selector,True,bool(item.get('editorChildren')))
            row['actualWidth']=frame.evaluate('innerWidth');row['canvas']=geometry
            row['adminViewport']=page.viewport_size
            expected,reference=canvas_image(front,front,item['selector'])
            if item.get('mask'):
                cut_editor=block_rows(frame,editor_selector,item['mask'],bool(item.get('editorChildren')))
                cut_front=block_rows(front,item['selector'],item['mask'],False)
                row['mask']={'editor':cut_editor,'frontend':cut_front}
                if abs(cut_editor['x']-cut_front['x'])>2 or abs(cut_editor['width']-cut_front['width'])>2:
                    raise RuntimeError(f'WooCommerce block placement differs: editor {cut_editor}, frontend {cut_front}')
                for selector in item.get('maskAlso',[]):
                    extra=[cut for cut in (block_rows(side,region,[selector],children,True) for side,region,children in ((frame,editor_selector,bool(item.get('editorChildren'))),(front,item['selector'],False)))]
                    row['mask'][selector]=extra
                actual=excise(actual,cut_editor,*(extra_cut(row,'editor')));expected=excise(expected,cut_front,*(extra_cut(row,'frontend')))
            out=Path(args.out).parent/'editor-screenshots';out.mkdir(parents=True,exist_ok=True)
            stem=f'{item["kind"]}-{str(item["id"]).replace("/","-")}-{width}'
            expected_path=out/(stem+'-frontend.png');actual_path=out/(stem+'-editor.png')
            expected.save(expected_path);actual.save(actual_path)
            # Below a masked block of fractional height the rest of the page sits
            # at a different sub-pixel offset on each side: it is compared at the
            # better of a one-row raster alignment.
            split=min(cut_editor['top'],cut_front['top']) if item.get('mask') else None
            row.update(diff=difference(expected,actual) if split is None else masked_difference(expected,actual,split),frontendScreenshot=str(expected_path),editorScreenshot=str(actual_path),referenceCanvas=reference)
            row['passed']=row['diff']<=args.threshold
            browser.close()
    except Exception as error: row['error']=str(error)
    print(f'editor visual {row["kind"]} {row["id"]} {width}: '+(f'{row["diff"]:.3%}' if 'diff' in row else row['error']),flush=True)
    return row


def block_rows(frame,region,selectors,children=False,optional=False):
    """The first masked block's rows and columns, relative to the captured region."""
    frame.evaluate('scrollTo(0,0)')
    box=frame.locator(region).first.evaluate(GEOMETRY,children)
    rect=frame.evaluate('(s)=>{const e=s.map(q=>document.querySelector(q)).find(Boolean);if(!e)return null;const r=e.getBoundingClientRect();return {x:r.x,y:r.y+scrollY,width:r.width,height:r.height}}',selectors)
    if not rect or rect['height']<=0:
        if optional: return None
        raise RuntimeError(f'Masked block {selectors} is missing')
    # Top and bottom are rounded separately (not top + rounded height): the rows
    # below the block keep their own raster alignment on both sides, so a block
    # of fractional height does not shift the rest of the page by a pixel.
    top=rect['y']-box['y']-box['scroll']
    return {'x':round(rect['x']-box['x']),'top':round(top),'width':round(rect['width']),'height':round(top+rect['height'])-round(top)}


def extra_cut(row,side):
    return [cuts[0 if side=='editor' else 1] for key,cuts in row['mask'].items() if isinstance(cuts,list) and cuts[0 if side=='editor' else 1]]


def excise(image,*cuts):
    """The capture without the masked blocks' rows (overlapping cuts merge)."""
    keep=np.ones(image.height,dtype=bool)
    for cut in cuts:
        top=max(0,min(image.height,cut['top']));bottom=max(top,min(image.height,cut['top']+cut['height']))
        keep[top:bottom]=False
    rows=np.asarray(image)[keep]
    return Image.fromarray(rows) if len(rows) else Image.new('RGB',(image.width,1),'white')


def fill_basket(page,site):
    products=page.request.get(site+'/wp-json/wc/store/v1/products?per_page=50').json()
    product=next((p for p in products if p.get('type')=='simple' and p.get('is_purchasable') and p.get('is_in_stock')),None)
    if not product: raise RuntimeError('No simple purchasable product to fill the checkout basket')
    page.goto(site+f'/?add-to-cart={product["id"]}',wait_until='networkidle')


def fallback_cases(args,item):
    """Explicit fixture-only template roundtrip; never races other captures."""
    override=item['templateOverride']; rows=[]
    endpoint=args.site+f'/wp-json/wp/v2/posts/{override["postId"]}'
    headers={'X-WP-Nonce':args.rest_nonce}
    with sync_playwright() as pw:
        request=pw.request.new_context(storage_state=args.auth_state)
        response=request.get(endpoint+'?context=edit',headers=headers)
        if not response.ok:raise RuntimeError('Cannot read fallback fixture post')
        original=response.json();request.dispose()
    if original.get('template','')!=override['from']: raise RuntimeError('Post template changed before fallback test')
    try:
        with sync_playwright() as pw:
            request=pw.request.new_context(storage_state=args.auth_state)
            try:
                updated=request.post(endpoint,headers=headers,data={'template':override['to']})
                if not updated.ok:raise RuntimeError('Cannot select fallback template on fixture')
            finally:request.dispose()
        rows=[case(args,item,width) for width in WIDTHS]
    finally:
        with sync_playwright() as pw:
            request=pw.request.new_context(storage_state=args.auth_state)
            restored=request.post(endpoint,headers=headers,data={'template':override['from']})
            after=request.get(endpoint+'?context=edit',headers=headers).json()
            request.dispose()
            if not restored.ok or after.get('template','')!=override['from'] or after['content']['raw']!=original['content']['raw']:
                raise RuntimeError('Failed to restore fixture template/content after fallback test')
    for row in rows:row['templateRoundtrip']={**override,'restored':True}
    return rows


def inventory(args,request):
    headers={'X-WP-Nonce':args.rest_nonce}
    def get(path):
        r=request.get(args.site+'/wp-json/wp/v2/'+path,headers=headers)
        if not r.ok: raise RuntimeError(f'Editor visual inventory HTTP {r.status}: {path}')
        return r.json()
    entities=[]
    for plural in ('pages','posts'):
        for number in range(1,10000):
            batch=get(f'{plural}?context=edit&per_page=100&page={number}')
            entities.extend(batch)
            if len(batch)<100: break
    items=[{'kind':e['type'],'id':e['id'],'path':urlparse(e['link']).path,'region':'content',
        'editorUrl':f'/wp-admin/post.php?post={e["id"]}&action=edit','selector':'.wp-block-post-content'} for e in entities if e.get('content',{}).get('raw','').strip()]
    for item in items:
        entity=next(e for e in entities if e['id']==item['id'])
        # The theme opens posts template-locked, so the editor canvas shows
        # the whole article template; compare only the post content region.
        if entity['type']=='post': item['editorSelector']='.wp-block-post-content'
        if entity.get('template'):
            item['editorUrl']=f'/wp-admin/site-editor.php?postType={entity["type"]}&postId={entity["id"]}&canvas=edit'
            item['editorSelector']='.wp-block-post-content'
    settings=get('settings');front_id=settings.get('page_on_front');blog_id=settings.get('page_for_posts')
    for item in items:
        if item['id']==blog_id:
            # WordPress renders the assigned posts page through home.html,
            # without a post-content wrapper. Its source blocks are the same
            # editable listing composition, compared against that whole region.
            item['selector']='.wp-site-blocks'
            item['editorUrl']=f'/wp-admin/site-editor.php?postType=page&postId={blog_id}&canvas=edit'
    if getattr(args,'theme_dir',None):
        bundle=json.loads((Path(args.theme_dir)/'content/content.json').read_text())
        # Live paths from the importer's record (WordPress may suffix a slug).
        imported=request.get(args.site+'/wp-json/h2wp-gb/v1/import-status',headers=headers)
        live={row.get('key'):urlparse(row.get('path') or '').path for row in ((imported.json().get('entities') or []) if imported.ok else []) if row.get('key') and row.get('path')}
        allowed={live.get(row['key']) or '/'+row['slug'].strip('/')+'/' for row in bundle['pages']}
        if any(row.get('kind')=='front' for row in bundle['pages']): allowed.add('/')
        items=[item for item in items if item['path'] in allowed]

    # WooCommerce renders the shop page through its Product Catalog template
    # (archive-product), never the page's own content, while the Site Editor
    # still opens that page in the page template. Comparing its post content
    # would wait for a region the frontend never has; measure the catalog
    # template the owner actually edits instead.
    status=request.get(args.site+'/wp-json/h2wp-gb/v1/import-status',headers=headers)
    if not status.ok: raise RuntimeError(f'Editor visual inventory HTTP {status.status}: import-status')
    rows=status.json().get('entities') or []
    shop=next((e for e in entities for row in rows if row.get('kind')=='shop' and row.get('id')==e['id']),None)
    if shop: items=[item for item in items if item['id']!=shop['id']]
    # The cart and checkout blocks draw WooCommerce's built-in preview basket
    # (sample products, fee and tax) in the editor, while the frontend draws
    # the visitor's real basket; no theme can make those pixels agree. These
    # pages are compared as whole documents (frame, header, footer, spacing)
    # in their template with only the block's own rows cut out of both
    # captures; the block's horizontal placement is compared separately, and
    # its behaviour is the functional WooCommerce audit's.
    commerce={row.get('id'):row.get('kind') for row in rows if row.get('kind') in ('cart','checkout')}
    for item in items:
        if item['id'] in commerce:
            item.update(region='document',selector='.wp-site-blocks',editorSelector=ROOT,
              editorUrl=f'/wp-admin/site-editor.php?postType=page&postId={item["id"]}&canvas=edit',
              mask=['.wp-block-woocommerce-cart','.wp-block-woocommerce-checkout'],basket=commerce[item['id']]=='checkout',
              # The notices block shows a placeholder in the editor only.
              maskAlso=['.wp-block-woocommerce-store-notices'])
    templates=get('templates?context=edit&per_page=100')
    assigned=sorted({e['template'] for e in entities if e.get('template') and e['type'] in ('page','post')})
    for slug in dict.fromkeys(['front-page','home','single']+assigned+(['archive-product'] if shop else [])):
        matching=next((t for t in templates if t.get('theme')==args.theme_slug and t['slug']==slug),None)
        if not matching: continue
        # A site with a static front page and no posts page never renders
        # home.html, and one with no posts at all has no article for single;
        # the new-post gate (--edit-roundtrip) proves single on a fresh post.
        if slug=='home' and not blog_id and settings.get('show_on_front')=='page': continue
        if slug=='single' and not any(e['type']=='post' for e in entities) and getattr(args,'edit_roundtrip',False): continue
        if slug=='front-page': representative=next((e for e in entities if e['id']==front_id),None)
        elif slug=='home': representative=next((e for e in entities if e['id']==blog_id),None)
        elif slug=='single': representative=next((e for e in entities if e['type']=='post' and not e.get('template')),None)
        elif slug=='archive-product': representative=shop
        else:
            # WordPress renders the static front page through front-page.html
            # whatever template it selects: that selection never renders, and
            # the front-page case above covers the page.
            representative=next((e for e in entities if e.get('template')==slug and e['id']!=front_id),None)
            if not representative and any(e.get('template')==slug for e in entities): continue
        override=None
        if not representative and slug=='single' and getattr(args,'edit_roundtrip',False):
            representative=next((e for e in entities if e['type']=='post'),None)
            if representative: override={'postId':representative['id'],'from':representative.get('template',''),'to':''}
        if not representative: raise RuntimeError(f'No representative content for template {slug}; fixture --edit-roundtrip can test the fallback with a restored template assignment')
        item={'kind':'templates','id':matching['id'],'path':urlparse(representative['link']).path,'region':'document','selector':'.wp-site-blocks',
          'editorUrl':f'/wp-admin/site-editor.php?postType={representative["type"]}&postId={representative["id"]}&canvas=edit'}
        if override:item['templateOverride']=override
        if slug=='archive-product':
            # Store notices, archive title, result count and pagination show
            # placeholder previews in the template editor; the product grid
            # shows the real products on both sides.
            item.update(editorUrl=f'/wp-admin/site-editor.php?postType=wp_template&postId={quote(matching["id"],safe="")}&canvas=edit',
              region='products',selector='.wp-block-woocommerce-product-template',editorSelector='.wp-block-woocommerce-product-template',
              # The editor labels every product's button 'Add to cart' and
              # joins price ranges with an em dash; the frontend does neither.
              mask=['.wp-block-woocommerce-product-template .wp-block-button__link','.wp-block-woocommerce-product-template .wc-block-components-product-button__button','.wp-block-woocommerce-product-template .wc-block-components-product-price','.wp-block-woocommerce-product-template .wp-block-woocommerce-product-price'])
        items.append(item)
    parts=[part for part in get('template-parts?context=edit&per_page=100') if part.get('theme')==args.theme_slug and (part.get('area') or part.get('slug')) in ('header','footer')]
    # Templates the REST listing leaves out are read from the theme's files.
    known={t.get('slug') for t in templates if t.get('theme')==args.theme_slug}
    theme_dir=Path(args.theme_dir) if getattr(args,'theme_dir',None) else None
    on_disk=[{'slug':f.stem,'theme':args.theme_slug,'content':{'raw':f.read_text(errors='ignore')}} for f in sorted((theme_dir/'templates').glob('*.html'))] if theme_dir and (theme_dir/'templates').is_dir() else []
    part_templates=templates+[t for t in on_disk if t['slug'] not in known]
    # Pages a menu links to render that link as current on their own page,
    # which the part editor never shows: prefer a page no menu links to.
    try: menus=get('navigation?context=edit&per_page=100') if parts else []
    except (KeyError,RuntimeError): menus=[]
    linked={int(n) for menu in menus for n in re.findall(r'"id"\s*:\s*(\d+)',(menu.get('content') or {}).get('raw','') if isinstance(menu.get('content'),dict) else '')}
    # The part editor has no page: it loads the default page styles and fonts,
    # so the reference is a page that renders with exactly those.
    config=json.loads((Path(args.theme_dir)/'content/config.json').read_text()) if getattr(args,'theme_dir',None) and (Path(args.theme_dir)/'content/config.json').is_file() else {}
    bundle_keys={row['slug'].strip('/'):row['key'] for row in (json.loads((Path(args.theme_dir)/'content/content.json').read_text()).get('pages',[]) if config else [])}
    def plain_page(e):
        key=bundle_keys.get(e.get('slug',''))
        return key is not None and (config.get('pageStyles') or {}).get(key,[])==config.get('defaultPageStyles',[]) and (config.get('pageFontStyles') or {}).get(key,[])==config.get('defaultPageFontStyles',[]) and not (config.get('pageBodyClasses') or {}).get(key)
    for part in parts:
        path=part_path(part['slug'],part_templates,entities,settings,args.theme_slug,linked,prefer=plain_page)
        if path is None: continue
        # The part editor's canvas root spans the canvas; its blocks are the part.
        # The frontend reference is a page whose template renders this part
        # (a header variant is not the front page's header).
        items.append({'kind':'template-parts','id':part['id'],'path':path,'region':part['slug'],'selector':f'{part.get("area")}.wp-block-template-part','editorChildren':True,
          'editorUrl':f'/wp-admin/site-editor.php?postType=wp_template_part&postId={quote(part["id"],safe="")}&canvas=edit'})
    return items


def uses_part(template,slug):
    """Whether a template's markup renders the template part `slug`."""
    raw=(template.get('content') or {}).get('raw','') if isinstance(template.get('content'),dict) else str(template.get('content') or '')
    return re.search(r'<!--\s*wp:template-part\s+\{[^}]*"slug"\s*:\s*"'+re.escape(slug)+'"',raw) is not None


def part_path(slug,templates,entities,settings,theme,linked=frozenset(),prefer=None):
    """The frontend path of a page rendering template part `slug`, or None.

    The front page when its template renders the part and no menu links to it,
    then ordinary pages (the page template; a theme without its own page
    template gets WordPress's, which renders the parts), an article and any
    page on a custom template; within each, a page no menu links to (`linked`
    ids) first, so no menu item is drawn in its current state, and among those
    one `prefer` accepts (a page on the default styles) first."""
    front_id=settings.get('page_on_front');blog_id=settings.get('page_for_posts')
    own={t['slug']:t for t in templates if t.get('theme')==theme}
    special={front_id,blog_id}
    candidates=[('front-page',lambda e:e['id']==front_id and e['id'] not in linked),
                ('page',lambda e:e['type']=='page' and not e.get('template') and e['id'] not in special),
                ('single',lambda e:e['type']=='post' and not e.get('template'))]
    candidates+=[(name,(lambda n:lambda e:e.get('template')==n)(name)) for name in own if name not in ('page','front-page','single')]
    for name,matches in candidates:
        if (name in own and uses_part(own[name],slug)) or (name=='page' and name not in own):
            found=[e for e in entities if matches(e)]
            ranked=[e for e in found if e['id'] not in linked]
            entity=next((e for e in ranked if prefer is None or prefer(e)),ranked[0] if ranked else found[0] if found else None)
            # The front page is served at the site root whatever its own slug.
            if entity: return '/' if name=='front-page' else urlparse(entity['link']).path
    return None
