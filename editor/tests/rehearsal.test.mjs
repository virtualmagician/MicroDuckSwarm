// Rehearsal tools — solo/mute (docs/authoring.md M3 rehearsal tools). Pure
// state logic (mixing-desk semantics: solo is exclusive and overrides mute
// without disturbing it) plus its effect on pose derivation: a soloed-out
// or muted role must resolve to the neutral standing pose at its mark, and
// none of this may ever be reachable from a serialized show — it is editor
// session state, not show content.
import { test, describe } from 'node:test';
import assert from 'node:assert/strict';

import { newShow, addKeyframe, addEvent, setMark, serializeShow, parseShow } from '../duckshow-core.js';
import {
  createRehearsalState, toggleSolo, toggleMute, clearSolo,
  resolveNeutralRoles, pruneRehearsalState, renameInRehearsalState,
  precomputeShowPaths, derivePose, deriveShowPoses, deriveFrame,
} from '../duckshow-viewer.js';

const ROLES = ['lead', 'wing', 'tail'];

// ---------------------------------------------------------------------------
// Pure state transitions
// ---------------------------------------------------------------------------

describe('rehearsal state — solo/mute transitions', () => {
  test('a fresh state has nothing soloed and nothing muted', () => {
    const s = createRehearsalState();
    assert.equal(s.solo, null);
    assert.equal(s.muted.size, 0);
  });

  test('toggleSolo solos a role, and solo is exclusive: soloing a different role replaces it, not adds to it', () => {
    let s = createRehearsalState();
    s = toggleSolo(s, 'lead');
    assert.equal(s.solo, 'lead');
    s = toggleSolo(s, 'wing');
    assert.equal(s.solo, 'wing', 'only one role is ever soloed at a time');
  });

  test('toggleSolo on the already-soloed role un-solos it', () => {
    let s = createRehearsalState();
    s = toggleSolo(s, 'lead');
    s = toggleSolo(s, 'lead');
    assert.equal(s.solo, null);
  });

  test('clearSolo forces solo off and is a no-op when nothing is soloed', () => {
    let s = toggleSolo(createRehearsalState(), 'lead');
    s = clearSolo(s);
    assert.equal(s.solo, null);
    const same = clearSolo(s);
    assert.equal(same, s, 'no-op clearSolo returns the identical state object');
  });

  test('toggleMute adds/removes a role from the muted set, independent of solo', () => {
    let s = createRehearsalState();
    s = toggleMute(s, 'wing');
    assert.ok(s.muted.has('wing'));
    s = toggleMute(s, 'tail');
    assert.deepEqual([...s.muted].sort(), ['tail', 'wing']);
    s = toggleMute(s, 'wing');
    assert.deepEqual([...s.muted], ['tail']);
  });

  test('toggling mute while a role is soloed does not touch solo, and vice versa', () => {
    let s = toggleSolo(createRehearsalState(), 'lead');
    s = toggleMute(s, 'wing');
    assert.equal(s.solo, 'lead');
    assert.ok(s.muted.has('wing'));
  });
});

// ---------------------------------------------------------------------------
// Solo overrides mute; un-solo restores prior mutes; solo is exclusive
// ---------------------------------------------------------------------------

