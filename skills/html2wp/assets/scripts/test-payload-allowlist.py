"""What leaves the user's machine — the allowlist, and the sandbox's copy of it.

Two gates decide what a conversion sends to the service and what an untrusted
build can even see:

    secret-filter.mjs   which files are copied into astro-project/ and so into
                        the uploaded tarball.
    sandbox.py          which files are copied into the build sandbox, where
                        `npm run build` runs. Containing a build and then
                        handing it the secrets is not containment.

They are written in different languages against the same policy, which is
exactly the kind of pair that rots. The last section checks them against each
other rather than trusting that whoever edited one remembered the other.
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
import sandbox  # noqa: E402

failures = []


def check(label, condition):
    print(f"{'ok  ' if condition else 'FAIL'} — {label}")
    if not condition:
        failures.append(label)


def js_verdicts(names):
    """assetVerdict for each name, at the module's own default settings."""
    script = """
    import { assetVerdict, strictAssetMode } from './lib/secret-filter.mjs';
    const names = JSON.parse(process.env.NAMES);
    const out = {};
    for (const n of names) out[n] = assetVerdict(n, { strict: strictAssetMode() }).copy;
    console.log(JSON.stringify({ strict: strictAssetMode(), out }));
    """
    import os
    env = {**os.environ, "NAMES": json.dumps(names)}
    # H2WP_ASSET_ALLOWLIST is deliberately NOT set: the point is what the
    # module does when nobody has configured it.
    env.pop("H2WP_ASSET_ALLOWLIST", None)
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=HERE, capture_output=True, text=True, timeout=60, env=env,
    )
    if result.returncode != 0:
        print(result.stderr)
        raise SystemExit("could not run secret-filter.mjs")
    return json.loads(result.stdout)


SECRETS = [
    ".env", ".env.local", ".envrc", ".npmrc", ".netrc",
    "id_rsa", "server.pem", "app.key", "credentials.json",
    "secrets.yaml", "terraform.tfvars", "data.sqlite", "dump.sql",
    "backup.zip", "site.tar.gz", "db.bak",
]
ASSETS = [
    "index.css", "app.js", "bundle.mjs", "data.json", "hero.webp",
    "logo.svg", "inter.woff2", "promo.mp4", "captions.vtt",
    "stream.m3u8", "model.glb", "geometry.bin", "chair.usdz",
]
NOT_ASSETS = ["notes.md", "config.yml", "Dockerfile", "README", "script.py"]

print("== the upload filter (secret-filter.mjs) ==")
report = js_verdicts(SECRETS + ASSETS + NOT_ASSETS)
check("the web-asset allowlist is ON by default", report["strict"] is True)

for name in SECRETS:
    check(f"refuses {name}", report["out"][name] is False)
for name in ASSETS:
    check(f"still ships {name}", report["out"][name] is True)
for name in NOT_ASSETS:
    check(f"leaves out non-asset {name}", report["out"][name] is False)


print()
print("== the build sandbox (sandbox.py) ==")

with tempfile.TemporaryDirectory() as tmp:
    project = Path(tmp) / "project"
    (project / "src").mkdir(parents=True)
    (project / ".ssh").mkdir()
    (project / ".aws").mkdir()
    (project / "node_modules").mkdir()
    # These carry a key HEADER and no key material — they exist so the test
    # can prove the sandbox withholds them. Annotated for gitleaks, which
    # matches the header alone and would otherwise fail the publish gate on
    # every push, training everyone to ignore the one scanner that matters.
    for rel, body in [
        ("package.json", '{"name":"site"}'),
        ("index.html", "<html></html>"),
        ("vite.config.js", "export default {}"),
        ("src/main.js", "console.log(1)"),
        (".env", "STRIPE_SECRET=sk_live_x"),  # gitleaks:allow
        (".envrc", "export AWS_SECRET_ACCESS_KEY=x"),
        (".npmrc", "//registry:_authToken=x"),
        ("server.pem", "-----BEGIN PRIVATE KEY-----"),  # gitleaks:allow
        ("backup.zip", "PK"),
        ("dump.sql", "INSERT INTO users"),
        (".ssh/id_rsa", "-----BEGIN OPENSSH PRIVATE KEY-----"),  # gitleaks:allow
        (".aws/credentials", "[default]"),
        ("node_modules/dep.js", "module.exports={}"),
    ]:
        (project / rel).write_text(body)

    sandbox.withheld.clear()
    work, _deps = sandbox.prepare_workspace(project)
    present = {p.name for p in work.rglob("*") if p.is_file()}

    for leaked in [".env", ".envrc", ".npmrc", "server.pem", "backup.zip",
                   "dump.sql", "id_rsa", "credentials", "dep.js"]:
        check(f"the build cannot see {leaked}", leaked not in present)
    check("the .ssh directory is not there at all", not (work / ".ssh").exists())
    check("the .aws directory is not there at all", not (work / ".aws").exists())

    for needed in ["package.json", "index.html", "vite.config.js", "main.js"]:
        check(f"the build still gets {needed}", needed in present)

    check("what was withheld is reported, not silent", len(sandbox.withheld) > 0)


print()
print("== the two lists have not drifted apart ==")

# Every name the JS filter refuses by SHAPE must also be withheld from the
# sandbox. The reverse is not required: the sandbox additionally drops build
# scaffolding that the upload filter never sees.
sandbox_refuses = {
    name for name in SECRETS
    if name in sandbox._SECRET_DIRS or sandbox._SECRET_NAME_RE.match(name)
}
js_refuses = {name for name in SECRETS if report["out"][name] is False}
missing = js_refuses - sandbox_refuses
check(
    f"sandbox.py refuses everything secret-filter.mjs does (gap: {sorted(missing) or 'none'})",
    not missing,
)

print()
if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("ALL OK")
