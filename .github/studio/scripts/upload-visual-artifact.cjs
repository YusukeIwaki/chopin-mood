'use strict';
/*
 * Tiny trusted visual-artifact uploader for Studio.
 *
 * Called ONLY by the trusted Python controller (never by a model).
 * Uses the official @actions/artifact@2.3.2 CommonJS client:
 * DefaultArtifactClient.uploadArtifact(name, files, rootDirectory,
 * {retentionDays: 90}). Official references:
 * https://github.com/actions/toolkit/tree/main/packages/artifact
 * https://github.com/actions/upload-artifact
 *
 * Args: --dir DIR --name NAME --result RESULT_JSON
 * Validates root/files (allowlist, no symlinks, no traversal, no
 * review.json, no logs/auth) before uploading. Writes ONLY small
 * success metadata {ok,id,size,name} to the result path; SDK
 * stdout/stderr stay private to this process and failures are
 * reported as one safe fixed category.
 */
const fs = require('fs');
const path = require('path');

const NAME_RE = /^studio-visual-child-\d+-run-\d+-a\d+-v\d+-(still|motion)$/;
const FRAME_RE = /^frame-\d{6,}\.png$/;

function fail(resultPath) {
  try {
    if (resultPath) {
      fs.mkdirSync(path.dirname(resultPath), { recursive: true });
      fs.writeFileSync(resultPath,
        JSON.stringify({ ok: false, category: 'upload_failed' }) + '\n');
    }
  } catch (_) { /* result write is best-effort; exit code still fails */ }
  process.stderr.write('visual artifact upload failed (classification: upload_failed)\n');
  process.exit(2);
}

function argValue(argv, flag) {
  const i = argv.indexOf(flag);
  return i >= 0 && i + 1 < argv.length ? argv[i + 1] : '';
}

async function main() {
  const dir = argValue(process.argv, '--dir');
  const name = argValue(process.argv, '--name');
  const resultPath = argValue(process.argv, '--result');
  if (!dir || !name || !resultPath || !NAME_RE.test(name)) return fail(resultPath);
  let rootStat;
  try { rootStat = fs.lstatSync(dir); } catch (_) { return fail(resultPath); }
  if (rootStat.isSymbolicLink() || !rootStat.isDirectory()) return fail(resultPath);
  let entries;
  try { entries = fs.readdirSync(dir, { withFileTypes: true }); }
  catch (_) { return fail(resultPath); }
  const stage = name.endsWith('-motion') ? 'motion' : 'still';
  const files = [];
  for (const entry of entries) {
    const full = path.join(dir, entry.name);
    if (entry.isSymbolicLink()) return fail(resultPath);
    let st;
    try { st = fs.lstatSync(full); } catch (_) { return fail(resultPath); }
    if (st.isSymbolicLink() || !st.isFile()) return fail(resultPath);
    if (entry.name !== path.basename(entry.name) ||
        entry.name.includes('..') || entry.name.includes('/') ||
        entry.name.includes('\\')) return fail(resultPath);
    const allowed = entry.name === 'evidence.json' ||
      entry.name === 'contact-sheet.jpg' || FRAME_RE.test(entry.name) ||
      (stage === 'motion' && entry.name === 'preview.mp4');
    if (!allowed) return fail(resultPath); // review.json/logs/auth never upload here
    files.push(full);
  }
  if (files.length === 0) return fail(resultPath);
  files.sort();
  let artifact;
  try {
    artifact = require('@actions/artifact');
  } catch (_) {
    try {
      const tools = process.env.STUDIO_ARTIFACT_TOOLS || '';
      artifact = require(path.join(tools, 'node_modules', '@actions', 'artifact'));
    } catch (__) { return fail(resultPath); }
  }
  const client = new artifact.DefaultArtifactClient();
  let response;
  try {
    response = await client.uploadArtifact(name, files, dir, { retentionDays: 90 });
  } catch (_) {
    return fail(resultPath); // SDK output stays private; fixed category only
  }
  if (!response || response.id === undefined || response.id === null) {
    return fail(resultPath);
  }
  const meta = { ok: true, name, id: response.id,
                 size: response.size === undefined ? null : response.size };
  try {
    fs.mkdirSync(path.dirname(resultPath), { recursive: true });
    fs.writeFileSync(resultPath, JSON.stringify(meta) + '\n');
  } catch (_) { return fail(resultPath); }
  process.stdout.write(JSON.stringify({ ok: true, name, id: response.id }) + '\n');
}

main().catch(() => {
  const resultPath = argValue(process.argv, '--result');
  fail(resultPath);
});