describe('resolveNeutralRoles — the mixing-desk rule', () => {
  test('with nothing soloed or muted, nobody is neutral', () => {
    const s = createRehearsalState();
    assert.deepEqual([...resolveNeutralRoles(s, ROLES)], []);
  });

  test('muting holds exactly the muted roles neutral', () => {
    let s = createRehearsalState();
    s = toggleMute(s, 'wing');
    assert.deepEqual([...resolveNeutralRoles(s, ROLES)].sort(), ['wing']);
  });

  test('soloing holds every OTHER role neutral, regardless of any mute state', () => {
    let s = createRehearsalState();
    s = toggleMute(s, 'wing'); // wing already muted...
    s = toggleSolo(s, 'lead'); // ...but lead is soloed
    assert.deepEqual([...resolveNeutralRoles(s, ROLES)].sort(), ['tail', 'wing'], 'solo neutralizes everyone but the soloed role, including a role that was never muted');
  });

  test('solo overrides mute: a muted role that IS the soloed role is not neutral', () => {
    let s = createRehearsalState();
    s = toggleMute(s, 'lead');
    s = toggleSolo(s, 'lead'); // solo the very role that happens to be muted
    assert.deepEqual([...resolveNeutralRoles(s, ROLES)].sort(), ['tail', 'wing'], 'the soloed role always performs, even if its own mute switch is on');
  });

  test('un-soloing restores exactly the mute set that was in effect before solo, unchanged by the solo interlude', () => {
    let s = createRehearsalState();
    s = toggleMute(s, 'wing');
    s = toggleMute(s, 'tail');
    const beforeSolo = resolveNeutralRoles(s, ROLES);
    s = toggleSolo(s, 'lead');
    assert.deepEqual([...resolveNeutralRoles(s, ROLES)].sort(), ['tail', 'wing']);
    s = toggleSolo(s, 'lead'); // un-solo
    assert.deepEqual([...resolveNeutralRoles(s, ROLES)].sort(), [...beforeSolo].sort(), 'restored exactly the pre-solo mute set');
  });

  test('mutes changed WHILE soloed still take effect once un-soloed', () => {
    let s = createRehearsalState();
    s = toggleSolo(s, 'lead');
    s = toggleMute(s, 'tail'); // flipped while lead is soloed
    s = toggleSolo(s, 'lead'); // un-solo
    assert.deepEqual([...resolveNeutralRoles(s, ROLES)], ['tail']);
  });

  test('a stale soloed role no longer in the cast is treated as no solo', () => {
    const s = toggleSolo(createRehearsalState(), 'ghost');
    assert.deepEqual([...resolveNeutralRoles(s, ROLES)], []);
  });
});

// ---------------------------------------------------------------------------
// Cast-change hygiene: prune (role removed) and rename carry-over
// ---------------------------------------------------------------------------

describe('rehearsal state cast-change hygiene', () => {
  test('pruneRehearsalState drops a solo/mute referring to a role no longer in the cast', () => {
    let s = createRehearsalState();
    s = toggleSolo(s, 'lead');
    s = toggleMute(s, 'wing');
    s = toggleMute(s, 'ghost');
    s = pruneRehearsalState(s, ['lead', 'wing']);
    assert.equal(s.solo, 'lead');
    assert.deepEqual([...s.muted], ['wing']);
  });

  test('pruneRehearsalState is identity-preserving when nothing is stale', () => {
    let s = createRehearsalState();
    s = toggleMute(s, 'wing');
    assert.equal(pruneRehearsalState(s, ROLES), s);
  });

  test('renameInRehearsalState carries a solo and a mute reference across a rename', () => {
    let s = createRehearsalState();
    s = toggleSolo(s, 'lead');
    s = toggleMute(s, 'wing');
    s = renameInRehearsalState(s, 'lead', 'leader');
    s = renameInRehearsalState(s, 'wing', 'left-wing');
    assert.equal(s.solo, 'leader');
    assert.deepEqual([...s.muted], ['left-wing']);
  });
});

// ---------------------------------------------------------------------------
// Pose derivation: a neutral role stands, dimmed, on its mark
// ---------------------------------------------------------------------------

