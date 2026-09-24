#!/usr/bin/env python3
"""Real WordPress Gutenberg gate, restricted to localhost.

Checks actual native block serialization, real editor canvas pixels, source
parity, completed import state and the installed preview. Schema v2 acceptance
requires complete evidence; diagnostic skips never produce a delivery pass.
"""
import argparse
import collections
import io
import hashlib
import uuid
import importlib.util
import inspect
import json
from pathlib import Path
import re
import shutil
import sys
import threading
import time
from urllib.parse import quote, urlparse
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent / 'lib'))
from capture_ready import fill_login, reveal_all  # noqa: E402
import numpy as np
from PIL import Image
from playwright.sync_api import sync_playwright


def local(value):
    parsed = urlparse(value)
    if parsed.scheme != 'http' or parsed.hostname not in ('localhost', '127.0.0.1', '::1'):
        raise argparse.ArgumentTypeError('Use an HTTP localhost origin')
    return value.rstrip('/')


def inspect_tree(page, expression):
    return page.evaluate('''expression => {
      const blocks = (0,eval)(expression), invalid = [], unknown = [];
      let count=0;
      function walk(nodes) { for(const b of nodes) {
        count++; if(b.isValid===false) invalid.push({name:b.name,errors:b.validationIssues});
        // An unregistered block parses as core/missing, which is registered.
        if(b.name==='core/missing') unknown.push(b.attributes.originalName||b.name);
        else if(!wp.blocks.getBlockType(b.name)) unknown.push(b.name);
        walk(b.innerBlocks || []);
      }}
      walk(blocks); return {count,invalid,unknown};
    }''', expression)


# The editor's own reading of stored block markup (report rows
# "serialization"): wp.blocks.parse, then wp.blocks.serialize, which is what
# the first save of that page, post, template or part writes whatever the
# owner changed. It must write back the stored markup (byteIdentical), and
# every attribute must read back (attributeLoss, per block: "parse" — a
# stored comment attribute the editor reads otherwise or not at all, one the
# block type does not declare or its save() does not carry; "save" — one that
# changes between the editor's reading and a reading of what it saves: a
# deprecated save migrated). Comment attributes compare as data (their key
# order and JSON escaping mean nothing to WordPress: the importer appends an
# image's `id` last and writes URLs with `\/`); whitespace is normalized only
# between two block delimiters. Everything else must match byte for byte.
SERIALIZATION = r'''raw => {
  const canon=v=>JSON.stringify(v,(k,x)=>x&&typeof x==='object'&&!Array.isArray(x)?Object.fromEntries(Object.keys(x).sort().map(n=>[n,x[n]])):x);
  const normal=html=>String(html).replace(/<!-- (\/)?wp:([a-z0-9-]+(?:\/[a-z0-9-]+)?)(?: (\{[\s\S]*?\}))? (\/)?-->/g,(m,close,name,attrs,self)=>{
      let parsed=null;if(attrs){try{parsed=JSON.parse(attrs)}catch(e){return m}}
      return '<!-- '+(close||'')+'wp:'+(name.includes('/')?name:'core/'+name)+(parsed?' '+canon(parsed):'')+' '+(self||'')+'-->';})
    .replace(/-->\s+<!--/g,'--><!--').trim();
  const blocks=wp.blocks.parse(raw), saved=wp.blocks.serialize(blocks), reopened=wp.blocks.parse(saved), loss=[];
  let count=0;
  const kept=list=>(list||[]).filter(b=>b.blockName||String(b.innerHTML||'').trim());
  const read=(stored,parsed,path)=>stored.forEach((r,i)=>{const b=parsed[i],name=r.blockName||'core/freeform',p=path+'/'+i+':'+name;count++;
    if(!b){loss.push({path:p,block:name,phase:'parse',key:null});return;}
    for(const [k,v] of Object.entries(r.attrs||{}))if(canon(b.attributes[k])!==canon(v))loss.push({path:p,block:name,phase:'parse',key:k,stored:v,read:b.attributes[k]??null});
    read(kept(r.innerBlocks),b.innerBlocks||[],p);});
  read(kept(wp.blockSerializationDefaultParser.parse(raw)),blocks,'');
  const again=(a,b,path)=>a.forEach((x,i)=>{const y=b[i],p=path+'/'+i+':'+x.name;
    if(!y||y.name!==x.name){loss.push({path:p,block:x.name,phase:'save',key:null});return;}
    for(const k of new Set([...Object.keys(x.attributes),...Object.keys(y.attributes)]))if(canon(x.attributes[k])!==canon(y.attributes[k]))loss.push({path:p,block:x.name,phase:'save',key:k,stored:x.attributes[k]??null,read:y.attributes[k]??null});
    again(x.innerBlocks||[],y.innerBlocks||[],p);});
  again(blocks,reopened,'');
  const a=normal(raw),b=normal(saved);let at=-1;
  if(a!==b){at=0;while(at<a.length&&a[at]===b[at])at++;}
  return {blocks:count,byteIdentical:a===b,attributeLoss:loss.slice(0,50),...(loss.length>50?{attributeLossTotal:loss.length}:{}),
    ...(at>=0?{firstDifference:{at,stored:a.slice(Math.max(0,at-80),at+160),saved:b.slice(Math.max(0,at-80),at+160)}}:{})};
}'''


def serialization_row(page, raw, **identity):
    """One "serialization" row: the stored markup through the editor's parser
    and serializer (SERIALIZATION); passed when it writes it back unchanged."""
    row = {**identity, **page.evaluate(SERIALIZATION, raw)}
    row['passed'] = bool(row['byteIdentical'] and not row['attributeLoss'])
    return row


def installed_integrity(request, args, nonce):
    response = request.get(args.site + '/wp-json/h2wp-gb/v1/theme-integrity', headers={'X-WP-Nonce':nonce})
    if not response.ok: raise RuntimeError('Cannot verify installed theme integrity')
    value = response.json()
    if value.get('stylesheet') != args.theme_slug or value.get('digest') != args.expected_digest:
        raise RuntimeError('Installed theme differs from the local theme being verified')
    args.installed_digest = value['digest']


