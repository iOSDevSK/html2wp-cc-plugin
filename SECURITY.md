# Security

## Reporting a vulnerability

Email **hello@html2wp.dev** with `SECURITY` in the subject.

Please include what you need to make the problem reproducible: the version or
commit, the steps, and what you expected instead. A proof of concept helps and
is welcome; you do not need one to report something.

**Please do not open a public GitHub issue for a security problem.** Issues
here are public from the moment they are filed, which puts every user of the
skill in front of the problem before there is anything to update to. The same
goes for the `/v1/report` endpoint described in the README — that is a
generator-defect mailbox, it is read by a person alongside ordinary bug
reports, and it is not the right channel for a vulnerability.

What to expect:

- an acknowledgement within **3 working days**;
- an assessment, and a rough timeline, within **10 working days**;
- credit in the release notes if you want it, and none if you would rather
  not.

This is a small operation, not a company with a security team. If a report
goes unanswered past those windows, send it again — it was missed, not
ignored.

## What is in scope

- This skill and the scripts in `skills/html2wp/assets/scripts/`, which run on
  your own machine.
- The conversion service at `api.html2wp.dev`.
- The WordPress theme the service generates, including the PHP runtime it
  ships and the content importer.

Things worth reporting even when they look small: anything that reads or
transmits files outside the input directory and the workspace, anything that
lets one conversion see another, anything in a generated theme that is
reachable without authentication, and anything that makes a finished
conversion hand over a theme it should have refused.

## What is out of scope

- Findings against a **WordPress, PHP or plugin version you chose to run**,
  where the fix is to update it.
- The throwaway Docker WordPress used by `test-env.sh` for local verification.
  It has a fixed admin password on purpose, binds only to `127.0.0.1`, and is
  destroyed after each run. It is a test fixture, not a deployment.
- Reports that a licence check can be removed from the client. It can — the
  client is yours and you can edit it. Nothing security-relevant depends on
  it: the service decides entitlement, and the parts you pay for are the parts
  it declines to produce without a key.
- Volumetric denial of service against `api.html2wp.dev`, and findings that
  amount to running a scanner against it.

## Testing against the service

Test against your own conversions and your own licence key. Do not attempt to
reach another account's jobs, artifacts or workspaces — if you believe you
have found a way to, that is exactly the report to send, and describing the
method is enough. There is no need to prove it on somebody else's data.

## What leaves your machine

Documented in the README under *How it works* and in `SKILL.md` under *What
goes up*, because a security policy is the wrong place to learn it for the
first time. In short: the **built site** is uploaded to the service, which is
what the conversion is performed on; the gate **verdicts** are reported
separately and are names and numbers only. Stage 1 filters credential-shaped
files out of the payload and names every one it dropped in `astro-report.json`
— if you find something that gets past that filter, it is in scope.