describe('neutral pose derivation', () => {
  function movingShow() {
    let show = newShow({ duration: 4, roles: ['lead', 'wing'] });
    show = addKeyframe(show, 'lead', 'locomotion', { t: 0, vx: 0.3, interp: 'step' });
    show = addKeyframe(show, 'lead', 'head', { t: 0, head_yaw: 0.4, interp: 'step' });
    show = addKeyframe(show, 'lead', 'mouth', { t: 0, open: 0.9, interp: 'step' });
    show = addKeyframe(show, 'wing', 'locomotion', { t: 0, vx: 0.2, interp: 'step' });
    show = setMark(show, 'lead', { x: 1, y: 0.5, heading: 0.25 });
    return show;
  }

  test('derivePose({neutral:true}) ignores every track and stands on the mark', () => {
    const show = movingShow();
    const paths = precomputeShowPaths(show);
    const path = paths.get('lead');
    const sampled = derivePose(show, 'lead', 2, path); // moving, per its tracks
    assert.notEqual(sampled.x, 1, 'sanity: without neutral, the role really has moved off its mark');

    const neutral = derivePose(show, 'lead', 2, path, { neutral: true });
    assert.equal(neutral.x, 1);
    assert.equal(neutral.y, 0.5);
    assert.equal(neutral.heading, 0.25);
    for (const f of ['headYaw', 'headPitch', 'headRoll', 'neckPitch', 'bodyZ', 'bodyRoll', 'bodyPitch', 'mouthOpen', 'walkPhase']) {
      assert.equal(neutral[f], 0, `${f} should be zeroed in the neutral pose, ignoring the moving-show tracks`);
    }
    assert.equal(neutral.resting, true);
    assert.equal(neutral.dimmed, true);
  });

  test('deriveShowPoses forces only the roles named in opts.neutralRoles; the rest sample normally', () => {
    const show = movingShow();
    const paths = precomputeShowPaths(show);
    const poses = deriveShowPoses(show, 2, paths, { neutralRoles: new Set(['lead']) });
    const lead = poses.find((p) => p.role === 'lead');
    const wing = poses.find((p) => p.role === 'wing');
    assert.equal(lead.dimmed, true);
    assert.equal(lead.x, 1, 'lead held on its mark');
    assert.notEqual(wing.dimmed, true, 'wing was not in neutralRoles, so it performs and is not dimmed');
    assert.ok(wing.x > 0, 'wing kept moving per its own locomotion track');
  });

  test('deriveFrame empties the trail and drops event labels for a neutral role, leaving an active role untouched', () => {
    let show = movingShow();
    show = addEvent(show, 'lead', { t: 2, sound: 'chirp' });
    show = addEvent(show, 'wing', { t: 2, sound: 'coo' });
    const paths = precomputeShowPaths(show);
    const neutralRoles = new Set(['lead']);
    const frame = deriveFrame(show, 2, paths, { neutralRoles });

    assert.deepEqual(frame.trails.get('lead'), [], 'a neutral role has no trail to show');
    assert.ok(frame.trails.get('wing').length > 0, 'an active, moving role still gets its trail');

    const labelRoles = frame.labels.map((l) => l.role);
    assert.ok(!labelRoles.includes('lead'), 'a neutral role\'s sound/skill event does not surface as a label — it is not really performing');
    assert.ok(labelRoles.includes('wing'), 'an active role\'s event label is unaffected');
  });

  test('with no neutralRoles supplied at all, behaviour is unchanged (nobody is forced neutral)', () => {
    const show = movingShow();
    const paths = precomputeShowPaths(show);
    const poses = deriveShowPoses(show, 2, paths);
    assert.ok(poses.every((p) => !p.dimmed));
  });
});

// ---------------------------------------------------------------------------
// Never show content: rehearsal state cannot leak into a serialized show
// ---------------------------------------------------------------------------

describe('rehearsal state never touches the show document', () => {
  test('solo/mute operate on a value entirely separate from the show; serializing before and after is byte-identical', () => {
    const show = newShow({ duration: 4, roles: ['lead', 'wing', 'tail'] });
    const before = serializeShow(show);

    let rehearsal = createRehearsalState();
    rehearsal = toggleSolo(rehearsal, 'lead');
    rehearsal = toggleMute(rehearsal, 'wing');
    rehearsal = toggleMute(rehearsal, 'tail');
    void rehearsal; // exercised purely for its own state; never merged into `show`

    const after = serializeShow(show);
    assert.equal(after, before, 'toggling solo/mute must not mutate the show object it was derived alongside');
  });

  test('a round-tripped show never carries a "solo" or "muted" field, even after heavy rehearsal-state use', () => {
    let show = newShow({ duration: 4, roles: ['lead', 'wing'] });
    show = setMark(show, 'lead', { x: 0, y: 0.4, heading: 0 });

    let rehearsal = createRehearsalState();
    for (const role of ['lead', 'wing']) {
      rehearsal = toggleSolo(rehearsal, role);
      rehearsal = toggleMute(rehearsal, role);
    }

    const text = serializeShow(show);
    assert.ok(!/"solo"/.test(text) && !/"muted"/.test(text), 'rehearsal keys must never appear in the serialized document');
    const reparsed = parseShow(text);
    assert.deepEqual(Object.keys(reparsed.editor.marks), ['lead'], 'only the mark explicitly set survives round-trip — rehearsal state added nothing to "editor"');
  });
});