def source_paths(args, entities=None):
    """The bundle's pages by their live paths: the importer's own record
    first (WordPress may suffix a slug, e.g. a numeric '404' page is
    '404-2'), else the bundle slug."""
    if not args.theme_dir: return None
    bundle=json.loads((Path(args.theme_dir)/'content/content.json').read_text())
    config=json.loads((Path(args.theme_dir)/'content/config.json').read_text())
    live={row.get('key'):urlparse(row.get('path') or '').path.strip('/') for row in (entities or []) if row.get('key') and row.get('path')}
    return {('' if row['key']==config.get('frontPage') else live.get(row['key'],row['slug'].strip('/'))) for row in bundle['pages']}


def edit_post_body(page):
    # WP 7's template-locked view has the template's blocks in the outer store.
    # Select the native post-only editing view before editing its actual body.
    page.evaluate("wp.data.dispatch('core/editor').setRenderingMode('post-only')")
    page.wait_for_function("""()=>{const saved=wp.blocks.parse(wp.data.select('core/editor').getEditedPostContent());
      const shown=wp.data.select('core/block-editor').getBlocks();return saved.length&&shown.length&&saved[0].name===shown[0].name;}""",timeout=60000)


def default_post_template(theme_dir,slug):
    if not theme_dir: raise RuntimeError('New-post default template proof requires --theme-dir')
    root=Path(theme_dir)
    for candidate in ('single-post-'+slug,'single-post','single','singular','index'):
        if (root/'templates'/(candidate+'.html')).is_file():return root.name+'//'+candidate
    raise RuntimeError('No native default post template exists in the generated theme')


def new_post_gate(page,args,nonce):
    """Publish/save/reopen a uniquely owned fixture post, then always remove it."""
    suffix=uuid.uuid4().hex[:12];title='Template verification '+suffix
    marker='Native article body '+suffix
    raw='<!-- wp:heading --><h2 class="wp-block-heading">Editable fixture heading</h2><!-- /wp:heading -->\n<!-- wp:paragraph --><p>'+marker+'</p><!-- /wp:paragraph -->'
    headers={'X-WP-Nonce':nonce};endpoint=args.site+'/wp-json/wp/v2/posts'
    response=page.request.post(endpoint,headers=headers,data={'title':title,'slug':'h2wp-fixture-'+suffix,'status':'publish','template':'','content':raw})
    if not response.ok:raise RuntimeError('Cannot create owned new-post fixture')
    created=response.json();result={'id':created['id'],'path':urlparse(created['link']).path,'slug':created['slug'],'template':'','passed':False,'deleted':False}
    try:
        page.goto(args.site+f'/wp-admin/post.php?post={created["id"]}&action=edit',wait_until='domcontentloaded')
        page.wait_for_function("window.wp&&wp.data&&wp.data.select('core/block-editor').getBlocks().length>0",timeout=60000)
        expected_template=default_post_template(args.theme_dir,created['slug'])
        result['editorDefault']=page.evaluate("()=>({mode:wp.data.select('core/editor').getRenderingMode(),template:wp.data.select('core/editor').getCurrentTemplateId()})")
        edit_post_body(page)
        page.evaluate("()=>{const p=wp.data.select('core/block-editor').getBlocks().find(b=>b.name==='core/paragraph');wp.data.dispatch('core/block-editor').updateBlockAttributes(p.clientId,{content:p.attributes.content+' saved'});}")
        page.evaluate("async()=>{await wp.data.dispatch('core/editor').savePost()}")
        page.reload();page.wait_for_function("window.wp&&wp.data&&wp.data.select('core/block-editor').getBlocks().length>0",timeout=60000)
        edit_post_body(page)
        result['roundtrip']=inspect_tree(page,"wp.data.select('core/block-editor').getBlocks()")
        result['roundtrip']['textPersisted']=page.evaluate("wp.data.select('core/editor').getEditedPostContent().includes("+json.dumps(marker+' saved')+")")
        front=page.context.browser.new_page()
        try:
            reply=front.goto(created['link'],wait_until='networkidle')
            result['httpStatus']=reply.status if reply else None
            result['layout']=front.evaluate('''({title,marker})=>({
              headers:new Set([...document.querySelectorAll('header')].map(e=>e.closest('.wp-block-template-part')||e)).size,
              footers:new Set([...document.querySelectorAll('footer')].map(e=>e.closest('.wp-block-template-part')||e)).size,
              titles:[...document.querySelectorAll('h1')].filter(e=>e.textContent.trim()===title).length,
              allH1:document.querySelectorAll('h1').length,
              body:[...document.querySelectorAll('.wp-block-post-content')].some(e=>e.textContent.includes(marker+' saved'))
            })''',{'title':title,'marker':marker})
            result['passed']=result['editorDefault']['mode']=='template-locked' and result['editorDefault']['template']==expected_template and result['httpStatus']==200 and all(result['layout'][key]==value for key,value in {'titles':1,'allH1':1,'body':True}.items()) and not result['roundtrip']['invalid'] and not result['roundtrip']['unknown'] and result['roundtrip']['textPersisted']
        finally:front.close()
    finally:
        deleted=page.request.delete(endpoint+f'/{created["id"]}?force=true',headers=headers)
        result['deleted']=deleted.ok and deleted.json().get('deleted') is True
        if not result['deleted']:raise RuntimeError('Cannot remove owned new-post fixture')
    return result


COUNT_ROOT = '''root=>{
  if(root.fallback)return new Set([...document.querySelectorAll(root.tag)].map(e=>e.closest('.wp-block-template-part')||e)).size;
  return [...document.querySelectorAll(root.tag)].filter(e=>(!root.anchor||e.id===root.anchor)&&root.classes.every(c=>e.classList.contains(c))).length;
}'''


def reference_page(page,args,headers):
    """The path of a published page on the default page template (not the
    front page, which may render a header variant), else the front page."""
    settings=page.request.get(args.site+'/wp-json/wp/v2/settings',headers=headers)
    front=settings.json().get('page_on_front') if settings.ok else None
    pages=page.request.get(args.site+'/wp-json/wp/v2/pages?context=edit&status=publish&per_page=100&orderby=id&order=asc',headers=headers)
    for row in (pages.json() if pages.ok else []):
        if row.get('id')!=front and not row.get('template') and row.get('link'):
            return urlparse(row['link']).path or '/'
    return '/'


