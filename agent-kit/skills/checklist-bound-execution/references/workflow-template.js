// checklist-bound-execution — reusable Workflow template.
//
// Copy this, fill in CHECKLIST (embed it as a LITERAL — Workflow scripts have NO filesystem access
// and the `args` global is unreliable), and run with the Workflow tool. The worker/verifier
// contracts are inlined into the prompts so this runs even before the global agent defs are loaded;
// once `checklist-worker` / `checklist-verifier` are active, add `agentType:` to the agent() calls.
//
// Pattern: embed checklist -> group by file (avoid write races) -> pipeline(worker -> verifier)
// with bounded loop-back -> collect verdicts. Worker proof + verifier verdict are schema-validated.

export const meta = {
  name: 'checklist-bound-execution',
  description: 'Execute checklist items via workers bound to verbatim specs, each independently verified',
  phases: [
    { title: 'Execute' },
    { title: 'Verify' },
  ],
};

// ── 1) EMBED the checklist (the anti-drift fix: verbatim, in the script — not args, not fs) ──
const CHECKLIST = {
  run: 'EXAMPLE RUN',
  items: [
    // { id, title, files:[abs], spec, acceptance_criteria:[...], status:'todo', verify_only:false }
  ],
};

// Structured-output schemas (see references/schemas.md). Workers/verifiers return ARRAYS of these.
const PROOF = {
  type: 'object', additionalProperties: false,
  required: ['item_id', 'status', 'files_changed', 'change_summary', 'acceptance_self_check', 'evidence', 'deviations'],
  properties: {
    item_id: { type: 'string' },
    status: { type: 'string', enum: ['done', 'blocked', 'skipped'] },
    files_changed: { type: 'array', items: { type: 'string' } },
    change_summary: { type: 'string' },
    acceptance_self_check: { type: 'array', items: {
      type: 'object', additionalProperties: false, required: ['criterion', 'met', 'evidence'],
      properties: { criterion: { type: 'string' }, met: { type: 'boolean' }, evidence: { type: 'string' } } } },
    evidence: { type: 'string' },
    deviations: { type: 'string' },
  },
};
const VERDICT = {
  type: 'object', additionalProperties: false,
  required: ['item_id', 'pass', 'criteria_results', 'reason', 'must_fix'],
  properties: {
    item_id: { type: 'string' },
    pass: { type: 'boolean' },
    criteria_results: { type: 'array', items: {
      type: 'object', additionalProperties: false, required: ['criterion', 'met', 'evidence'],
      properties: { criterion: { type: 'string' }, met: { type: 'boolean' }, evidence: { type: 'string' } } } },
    reason: { type: 'string' },
    must_fix: { type: 'string' },
  },
};
// Anthropic structured-output tools require the schema ROOT to be type:object — so wrap the arrays
// in an object and read `.proofs` / `.verdicts` off the agent return.
const PROOFS = { type: 'object', additionalProperties: false, required: ['proofs'], properties: { proofs: { type: 'array', items: PROOF } } };
const VERDICTS = { type: 'object', additionalProperties: false, required: ['verdicts'], properties: { verdicts: { type: 'array', items: VERDICT } } };

// ── 2) Partition by file to avoid two workers editing one file concurrently ──
const groups = {};
for (const it of CHECKLIST.items) {
  const key = (it.files && it.files[0]) || it.id;
  (groups[key] ||= []).push(it);
}
const fileGroups = Object.entries(groups).map(([file, items]) => ({ file, items }));
const short = (f) => f.split('/').pop();

const WORKER_CONTRACT = [
  'You are a checklist-WORKER. Do ONLY the items below, exactly as specified. Do not work from',
  'memory or assumption — the spec and acceptance_criteria ARE the source of truth. Touch ONLY the',
  'named files. If an item is impossible or ambiguous as written, return status "blocked" with the',
  'reason instead of guessing. Make the MINIMAL change that satisfies EVERY acceptance criterion.',
  'Read each target file before editing. Anything changed beyond the literal spec is a deviation —',
  'report it; never expand scope, refactor, or "improve" unrelated code.',
].join('\n');

