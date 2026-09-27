"""Capture-only TanStack build adaptation; call only on a disposable copy."""
import json
import re
from pathlib import Path

CAPTURE_NOTE = '// html2wp capture copy only: retain native SSR; omit deployment bundling.'
def has_cloudflare_nitro(text):
    # Treat strings/templates as single tokens; ignore commented examples.
    tokens = re.findall(r'''//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`|[\w$]+|[^\s]''', text)
    tokens = [t for t in tokens if not t.startswith(('//', '/*'))]
    return any(tokens[i:i+5] == ['nitro', ':', '{', 'preset', ':']
               and tokens[i+5] in ('"cloudflare-module"', "'cloudflare-module'")
               for i in range(max(0, len(tokens)-5)))


def lovable_version(work):
    try:
        package = json.loads((work / "package.json").read_text())
        deps = {**(package.get("dependencies") or {}), **(package.get("devDependencies") or {})}
        return deps.get("@lovable.dev/vite-tanstack-config")
    except (OSError, ValueError, TypeError):
        return None


class UnsupportedCaptureConfig(ValueError):
    pass


def already_adapted(text):
    return bool(re.match(
        r'\Aimport \{ defineConfig as (__h2wpCaptureDefineConfig_*) \} from "@lovable\.dev/vite-tanstack-config";\n'
        + re.escape(CAPTURE_NOTE) + r'\nconst ([A-Za-z_$][\w$]*) = \(options = \{\}\) => \1\(\{ \.\.\.options, nitro: false \}\);\n'
        r'\s*export\s+default\s+\2\s*\(\s*\{', text))


def lovable_capture_config(text, work):
    """Use Lovable v2's supported no-deploy mode, preserving its SSR options.

    Only the literal, single named-import/object-call form is rewritten.
    Unknown wrappers and function configs retain their existing behavior.
    This is not a deployment config or a rename of the Worker entrypoint.
    """
    version = lovable_version(work)
    if version != '2.5.3':
        return text, None
    # Only the explicit deployment preset known to replace the SSR entry.
    if not has_cloudflare_nitro(text):
        return text, None
    match = re.match(
        r"\A\s*import\s*\{\s*defineConfig(?:\s+as\s+([A-Za-z_$][\w$]*))?\s*\}"
        r"\s*from\s*(['\"])@lovable\.dev/vite-tanstack-config\2\s*;?", text)
    if not match:
        return text, None
    local = match.group(1) or "defineConfig"
    if not re.match(r"\s*export\s+default\s+" + re.escape(local) + r"\s*\(\s*\{", text[match.end():]):
        return text, None
    alias = "__h2wpCaptureDefineConfig"
    while alias in text:
        alias += "_"
    replacement = (
        f'import {{ defineConfig as {alias} }} from "@lovable.dev/vite-tanstack-config";\n'
        f'{CAPTURE_NOTE}\n'
        f'const {local} = (options = {{}}) => {alias}({{ ...options, nitro: false }});\n'
    )
    return replacement + text[match.end():], "Lovable v2 capture build: Nitro deployment adapter disabled"


TANSTACK_PRERENDER = "prerender: { enabled: true, crawlLinks: true }"


def enable_tanstack_prerender(work):
    """Turn on TanStack Start's static prerender in the build copy's Vite
    config. Returns what was done, or None when no config could be patched
    (the build then fails loudly on "no index.html", as before)."""
    for name in ("vite.config.ts", "vite.config.mts", "vite.config.js", "vite.config.mjs"):
        cfg = Path(work) / name
        if cfg.is_file() and not cfg.is_symlink():
            break
    else:
        return None
    text = cfg.read_text()
    text, capture_note = lovable_capture_config(text, Path(work))
    if lovable_version(Path(work)) and has_cloudflare_nitro(text) and not capture_note and not already_adapted(text):
        raise UnsupportedCaptureConfig(
            'Nitro cloudflare-module needs a compatible capture adapter; '
            'supported: @lovable.dev/vite-tanstack-config 2.5.3 with a direct '
            'named defineConfig import and literal export options. '
            'Configuration was left unchanged; do not force prerender on.')
    if re.search(r"prerender\s*:\s*\{[^}]*enabled\s*:\s*false", text):
        new = re.sub(r"(prerender\s*:\s*\{[^}]*enabled\s*:\s*)false", r"\1true", text, count=1)
        how = "prerender.enabled flipped to true"
    elif re.search(r"\bprerender\s*:", text):
        new = text
        how = "prerender already configured — left as authored"
    elif re.search(r"tanstackStart\s*:\s*\{", text):
        new = re.sub(r"(tanstackStart\s*:\s*\{)", r"\1 " + TANSTACK_PRERENDER + ",", text, count=1)
        how = "prerender added to tanstackStart: {…}"
    elif re.search(r"tanstackStart\(\s*\{", text):
        new = re.sub(r"(tanstackStart\(\s*\{)", r"\1 " + TANSTACK_PRERENDER + ",", text, count=1)
        how = "prerender added to tanstackStart({…})"
    elif re.search(r"tanstackStart\(\s*\)", text):
        new = re.sub(r"tanstackStart\(\s*\)", "tanstackStart({ " + TANSTACK_PRERENDER + " })", text, count=1)
        how = "prerender added to tanstackStart()"
    elif "@lovable.dev/vite-tanstack-config" in text and re.search(r"defineConfig\(\s*\{", text):
        new = re.sub(r"(defineConfig\(\s*\{)", r"\1 tanstackStart: { " + TANSTACK_PRERENDER + " },", text, count=1)
        how = "tanstackStart.prerender added to the Lovable config"
    elif "@lovable.dev/vite-tanstack-config" in text and re.search(r"defineConfig\(\s*\)", text):
        new = re.sub(r"defineConfig\(\s*\)", "defineConfig({ tanstackStart: { " + TANSTACK_PRERENDER + " } })", text, count=1)
        how = "tanstackStart.prerender added to the Lovable config"
    else:
        return None
    cfg.write_text(new)
    return f"{cfg.name}: {how}" + (f"; {capture_note}" if capture_note else "")