def new_page_gate(page,args,nonce):
    """Create an owned draft page, prove it inherits the shared chrome once, then remove it."""
    if not args.theme_dir or not getattr(args,'package',None):raise RuntimeError('New-page chrome proof requires --theme-dir')
    roots={name:args.package.part_root(Path(args.theme_dir),name) for name in ('header','footer')}
    suffix=uuid.uuid4().hex[:12];title='Page verification '+suffix
    marker='Native page body '+suffix
    raw='<!-- wp:paragraph --><p>'+marker+'</p><!-- /wp:paragraph -->'
    headers={'X-WP-Nonce':nonce};endpoint=args.site+'/wp-json/wp/v2/pages'
    response=page.request.post(endpoint,headers=headers,data={'title':title,'slug':'h2wp-fixture-page-'+suffix,'status':'draft','template':'','content':raw})
    if not response.ok:raise RuntimeError('Cannot create owned new-page fixture')
    created=response.json()
    preview=args.site+f'/?page_id={created["id"]}&preview=true'
    result={'id':created['id'],'kind':'page','status':created.get('status'),'template':created.get('template',''),'previewUrl':preview,'passed':False,'deleted':False,'chrome':{}}
    try:
        # Same authenticated session: a draft preview is private to its editor.
        # browser.new_page() owns its context, so copy its cookies instead.
        preview_context=page.context.browser.new_context(storage_state=page.context.storage_state())
        front=preview_context.new_page()
        try:
            # The reference is a page on the default page template: the front
            # page may render its own header variant (header-2).
            reference=reference_page(page,args,headers)
            result['referencePath']=reference
            reply=front.goto(args.site+reference,wait_until='networkidle')
            if not reply or reply.status!=200:raise RuntimeError('Reference page is unavailable for chrome parity')
            front_counts={name:front.evaluate(COUNT_ROOT,root) for name,root in roots.items() if root}
            reply=front.goto(preview,wait_until='networkidle')
            result['httpStatus']=reply.status if reply else None
            result['layout']=front.evaluate('''marker=>({
              paragraph:[...document.querySelectorAll('.wp-block-post-content')].some(e=>e.textContent.includes(marker)),
              occurrences:document.body.innerText.split(marker).length-1
            })''',marker)
            for name,root in roots.items():
                if root:result['chrome'][name]={'root':root,'count':front.evaluate(COUNT_ROOT,root),'frontPageCount':front_counts[name]}
        finally:preview_context.close()
        chrome_ok=all((row['count']>=1 and (row['frontPageCount'] is None or row['count']==row['frontPageCount'])) if row['root']['fallback'] else (row['count']==1 and row['frontPageCount'] in (1,None)) for row in result['chrome'].values())
        result['passed']=bool(result['status']=='draft' and result['template']=='' and result['httpStatus']==200 and result['layout']['paragraph'] and chrome_ok)
    finally:
        deleted=page.request.delete(endpoint+f'/{created["id"]}?force=true',headers=headers)
        result['deleted']=deleted.ok and deleted.json().get('deleted') is True
        if not result['deleted']:raise RuntimeError('Cannot remove owned new-page fixture')
    return result


# Every wp-admin screen's body carries the WordPress version it runs
# (`version-7-1-2`, admin-header.php); the report records it.
ADMIN_VERSION=re.compile(r'(?:^|\s)version-(\d+(?:-\d+)*)(?=\s|$)')


def wordpress_version(page):
    """The WordPress version of the admin screen `page` shows ('7.1.2'), or
    None when it cannot be read: the version is recorded, never checked."""
    try:
        page.wait_for_load_state('domcontentloaded')
        found = ADMIN_VERSION.search(page.evaluate('document.body ? document.body.className : ""') or '')
        return found.group(1).replace('-', '.') if found else None
    except Exception:
        return None


def sign_in(page, args):
    """Log `page` in to wp-admin; returns the REST nonce of that session and
    notes the WordPress version it signed in to (args.wordpress_version)."""
    page.goto(args.site + '/wp-login.php')
    fill_login(page, args.user, args.password)  # read back and retried: lib/capture_ready.py
    page.locator('#wp-submit').click()
    page.wait_for_url('**/wp-admin/**')
    args.wordpress_version = wordpress_version(page)
    return page.request.get(args.site + '/wp-admin/admin-ajax.php?action=rest-nonce').text().strip()


def sign_in_only(args):
    """The session the editor gate would leave behind (args.auth_state and
    args.rest_nonce), for a smoke run whose editor visual phase comes first."""
    with sync_playwright() as pw:
        browser = launch_chromium(pw)
        try:
            page = browser.new_page()
            args.rest_nonce = sign_in(page, args)
            args.auth_state = page.context.storage_state()
        finally:
            browser.close()