const VERIFIER_CONTRACT = [
  'You are a checklist-VERIFIER and you are READ-ONLY (do not edit — that is deliberate, so you',
  'cannot bias yourself by fixing). RE-READ the actual resulting file yourself (Read/Grep); treat the',
  "worker's proof as claims to scrutinise, NOT as truth. Adversarial: assume an item is NOT done",
  'until proven by evidence you observe directly. Default a criterion to not-met when evidence is',
  'missing or ambiguous. pass=true ONLY if every acceptance criterion is met; else give a precise must_fix.',
].join('\n');

const workerPrompt = (file, items) => [
  WORKER_CONTRACT, '',
  `FILE (your single writer-scope): ${file}`, '',
  'ITEMS (verbatim):', JSON.stringify(items, null, 2), '',
  'Return a "proofs" array (one PROOF object per item), each self-checking every acceptance',
  'criterion with concrete evidence (the new line(s) / a grep result).',
].join('\n');

const verifierPrompt = (file, items, proofs) => [
  VERIFIER_CONTRACT, '',
  `FILE to re-read: ${file}`, '',
  'ITEMS + acceptance_criteria (verbatim):', JSON.stringify(items, null, 2), '',
  'Worker-reported proofs (scrutinise, do not trust):', JSON.stringify(proofs ?? [], null, 2), '',
  'Return a "verdicts" array (one VERDICT object per item).',
].join('\n');

const MAX_RETRIES = 2;
// To use the global specialist agents once loaded, add `, agentType: 'checklist-worker'` (worker
// stages) and `, agentType: 'checklist-verifier'` (verify stages) to the opts below.

const results = await pipeline(
  fileGroups,
  // stage 1: worker applies the actionable (non-verify_only) items for this file
  (grp) => {
    const todo = grp.items.filter((it) => !it.verify_only);
    if (!todo.length) return { ...grp, proofs: [] };
    return agent(workerPrompt(grp.file, todo), { label: `worker:${short(grp.file)}`, phase: 'Execute', schema: PROOFS })
      .then((proofs) => ({ ...grp, proofs: proofs || [] }));
  },
  // stage 2: verify ALL items in the group, with bounded loop-back on failure
  async (grp) => {
    const { file, items } = grp;
    let proofs = grp.proofs;
    let verdicts = (await agent(verifierPrompt(file, items, proofs), { label: `verify:${short(file)}`, phase: 'Verify', schema: VERDICTS })) || [];
    for (let attempt = 1; attempt <= MAX_RETRIES; attempt++) {
      const failed = verdicts.filter((v) => v && v.pass === false);
      if (!failed.length) break;
      // only re-work failed items that are actually editable (not verify_only)
      const failItems = items.filter((it) => !it.verify_only && failed.some((v) => v.item_id === it.id));
      if (!failItems.length) break;
      const fixNotes = failed.map((v) => `- ${v.item_id}: ${v.must_fix}`).join('\n');
      log(`retry ${attempt} on ${short(file)}: ${failItems.map((i) => i.id).join(', ')}`);
      const fixed = (await agent(workerPrompt(file, failItems) + `\n\nVERIFIER REJECTED these; fix exactly:\n${fixNotes}`,
        { label: `fix:${short(file)}#${attempt}`, phase: 'Execute', schema: PROOFS })) || [];
      const reverdict = (await agent(verifierPrompt(file, failItems, fixed),
        { label: `reverify:${short(file)}#${attempt}`, phase: 'Verify', schema: VERDICTS })) || [];
      verdicts = verdicts.map((v) => reverdict.find((r) => r.item_id === v.item_id) || v);
    }
    return { file, verdicts };
  },
);

const allVerdicts = results.filter(Boolean).flatMap((r) => r.verdicts).filter(Boolean);
const passed = allVerdicts.filter((v) => v.pass);
const failing = allVerdicts.filter((v) => v.pass === false);
log(`done: ${passed.length}/${allVerdicts.length} items verified, ${failing.length} still failing`);
return { verified: passed.map((v) => v.item_id), failing, allVerdicts };
