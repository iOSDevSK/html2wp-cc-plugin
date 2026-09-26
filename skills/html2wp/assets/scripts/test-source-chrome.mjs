import assert from 'node:assert/strict';
import {mkdtempSync, mkdirSync, writeFileSync, readFileSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join, dirname} from 'node:path';
import {fileURLToPath} from 'node:url';
import {execFileSync} from 'node:child_process';
import {headerCandidate} from './lib/source-chrome.mjs';

const header = '<div class="sticky top-0 z-50"><p>Contact</p><nav><a href="about.html">About</a></nav><div hidden>Mobile drawer</div></div>';
const main = '<main><h1>Page</h1></main>';
assert.equal(headerCandidate(header + main).selector, 'div.sticky');
assert.equal(headerCandidate('<header>Semantic</header>' + header + main), null);
assert.equal(headerCandidate('<main>' + header + '</main>'), null);
assert.equal(headerCandidate('<div class="sticky top-0">' + header + main + '</div>').selector, 'div.z-50');
assert.equal(headerCandidate('<div><nav>Links</nav></div>' + main), null);
assert.equal(headerCandidate('<div class="masthead" role="banner"><nav>Links</nav></div>' + main).selector, 'div.masthead');
assert.equal(headerCandidate('<div role="banner"><nav>Links</nav></div><div></div>' + main), null);
assert.equal(headerCandidate('<div class="wrapper" data-role="banner"><nav>Links</nav></div>' + main), null);
assert.equal(headerCandidate('<div class = "wrapper" role = "banner"><nav>Links</nav></div>' + main).selector, 'div.wrapper');
for (const tag of ['script','style','template','textarea','noscript']) {
  assert.equal(headerCandidate('<'+tag+'><div class="header"><nav>Example</nav></div></'+tag+'><div><nav>Real</nav></div>'+main),null);
}
assert.equal(headerCandidate('<script>const example=`<div class="header"><nav>demo</nav></div>`;</script>'+header+main).selector,'div.sticky');
assert.equal(headerCandidate('<!-- <div class="header"><nav>Example</nav></div> -->'+header+main).selector,'div.sticky');
// A2 sees unmasked source: never select an identical example in a script.
assert.equal(headerCandidate('<script>const example=`'+header+'`;</script>'+header+main),null);
// A collision on the shortest selector can still allow a unique real node.
assert.equal(headerCandidate('<script>const example=`<div class="sticky"><nav>Example</nav></div>`;</script>'+header+main).selector,'div.top-0');
const here = dirname(fileURLToPath(import.meta.url));
const root = mkdtempSync(join(tmpdir(), 'h2wp-chrome-test-'));
try {
  const input = join(root,'input'), ws = join(root,'ws');mkdirSync(input);mkdirSync(ws);
  for (const [name,title] of [['index.html','Home'],['about.html','About']]) {
    writeFileSync(join(input,name), '<html><head><title>'+title+'</title></head><body>'+header+main.replace('Page',title)+'<footer>Footer</footer></body></html>');
  }
  const analysis = join(ws,'analysis.json');
  execFileSync('node',[join(here,'analyze-input.mjs'),input,'--out='+analysis]);
  execFileSync('python3',[join(here,'flash-manifest.py'),'--analysis',analysis,'--input',input,'--workspace',ws]);
  const manifest = JSON.parse(readFileSync(join(ws,'conversion-manifest.json')));
  assert.equal(manifest.chrome.header.selector,'div.sticky');
  assert.ok(manifest.pages.every(p=>p.chrome==='self-contained'));
  console.log('source chrome: selector boundaries, uniqueness and manifest coverage passed');
} finally {rmSync(root,{recursive:true,force:true});}