def editor_gate(args):
    reports = []
    # One "serialization" row per surface with block markup (SERIALIZATION).
    args.serialization_result = serialization = []
    with sync_playwright() as pw:
        browser = launch_chromium(pw)
        page = browser.new_page()
        nonce = sign_in(page, args)
        active = page.request.get(args.site + '/wp-json/wp/v2/themes?status=active', headers={'X-WP-Nonce':nonce})
        if not active.ok or not any(t.get('stylesheet')==args.theme_slug for t in active.json()):
            raise RuntimeError('The expected generated theme is not active on localhost')
        def integrity():
            installed_integrity(page.request,args,nonce)
        if args.theme_dir: integrity()
        rows = []
        for kind in ('pages', 'posts'):
            for number in range(1, 10000):
                response = page.request.get(args.site + f'/wp-json/wp/v2/{kind}?context=edit&per_page=100&page={number}', headers={'X-WP-Nonce':nonce})
                if not response.ok:
                    raise RuntimeError(f'{kind} inventory HTTP {response.status}: {response.text()[:200]}')
                rows.extend(response.json())
                if number >= int(response.headers.get('x-wp-totalpages', '1')):
                    break
        status=page.request.get(args.site+'/wp-json/h2wp-gb/v1/import-status',headers={'X-WP-Nonce':nonce})
        allowed=source_paths(args,status.json().get('entities') if status.ok else None)
        if allowed is not None: rows=[row for row in rows if urlparse(row['link']).path.strip('/') in allowed]
        if not rows:
            raise RuntimeError('No WordPress pages/posts to inspect')
        for row in rows:
            if not row.get('content', {}).get('raw', '').strip():
                # WooCommerce's shop archive can legitimately have an empty
                # page body: its content is supplied by the block template.
                reports.append({'id':row['id'],'slug':row['slug'],'kind':row['type'],
                    'path':urlparse(row['link']).path,'count':0,'invalid':[],'unknown':[],'empty':True})
                continue
            page.goto(args.site + f'/wp-admin/post.php?post={row["id"]}&action=edit')
            page.wait_for_function("window.wp && wp.data && wp.data.select('core/editor') && wp.data.select('core/editor').getCurrentPostId() && wp.data.select('core/block-editor').getBlocks().length > 0", timeout=60000)
            edit_post_body(page)
            result = inspect_tree(page, "wp.data.select('core/block-editor').getBlocks()")
            result.update(id=row['id'], slug=row['slug'], kind=row['type'], path=urlparse(row['link']).path)
            serialization.append(serialization_row(page, row['content']['raw'], kind=row['type'], id=row['id'], slug=row['slug'], path=result['path']))
            if args.roundtrip_gates and not result['invalid'] and not result['unknown']:
                # This flag is only for the throwaway fixture DB. Restore the
                # original serialized content in finally, including on failure.
                original = row['content']['raw']
                try:
                    changed = page.evaluate('''() => {
                      const edits=[], marker=' Roundtrip check';
                      const all=[];const walk=bs=>{for(const b of bs){all.push(b);walk(b.innerBlocks||[])}};
                      walk(wp.data.select('core/block-editor').getBlocks());
                      const edit=(b,key,kind)=>{if(!b)return;const value=String(b.attributes[key]||'')+marker;
                        wp.data.dispatch('core/block-editor').updateBlockAttributes(b.clientId,{[key]:value});edits.push({kind,key,value});};
                      edit(all.find(b=>['core/heading','core/paragraph'].includes(b.name)),'content','text');
                      edit(all.find(b=>b.name==='core/image'),'alt','imageAlt');
                      edit(all.find(b=>b.name==='core/navigation-link'),'label','menuLabel');
                      const faq=all.find(b=>b.name==='h2wp/element' && Object.hasOwn(b.attributes.htmlAttributes||{},'data-h2wp-accordion'));
                      if(faq){const panel=all.find(b=>b.name==='h2wp/element' && (b.attributes.anchor||b.attributes.htmlAttributes?.id)===faq.attributes.htmlAttributes['aria-controls']);
                        const find=bs=>{for(const b of bs){if(b.name==='core/paragraph')return b;const c=find(b.innerBlocks||[]);if(c)return c}};
                        if(panel)edit(find(panel.innerBlocks||[]),'content','faqAnswer');}
                      return edits;
                    }''')
                    page.evaluate("async () => { await wp.data.dispatch('core/editor').savePost(); }")
                    page.reload()
                    page.wait_for_function("window.wp && wp.data && wp.data.select('core/block-editor').getBlocks().length > 0", timeout=60000)
                    edit_post_body(page)
                    result['roundtrip'] = inspect_tree(page, "wp.data.select('core/block-editor').getBlocks()")
                    result['roundtrip']['edits']=changed
                    result['roundtrip']['textPersisted'] = page.evaluate('''edits=>{
                      const all=[];const walk=bs=>{for(const b of bs){all.push(b);walk(b.innerBlocks||[])}};
                      walk(wp.data.select('core/block-editor').getBlocks());
                      return edits.every(e=>all.some(b=>String(b.attributes[e.key]||'')===e.value));
                    }''',changed)
                finally:
                    restored = page.request.post(args.site + f'/wp-json/wp/v2/{"posts" if row["type"]=="post" else "pages"}/{row["id"]}', headers={'X-WP-Nonce':nonce}, data={'content':original})
                    if not restored.ok: raise RuntimeError('Could not restore fixture after edit test')
            reports.append(result)
            print(f'editor {row["slug"]}: {result["count"]} blocks, {len(result["invalid"])} invalid', flush=True)
        if args.roundtrip_gates:
            args.new_post_result=new_post_gate(page,args,nonce)
            args.new_page_result=new_page_gate(page,args,nonce)
        # Parsing here uses the actual registered JavaScript block definitions;
        # PHP parse_blocks alone cannot detect save() mismatches.
        page.goto(args.site + '/wp-admin/site-editor.php?postType=wp_template&canvas=edit')
        page.wait_for_function("window.wp && wp.blocks && wp.blocks.getBlockType('core/template-part')", timeout=60000)
        page.wait_for_timeout(1000)
        for kind in ('templates', 'template-parts'):
            response = page.request.get(args.site + f'/wp-json/wp/v2/{kind}?context=edit&per_page=100', headers={'X-WP-Nonce':nonce})
            if not response.ok: raise RuntimeError(f'{kind}: HTTP {response.status}')
            for row in response.json():
                if row.get('theme') != args.theme_slug:
                    continue
                raw = row.get('content', {}).get('raw', '')
                page.evaluate('raw => {window.__h2wpCheck=wp.blocks.parse(raw)}', raw)
                result = inspect_tree(page, 'window.__h2wpCheck')
                result.update(id=row['id'], kind=kind)
                result['unresolvedTokens'] = bool(__import__('re').search(r'(?:asset:|page:)[A-Za-z0-9]', raw))
                reports.append(result)
                serialization.append(serialization_row(page, raw, kind=kind, id=row['id']))
        # WooCommerce manages product properties in its own editor. Validate
        # the native description with the actual Site Editor block registry.
        response = page.request.get(args.site + '/wp-json/wp/v2/product?context=edit&per_page=100', headers={'X-WP-Nonce':nonce})
        if response.status != 404:
            for number in range(1, int(response.headers.get('x-wp-totalpages','1')) + 1):
                if number > 1:
                    response = page.request.get(args.site + f'/wp-json/wp/v2/product?context=edit&per_page=100&page={number}', headers={'X-WP-Nonce':nonce})
                if not response.ok: raise RuntimeError('Cannot inventory product descriptions')
                for row in response.json():
                    if allowed is not None and urlparse(row['link']).path.strip('/') not in allowed: continue
                    raw = row.get('content',{}).get('raw','')
                    page.evaluate('raw => {window.__h2wpCheck=wp.blocks.parse(raw)}',raw)
                    result = inspect_tree(page,'window.__h2wpCheck')
                    result.update(id=row['id'],slug=row['slug'],kind='product',path=urlparse(row['link']).path)
                    # A description in block markup (one written as plain HTML is WooCommerce's own).
                    if '<!-- wp:' in raw: serialization.append(serialization_row(page, raw, kind='product', id=row['id'], slug=row['slug'], path=result['path']))
                    if args.roundtrip_gates and not result['invalid'] and not result['unknown']:
                        serialized=page.evaluate('wp.blocks.serialize(window.__h2wpCheck)')
                        endpoint=args.site+f'/wp-json/wp/v2/product/{row["id"]}'
                        try:
                            saved=page.request.post(endpoint,headers={'X-WP-Nonce':nonce},data={'content':serialized})
                            if not saved.ok: raise RuntimeError('Cannot save native product description')
                            restored_raw=page.request.get(endpoint+'?context=edit',headers={'X-WP-Nonce':nonce}).json()['content']['raw']
                            page.evaluate('raw => {window.__h2wpCheck=wp.blocks.parse(raw)}',restored_raw)
                            result['roundtrip']=inspect_tree(page,'window.__h2wpCheck')
                            result['roundtrip']['textPersisted']=restored_raw.strip()==serialized.strip()
                        finally:
                            restored=page.request.post(endpoint,headers={'X-WP-Nonce':nonce},data={'content':raw})
                            if not restored.ok: raise RuntimeError('Cannot restore product description')
                    reports.append(result)
        if args.theme_dir: integrity()
        args.auth_state=page.context.storage_state()
        args.rest_nonce=nonce
        browser.close()
    return reports


