#!/usr/bin/env python3
"""Capture the real editor canvas, keeping CSS viewport units and responsive width.

Overlapping scroll captures retain native layout. Only already captured upper
pixels (including repeated sticky overlays) are omitted from subsequent tiles;
no content nodes/styles are hidden or replaced with frontend markup.
"""
import io
import json
import math
from pathlib import Path
from urllib.parse import quote, urlparse
import numpy as np
from PIL import Image
from playwright.sync_api import sync_playwright

WIDTHS=(1440,820,390)
ROOT='.is-root-container'
GEOMETRY="""e=>{
 const boxes=n=>{const r=n.getBoundingClientRect();if(r.width&&r.height)return [r];return [...n.children].flatMap(boxes)};
 const rs=boxes(e);if(!rs.length)return {x:0,y:0,width:0,height:0,scroll:scrollY,viewport:innerHeight};
 const x=Math.min(...rs.map(r=>r.x)),y=Math.min(...rs.map(r=>r.y));
 return {x,y,width:Math.max(...rs.map(r=>r.right))-x,height:Math.max(...rs.map(r=>r.bottom))-y,scroll:scrollY,viewport:innerHeight};
}"""


def ready(frame):
    frame.evaluate('''async () => {
      for(const i of document.images) i.loading='eager';
      await document.fonts.ready;
      await Promise.all([...document.images].map(i=>i.decode().catch(()=>{})));
    }''')
    frame.add_style_tag(content='*,*::before,*::after{animation:none!important;transition:none!important;caret-color:transparent!important}')


def canvas_image(page, frame, selector, iframe=False):
    root=frame.locator(selector).first
    root.wait_for(state='visible')
    frame.evaluate('scrollTo(0,0)')
    ready(frame)
    page.wait_for_timeout(500)
    initial=root.evaluate(GEOMETRY)
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
        geometry=root.evaluate(GEOMETRY)
        if abs(geometry['height']-initial['height'])>1:
            raise RuntimeError('Canvas height changed while capturing; wait for dynamic content')
        top=geometry['y']+done
        take=min(height-done,math.floor(geometry['viewport']-top))
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


def size_frame(page,width):
    # The administration shell stays desktop-sized so WordPress does not replace
    # the Site Editor with its mobile navigation screen. Only the real iframe's
    # viewport is resized; its content uses the requested responsive media queries.
    element=page.locator('iframe[name="editor-canvas"]')
    element.wait_for(state='visible',timeout=60000)
    for _ in range(8):
        element.evaluate('(e,w)=>{e.style.setProperty("width",w+"px","important");e.style.setProperty("height","900px","important");e.style.setProperty("max-width","none","important")}',width)
        frame=element.element_handle().content_frame()
        frame.locator(ROOT).wait_for(state='visible',timeout=60000)
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
            if item['kind']!='template-parts' and item['editorUrl'].startswith('/wp-admin/site-editor.php'):
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
            response=front.goto(args.site+item['path'],wait_until='networkidle')
            if not response or response.status!=200: raise RuntimeError('Frontend reference did not return HTTP200')
            ready(frame);ready(front)
            ref=front.locator(item['selector']).first.evaluate(GEOMETRY)
            editor_selector=item.get('editorSelector',ROOT)
            editor_geometry=frame.locator(editor_selector).first.evaluate(GEOMETRY)
            handle=frame.frame_element();outer=handle.bounding_box()
            alignment={axis:(ref[axis]%1)-((editor_geometry[axis]+outer[axis])%1) for axis in ('x','y')}
            frame.locator(ROOT).evaluate('''(e,a)=>{
              const s=getComputedStyle(e);
              if(s.position!=='relative')throw new Error('Editor canvas lacks a relative admin positioning container');
              e.style.top=(parseFloat(s.top)||0)+a.y+'px';
              e.style.left=(parseFloat(s.left)||0)+a.x+'px';
            }''',alignment)
            row['adminCanvasAlignment']=alignment
            actual,geometry=canvas_image(page,frame,editor_selector,True)
            row['actualWidth']=frame.evaluate('innerWidth');row['canvas']=geometry
            row['adminViewport']=page.viewport_size
            expected,reference=canvas_image(front,front,item['selector'])
            out=Path(args.out).parent/'editor-screenshots';out.mkdir(parents=True,exist_ok=True)
            stem=f'{item["kind"]}-{str(item["id"]).replace("/","-")}-{width}'
            expected_path=out/(stem+'-frontend.png');actual_path=out/(stem+'-editor.png')
            expected.save(expected_path);actual.save(actual_path)
            row.update(diff=difference(expected,actual),frontendScreenshot=str(expected_path),editorScreenshot=str(actual_path),referenceCanvas=reference)
            row['passed']=row['diff']<=args.threshold
            browser.close()
    except Exception as error: row['error']=str(error)
    print(f'editor visual {row["kind"]} {row["id"]} {width}: '+(f'{row["diff"]:.3%}' if 'diff' in row else row['error']),flush=True)
    return row


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
        allowed={'/'+row['slug'].strip('/')+'/' for row in bundle['pages']}
        if any(row.get('kind')=='front' for row in bundle['pages']): allowed.add('/')
        items=[item for item in items if item['path'] in allowed]

    templates=get('templates?context=edit&per_page=100')
    assigned=sorted({e['template'] for e in entities if e.get('template') and e['type'] in ('page','post')})
    for slug in dict.fromkeys(['front-page','home','single']+assigned):
        matching=next((t for t in templates if t.get('theme')==args.theme_slug and t['slug']==slug),None)
        if not matching: continue
        if slug=='front-page': representative=next((e for e in entities if e['id']==front_id),None)
        elif slug=='home': representative=next((e for e in entities if e['id']==blog_id),None)
        elif slug=='single': representative=next((e for e in entities if e['type']=='post' and not e.get('template')),None)
        else: representative=next((e for e in entities if e.get('template')==slug),None)
        override=None
        if not representative and slug=='single' and getattr(args,'edit_roundtrip',False):
            representative=next((e for e in entities if e['type']=='post'),None)
            if representative: override={'postId':representative['id'],'from':representative.get('template',''),'to':''}
        if not representative: raise RuntimeError(f'No representative content for template {slug}; fixture --edit-roundtrip can test the fallback with a restored template assignment')
        item={'kind':'templates','id':matching['id'],'path':urlparse(representative['link']).path,'region':'document','selector':'.wp-site-blocks',
          'editorUrl':f'/wp-admin/site-editor.php?postType={representative["type"]}&postId={representative["id"]}&canvas=edit'}
        if override:item['templateOverride']=override
        items.append(item)
    for part in get('template-parts?context=edit&per_page=100'):
        if part.get('theme')!=args.theme_slug or part['slug'] not in ('header','footer'):continue
        items.append({'kind':'template-parts','id':part['id'],'path':'/','region':part['slug'],'selector':f'{part["slug"]}.wp-block-template-part',
          'editorUrl':f'/wp-admin/site-editor.php?postType=wp_template_part&postId={quote(part["id"],safe="")}&canvas=edit'})
    return items
