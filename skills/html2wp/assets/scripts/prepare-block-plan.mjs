#!/usr/bin/env node
// Protected-repository-owned local orchestration entrypoint. No AI runs here.
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
const result = spawnSync('python3', [fileURLToPath(new URL('./gutenberg-plan.py', import.meta.url)), ...process.argv.slice(2)], { stdio: 'inherit' });
if (result.error) console.error(result.error.message);
process.exit(result.status ?? 1);