def basename(url):
    return urlparse(url or '').path.rsplit('/',1)[-1]


def capture(page, url):
    response = page.goto(url, wait_until='networkidle')
    if not response or response.status != 200: raise RuntimeError(f'{url}: HTTP {response.status if response else "none"}')
    # Instant scrolling, as the editor visual gate's ready(): a source
    # `scroll-behavior:smooth` animates each scrollTo below.
    page.add_style_tag(content='html,body{scroll-behavior:auto!important}')
    page.evaluate('''async () => {
      for(const image of document.images){image.loading='eager';image.removeAttribute('srcset');image.removeAttribute('sizes');}
      await document.fonts.ready;
      await Promise.all([...document.images].map(i=>i.decode().catch(()=>{})));
    }''')
    # Every reveal-on-scroll element in its end state before the scroll-through:
    # animations stop only after it, and one frozen mid-fade is half a picture.
    reveal_all(page)
    for y in range(0, page.evaluate('document.body.scrollHeight'), 700):
        page.evaluate('(y)=>scrollTo(0,y)', y)
        page.wait_for_timeout(80)
    page.evaluate('scrollTo(0,0)')
    page.wait_for_timeout(700)
    page.add_style_tag(content='*,*::before,*::after{animation:none!important;transition:none!important;caret-color:transparent!important}')
    return page.screenshot(full_page=True)


LAUNCH_ATTEMPTS=3
LAUNCH_RETRY_SECONDS=1.0


def launch_chromium(pw):
    """pw.chromium.launch(), tried again when the browser dies starting up.
    Chromium in the runtime container now and then crashes in its first
    moments (SIGSEGV before the first page, likelier with several workers
    starting at once): that says nothing about the theme, and the phase it
    belonged to must not fail over it. A browser that cannot start on the
    last attempt still fails the run."""
    for attempt in range(LAUNCH_ATTEMPTS):
        try:
            return pw.chromium.launch()
        except Exception:
            if attempt==LAUNCH_ATTEMPTS-1: raise
            time.sleep(LAUNCH_RETRY_SECONDS*(attempt+1))


def pool_map(fn, items, workers):
    """fn(browser, item) for every item, `workers` at a time; the results in
    item order. Each worker thread starts its own Playwright and Chromium and
    keeps them for all the items it takes (sync Playwright is bound to the
    thread that started it); each item opens its own context in it. A browser
    that died is launched again for the next item. The first failure in item
    order is raised once all items ran, as ThreadPoolExecutor.map raised it."""
    items=list(items);results,errors=[None]*len(items),{}
    queue=collections.deque(enumerate(items));lock=threading.Lock()
    def work():
        try:
            with sync_playwright() as pw:
                browser=None
                try:
                    while True:
                        with lock:
                            if not queue: return
                            index,item=queue.popleft()
                        try:
                            if browser is None or not browser.is_connected(): browser=launch_chromium(pw)
                            results[index]=fn(browser,item)
                        except Exception as error: errors[index]=error
                finally:
                    if browser is not None and browser.is_connected(): browser.close()
        except Exception as error:
            with lock: queue.clear()
            errors.setdefault(-1,error)
    threads=[threading.Thread(target=work,daemon=True) for _ in range(max(1,min(workers,len(items))))]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    # A worker that could not even start Playwright leaves items unmeasured.
    for index in range(len(items)):
        if index not in errors and results[index] is None and -1 in errors: errors[index]=errors[-1]
    errors.pop(-1,None)
    if errors: raise errors[min(errors)]
    return results


def source_digest(root):
    """sha256 over every file of the source directory: its path and bytes."""
    hashed=hashlib.sha256()
    for path in sorted(root.rglob('*')):
        if path.is_file():
            hashed.update(path.relative_to(root).as_posix().encode()+b'\0')
            hashed.update(hashlib.sha256(path.read_bytes()).digest())
    return hashed.hexdigest()


def source_cache(args, routes):
    """The source-side capture cache of this run: {dir, sourceDigest}, or
    {disabled: why}, or None without --source-dir.

    The source is the original static site and does not change between the
    reruns that follow each repair, so its captures are kept and reused. An
    entry is keyed by the digest of every source file, the capture code (the
    source of capture() and visual_case(), so any edit to how a page is
    captured is a new key), the Chromium version, the width and the page; a
    change to any of them is a miss. The server behind --source must be
    serving --source-dir: every route's source page is fetched and compared
    byte for byte first, or nothing is cached."""
    if not args.source_dir: return None
    root=Path(args.source_dir).resolve()
    for route in routes:
        rel=urlparse(route['source']).path.lstrip('/')
        target=root/rel
        if not rel or rel.endswith('/') or target.is_dir(): target=target/'index.html'
        try:
            with urlopen(args.source+quote(route['source'],safe='/%'),timeout=60) as reply: served=reply.read()
        except (OSError,ValueError) as error:
            return {'disabled':f'cannot read {route["source"]} from --source: {error}'}
        if not target.is_file() or target.read_bytes()!=served:
            return {'disabled':f'--source does not serve --source-dir: {route["source"]} differs'}
    digest=source_digest(root)
    code=hashlib.sha256((inspect.getsource(capture)+inspect.getsource(visual_case)).encode()).hexdigest()
    key=hashlib.sha256((digest+code).encode()).hexdigest()[:24]
    base=Path(args.out).parent/'.h2wp-capture-cache'
    # Only the entries of the current source and capture code are kept.
    for stale in (base.iterdir() if base.is_dir() else []):
        if stale.is_dir() and stale.name!=key and re.fullmatch(r'[0-9a-f]{24}',stale.name): shutil.rmtree(stale,ignore_errors=True)
    (base/key).mkdir(parents=True,exist_ok=True)
    return {'dir':str(base/key),'sourceDigest':digest,'captureCode':code}


def cached_source(args, browser, source_path, width):
    """(png path, broken-set path) of one source capture in the cache."""
    cache=getattr(args,'source_cache',None)
    if not cache or 'dir' not in cache: return None
    name=hashlib.sha256(json.dumps([browser.version,width,source_path]).encode()).hexdigest()[:32]
    return Path(cache['dir'])/(name+'.png'),Path(cache['dir'])/(name+'.json')


def visual_case(args, case, browser=None):
    width, source_path, target_path = case
    if browser is None:
        with sync_playwright() as pw:
            browser=launch_chromium(pw)
            try: return visual_case(args, case, browser)
            finally: browser.close()
    context=browser.new_context(viewport={'width':width,'height':900},device_scale_factor=1,reduced_motion='reduce')
    try:
        page=context.new_page()
        images="[...document.images].map(i=>({src:i.currentSrc||i.src,decoded:i.complete&&i.naturalWidth>0,alt:i.alt}))"
        entry=cached_source(args,browser,source_path,width)
        if entry and entry[0].is_file() and entry[1].is_file():
            original=entry[0].read_bytes();broken=set(json.loads(entry[1].read_text())['broken']);source_capture='cached'
        else:
            original=capture(page,args.source+source_path)
            # An image file the source page itself cannot load (one it never
            # shipped) is carried through, not a conversion failure; a post
            # query repeats its card's image for every post.
            broken={basename(i['src']) for i in page.evaluate(images) if not i['decoded']}
            source_capture='fresh'
            if entry:
                # Written aside and renamed: a concurrent or interrupted run
                # never reads half an entry. The PNG goes last: it marks the entry.
                for path,data in ((entry[1],json.dumps({'broken':sorted(broken),'source':source_path,'width':width}).encode()),(entry[0],original)):
                    temp=path.with_name(path.name+'.'+str(threading.get_ident())+'.tmp');temp.write_bytes(data);temp.replace(path)
        actual=capture(page,args.site+target_path)
        behavior={'menus':[],'images':page.evaluate(images),'disclosures':[]}
        for image in behavior['images']:
            if not image['decoded'] and basename(image['src']) in broken: image['sourceBroken']=True
        controls=page.locator('[data-h2wp-accordion][aria-controls]')
        for index in range(controls.count()):
            control=controls.nth(index);panel_id=control.get_attribute('aria-controls')
            if control.get_attribute('aria-expanded')=='true': control.click()
            control.click();page.wait_for_timeout(250)
            panel=page.locator('[id='+json.dumps(panel_id)+']')
            behavior['disclosures'].append({'id':panel_id,'expanded':control.get_attribute('aria-expanded')=='true','visible':panel.is_visible(),'text':bool(panel.inner_text().strip())})
        page.evaluate('scrollTo(0,0)')
        menu_controls=page.locator('header button[data-spa-toggle][aria-label*="menu" i],header .wp-block-navigation__responsive-container-open')
        for index in range(menu_controls.count()):
            control=menu_controls.nth(index)
            if not control.is_visible(): continue
            # Source mobile panels are often plain link lists, not <nav> (1:1 frame).
            links=page.locator('header nav a[href], header [data-spa-panel] a[href]')
            hidden=[i for i in range(links.count()) if not links.nth(i).is_visible()]
            control.click();page.wait_for_timeout(150)
            revealed=[i for i in hidden if links.nth(i).is_visible()]
            actionable=False
            if revealed:
                links.nth(revealed[0]).click(trial=True);actionable=True
            behavior['menus'].append({'label':control.get_attribute('aria-label'),'revealedLinks':len(revealed),'linkActionable':actionable})
            # Continue other cases from a fresh page; no menu/FAQ states are saved.
        behavior['passed']=all(i['decoded'] or i.get('sourceBroken') for i in behavior['images']) and all(d['expanded'] and d['visible'] and d['text'] for d in behavior['disclosures']) and all(m['linkActionable'] for m in behavior['menus'])
    finally: context.close()
    a=np.array(Image.open(io.BytesIO(original)).convert('RGB'))
    b=np.array(Image.open(io.BytesIO(actual)).convert('RGB'))
    shape=(max(a.shape[0],b.shape[0]),max(a.shape[1],b.shape[1]),3)
    aa=np.full(shape,255,dtype=np.uint8);bb=aa.copy()
    aa[:a.shape[0],:a.shape[1]]=a;bb[:b.shape[0],:b.shape[1]]=b
    changed=np.max(np.abs(aa.astype(int)-bb.astype(int)),axis=2)>16
    fraction=float(changed.mean())
    stem=str(width)+'-'+(target_path.strip('/').replace('/','-') or 'home')
    out=Path(args.out).parent/'screenshots';out.mkdir(exist_ok=True,parents=True)
    (out/(stem+'-source.png')).write_bytes(original);(out/(stem+'-wp.png')).write_bytes(actual)
    print(f'visual {target_path} {width}: {fraction:.3%}',flush=True)
    row={'path':target_path,'width':width,'diff':fraction,'behavior':behavior,'passed':fraction<=args.threshold and behavior['passed'],
         'sourceScreenshot':str(out/(stem+'-source.png')),'wpScreenshot':str(out/(stem+'-wp.png'))}
    if getattr(args,'source_cache',None) and 'dir' in args.source_cache: row['sourceCapture']=source_capture
    return row


def route_coverage(args, routes):
    """Every imported post and the blog listing must be a visual route.

    The routes file is written by hand, and a blog whose posts and listing are
    simply left out of it is never compared at all: a build whose images were
    never fetched then passes. The required set comes from the theme's own
    bundle (the pages it imports as posts, and its blog listing), or, without
    --theme-dir, from the site's published posts."""
    norm = lambda path: '/' + urlparse(path).path.strip('/') + ('/' if urlparse(path).path.strip('/') else '')
    targets = {norm(r['target']) for r in routes}
    required = set()
    bundle = Path(args.theme_dir) / 'content/content.json' if args.theme_dir else None
    if bundle and bundle.exists():
        data = json.loads(bundle.read_text())
        for page in (data.get('pages') if isinstance(data, dict) else data) or []:
            if page.get('kind') in ('post', 'blog') and page.get('slug') not in (None, '', '/'):
                required.add(norm(page['slug']))
    else:
        with sync_playwright() as pw:
            request = pw.request.new_context()
            try:
                reply = request.get(args.site + '/wp-json/wp/v2/posts?per_page=100&_fields=link')
                for post in (reply.json() if reply.ok else []):
                    required.add(norm(post['link']))
            finally:
                request.dispose()
    missing = sorted(required - targets)
    return {'required': len(required), 'missing': missing, 'passed': not missing}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--site',type=local,required=True)
    parser.add_argument('--source',type=local)
    parser.add_argument('--routes',help='JSON array of {source,target}, excluding intentional commerce redesigns')
    parser.add_argument('--user',default='admin');parser.add_argument('--password',required=True)
    parser.add_argument('--theme-slug',required=True);parser.add_argument('--out',required=True)
    parser.add_argument('--theme-dir',help='Fingerprint the local generated theme for packaging')
    parser.add_argument('--theme-report',help='Compiler theme-report.json (default: <workspace>/theme-report.json beside theme/<slug>)')
    parser.add_argument('--edit-roundtrip',action='store_true',help='Also run save/reload, new-post and new-page gates (throwaway DB only)')
    parser.add_argument('--threshold',type=float,default=.01)
    parser.add_argument('--skip-editor',action='store_true')
    parser.add_argument('--scope',choices=('full','smoke'),default='full',
        help='smoke: 1440px only; visual, then editor visual, then the editor gate without the --edit-roundtrip gates. A smoke report is never packaging evidence')
    parser.add_argument('--workers',type=int,default=3,help='Captures at once in the visual and editor visual phases (default 3)')
    parser.add_argument('--source-dir',help='The directory --source serves: its captures are cached across runs in .h2wp-capture-cache beside --out')
    args=parser.parse_args();report={'schema':'h2wp-local-verification/2','scope':args.scope,'passed':False,'editor':[],'serialization':[],'visual':[],'editorVisual':[],'threshold':args.threshold}
    if not 0 <= args.threshold <= .01: parser.error('Visual threshold must be between 0 and 0.01')
    if args.workers<1: parser.error('--workers must be at least 1')
    # A smoke run answers "is the layout right at the widest width, and does
    # the editor accept every block" in one pass while repairing. It measures
    # nothing a full run does not; it only measures less, so its report is a
    # diagnosis, never evidence: gutenberg-package.py refuses any scope but full.
    widths=(1440,) if args.scope=='smoke' else (1440,820,390)
    # --edit-roundtrip still says the database is a throwaway fixture (the
    # editor visual inventory reads it); its save/reload gates are full only.
    args.roundtrip_gates=args.edit_roundtrip and args.scope=='full'
    # The index of the visual phase's captures (compare-pages.py
    # --from-captures reads it) is written only once that whole phase has
    # written them, and an earlier run's goes first: a run that stops before
    # that, in any phase, leaves none to be taken for this one's.
    index=Path(args.out).parent/'screenshots'/'captures.json'
    index.unlink(missing_ok=True)
    try:
        if args.theme_dir:
            spec=importlib.util.spec_from_file_location('h2wp_package',Path(__file__).with_name('gutenberg-package.py'))
            package=importlib.util.module_from_spec(spec);spec.loader.exec_module(package)
            report['themeDigest']=package.theme_digest(Path(args.theme_dir).resolve())
            args.expected_digest=report['themeDigest']
            args.package=package
            theme_report=package.theme_report_for(Path(args.theme_dir).resolve(),args.theme_report)
            if theme_report.get('contractSchema'):report['contractSchema']=theme_report['contractSchema']
        def editor_phase():
            report['editor']=editor_gate(args)
            report['serialization']=args.serialization_result
            if hasattr(args,'new_post_result'):report['newPost']=args.new_post_result
            if hasattr(args,'new_page_result'):report['newPage']=args.new_page_result
        def editor_visual_phase():
            spec=importlib.util.spec_from_file_location('h2wp_editor_visual',Path(__file__).with_name('gutenberg-editor-visual.py'))
            editor_visual=importlib.util.module_from_spec(spec);spec.loader.exec_module(editor_visual)
            with sync_playwright() as pw:
                request=pw.request.new_context(storage_state=args.auth_state)
                try:
                    items=editor_visual.inventory(args,request)
                    status=request.get(args.site+'/wp-json/h2wp-gb/v1/import-status',headers={'X-WP-Nonce':args.rest_nonce})
                    if not status.ok: raise RuntimeError('Cannot verify import completion: HTTP '+str(status.status))
                    report['import']=status.json()
                    imported=report['import']
                    imported['passed']=bool(imported.get('schema')=='h2wp-import-status/1' and imported.get('complete') is True and imported.get('phase')=='done' and not imported.get('pending') and not imported.get('errors') and imported.get('counts')==imported.get('importedCounts') and imported.get('bundleDigest')==imported.get('stateDigest'))
                    if args.theme_dir:
                        bundle=Path(args.theme_dir)/'content/content.json'
                        imported['passed']=imported['passed'] and imported.get('bundleSha256')==hashlib.sha256(bundle.read_bytes()).hexdigest()
                        screenshot=Path(args.theme_dir)/'screenshot.png'
                        info=package.screenshot_info(screenshot)
                        remote_url=args.site+'/wp-content/themes/'+args.theme_slug+'/screenshot.png'
                        remote=request.get(remote_url)
                        if not remote.ok: raise RuntimeError('Installed screenshot.png is unavailable')
                        sha=hashlib.sha256(screenshot.read_bytes()).hexdigest()
                        installed_sha=hashlib.sha256(remote.body()).hexdigest()
                        report['preview']={'sha256':sha,'installedSha256':installed_sha,'width':info['width'],'height':info['height'],'format':'PNG','nonblank':True,'sourceUrl':args.site+'/','remoteUrl':remote_url,'passed':sha==installed_sha}
                finally: request.dispose()
            if not items: raise RuntimeError('No real editor visual surfaces')
            # One editor load per surface, its canvas resized widest first
            # (gutenberg-editor-visual.py surface), rows in the same order.
            surfaces=[item for item in items if not item.get('templateOverride')]
            report['editorVisual']=[row for rows in pool_map(lambda browser,item:editor_visual.surface(browser,args,item,widths),surfaces,args.workers) for row in rows]
            for item in items:
                if item.get('templateOverride'):report['editorVisual'].extend(editor_visual.fallback_cases(args,item,widths))
        def visual_phase():
            if args.source and args.routes:
                routes=json.loads(Path(args.routes).read_text())
                if not routes: raise RuntimeError('No visual routes')
                cases=[(width,r['source'],r['target']) for r in routes for width in widths]
                args.source_cache=source_cache(args,routes)
                report['visual']=pool_map(lambda browser,case:visual_case(args,case,browser),cases,args.workers)
                digest=lambda path:hashlib.sha256(Path(path).read_bytes()).hexdigest()
                index.write_text(json.dumps({'schema':'h2wp-captures/1','site':args.site,'source':args.source,'scope':args.scope,'pairs':[
                    {'source':source,'target':target,'width':width,'sourcePng':Path(row['sourceScreenshot']).name,'wpPng':Path(row['wpScreenshot']).name,
                     'sha256':{'source':digest(row['sourceScreenshot']),'wp':digest(row['wpScreenshot'])}}
                    for (width,source,target),row in zip(cases,report['visual'])]},indent=2))
                if args.source_cache:
                    report['sourceCache']={k:v for k,v in args.source_cache.items() if k!='captureCode'}
                    if 'dir' in args.source_cache:
                        report['sourceCache'].update(hits=sum(r.get('sourceCapture')=='cached' for r in report['visual']),misses=sum(r.get('sourceCapture')=='fresh' for r in report['visual']))
                report['routeCoverage']=route_coverage(args,routes)
        if args.scope=='smoke':
            if not args.skip_editor:
                # Signed in (and the installed theme proven) first: the editor
                # visual phase needs the session, and a stale install fails
                # here in seconds rather than after the captures.
                sign_in_only(args)
                if args.theme_dir:
                    with sync_playwright() as pw:
                        request=pw.request.new_context(storage_state=args.auth_state)
                        try: installed_integrity(request,args,args.rest_nonce)
                        finally: request.dispose()
            visual_phase()
            if not args.skip_editor:
                editor_visual_phase()
                editor_phase()
        else:
            if not args.skip_editor:
                editor_phase()
                editor_visual_phase()
            visual_phase()
        if getattr(args,'installed_digest',None): report['installedThemeDigest']=args.installed_digest
        if args.theme_dir and getattr(args,'auth_state',None):
            with sync_playwright() as pw:
                request=pw.request.new_context(storage_state=args.auth_state)
                try: installed_integrity(request,args,args.rest_nonce)
                finally: request.dispose()
        if not report['editor'] and not report['visual']: raise RuntimeError('No gates executed')
        report['passed']=(args.skip_editor or (bool(report.get('serialization')) and all(r['passed'] for r in report['serialization']))) and all(not r['invalid'] and not r['unknown'] and not r.get('unresolvedTokens') and
            (not r.get('roundtrip') or (not r['roundtrip']['invalid'] and not r['roundtrip']['unknown'] and r['roundtrip']['textPersisted'])) for r in report['editor']) and all(r['passed'] for r in report['visual']) and bool(report['editorVisual']) and all(r['passed'] for r in report['editorVisual']) and report.get('import',{}).get('passed',False) and report.get('preview',{}).get('passed',False) and report.get('routeCoverage',{}).get('passed',True) and (not args.roundtrip_gates or (report.get('newPost',{}).get('passed',False) and (report.get('contractSchema')!='h2wp-blocks/2' or report.get('newPage',{}).get('passed',False))))
        if args.theme_dir and report['themeDigest']!=package.theme_digest(Path(args.theme_dir).resolve()):
            raise RuntimeError('Theme changed during verification; rerun against a frozen build')
        if report['passed'] and args.theme_dir and args.scope=='full':
            package.validate_evidence(Path(args.theme_dir).resolve(),report,theme_report)
    except Exception as error:
        report['passed']=False
        report['error']=str(error)
    # The WordPress the gate measured (null: never signed in, or unreadable).
    report['wordpress']={'version':getattr(args,'wordpress_version',None)}
    Path(args.out).parent.mkdir(parents=True,exist_ok=True)
    Path(args.out).write_text(json.dumps(report,indent=2))
    print(json.dumps({k:v for k,v in report.items() if k not in ('editor','visual','editorVisual','import','serialization')}))
    return 0 if report['passed'] else 1


if __name__=='__main__': sys.exit(main())
