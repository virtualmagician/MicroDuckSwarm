// Stage viewer — pose derivation, palette, camera easing, event labels
// (docs/viewer.md). No GL, no canvas: StageViewer is exercised with a fake
// renderer object (canvas = null) that just records what it was told to draw.
import { test, describe } from 'node:test';
import assert from 'node:assert/strict';

import { integrate } from '../duckshow-core.js';
import {
  precomputeRolePath, sampleRolePath,
  derivePose, deriveShowPoses,
  resolveMark, defaultMarkFor,
  deriveTrail,
  deriveEventLabels, DEFAULT_EVENT_LABEL_WINDOW,
  roleColorPalette, PALETTE_SATURATION, PALETTE_LIGHTNESS,
  singleRenameAt, roleColorPaletteContinuous,
  easeInOutCubic, blendCameraStates, easeCamera, cameraPresetState,
  CAMERA_PRESET_NAMES, CAMERA_EASE_DURATION,
  StageViewer,
} from '../duckshow-viewer.js';

const almost = (a, b, tol = 1e-7, msg) => assert.ok(Math.abs(a - b) < tol, msg ?? `${a} !~ ${b}`);

/** A minimal valid .duckshow document, roles keyed by their tracks object. */
function show(tracksByRole, { cast, duration = 4.0, editorMarks } = {}) {
  const roles = cast || Object.keys(tracksByRole);
  const doc = {
    format: 'duckshow/1',
    meta: { duration },
    requires: { policies: [] },
    cast: roles.map((role) => ({ role })),
    tracks: tracksByRole,
  };
  if (editorMarks) doc.editor = { marks: editorMarks };
  return doc;
}

const ORIGIN = { x: 0, y: 0, heading: 0 };

// ---------------------------------------------------------------------------
// Pose derivation
// ---------------------------------------------------------------------------

describe('pose derivation', () => {
  test('position/heading match duckshow-core.integrate exactly, at every grid point', () => {
    const doc = show({ lead: { locomotion: [{ t: 0, vx: 0.1, vyaw: 0.3 }] } }, { duration: 2 });
    const mark = { x: 0.2, y: -0.1, heading: 0.4 };
    const path = precomputeRolePath(doc, 'lead', mark, 0.02);
    const expected = integrate(doc, 'lead', 0.02, { start: mark });
    assert.equal(path.t.length, expected.length);
    for (let i = 0; i < expected.length; i++) {
      almost(path.t[i], expected[i].t, 1e-12);
      almost(path.x[i], expected[i].x, 1e-12);
      almost(path.y[i], expected[i].y, 1e-12);
      almost(path.heading[i], expected[i].heading, 1e-12);
    }
  });

  test('interpolated pose between grid points lies between its two neighbours', () => {
    const doc = show({ lead: { locomotion: [{ t: 0, vx: 0.1, vyaw: 0.5 }] } }, { duration: 2 });
    const path = precomputeRolePath(doc, 'lead', ORIGIN, 0.02);
    const s = sampleRolePath(path, 0.531); // between the 0.52 and 0.54 grid samples
    const before = sampleRolePath(path, 0.52);
    const after = sampleRolePath(path, 0.54);
    assert.ok(Math.min(before.x, after.x) - 1e-9 <= s.x && s.x <= Math.max(before.x, after.x) + 1e-9);
    assert.ok(Math.min(before.y, after.y) - 1e-9 <= s.y && s.y <= Math.max(before.y, after.y) + 1e-9);
  });

  test('head/pose/mouth pass straight through the sampler', () => {
    const doc = show({
      lead: {
        head: [{ t: 0, neck_pitch: 0.15, head_pitch: -0.1, head_yaw: 0.2, head_roll: 0.05 }, { t: 2, neck_pitch: 0.15, head_pitch: -0.1, head_roll: 0.05, head_yaw: 0.6 }],
        pose: [{ t: 0, z: -0.02, roll: 0.1, pitch: 0.2 }, { t: 2, z: 0, roll: 0.1, pitch: 0.2 }],
        mouth: [{ t: 0, open: 0 }, { t: 1, open: 1 }],
      },
    }, { duration: 2 });
    const path = precomputeRolePath(doc, 'lead', ORIGIN);
    const pose = derivePose(doc, 'lead', 0.5, path);
    almost(pose.headYaw, 0.2 + (0.6 - 0.2) * 0.25); // linear interp toward the t=2 keyframe
    almost(pose.headPitch, -0.1); // held constant across both keyframes
    almost(pose.headRoll, 0.05);
    almost(pose.neckPitch, 0.15);
    almost(pose.bodyZ, -0.02 + (0 - -0.02) * 0.25);
    almost(pose.bodyRoll, 0.1);
    almost(pose.bodyPitch, 0.2);
    almost(pose.mouthOpen, 0.5); // linear midpoint of the mouth open at t in [0,1]
  });

  test('a role with no head/pose/mouth track defaults every one of those fields to zero', () => {
    const doc = show({ lead: { locomotion: [{ t: 0, vx: 0 }] } }, { duration: 1 });
    const pose = derivePose(doc, 'lead', 0.5, precomputeRolePath(doc, 'lead', ORIGIN));
    for (const f of ['headYaw', 'headPitch', 'headRoll', 'neckPitch', 'bodyZ', 'bodyRoll', 'bodyPitch', 'mouthOpen']) {
      assert.equal(pose[f], 0, `${f} should default to 0`);
    }
  });

  test('a role with no locomotion track stands on its mark for the whole show', () => {
    const mark = { x: 1.2, y: -0.4, heading: 0.7 };
    const doc = show({ lead: { head: [{ t: 0, head_yaw: 0.1 }] } }, { duration: 3 });
    const path = precomputeRolePath(doc, 'lead', mark);
    for (const t of [0, 1, 1.5, 3]) {
      const pose = derivePose(doc, 'lead', t, path);
      almost(pose.x, mark.x); almost(pose.y, mark.y); almost(pose.heading, mark.heading);
    }
  });

  test('deriveShowPoses covers every cast role in cast order', () => {
    const doc = show({ lead: { locomotion: [{ t: 0, vx: 0.1 }] }, wing: {} }, { duration: 1 });
    const paths = new Map([
      ['lead', precomputeRolePath(doc, 'lead', ORIGIN)],
      ['wing', precomputeRolePath(doc, 'wing', { x: 0, y: 0.4, heading: 0 })],
    ]);
    const poses = deriveShowPoses(doc, 0.5, paths);
    assert.deepEqual(poses.map((p) => p.role), ['lead', 'wing']);
    almost(poses[1].y, 0.4);
  });

  describe('walkPhase', () => {
    test('advances while moving and holds once the duck stops', () => {
      const doc = show({
        lead: { locomotion: [{ t: 0, vx: 0.2, interp: 'step' }, { t: 1, vx: 0, interp: 'step' }] },
      }, { duration: 2 });
      const path = precomputeRolePath(doc, 'lead', ORIGIN, 0.02);
      const phaseAt = (t) => sampleRolePath(path, t).walkPhase;
      assert.equal(phaseAt(0), 0);
      assert.ok(phaseAt(0.5) > phaseAt(0.25), 'phase should be strictly increasing while walking');
      assert.ok(phaseAt(1.0) > phaseAt(0.5));
      const held = phaseAt(1.0);
      almost(phaseAt(1.5), held, 1e-9, 'walkPhase must hold, not reset, once the duck settles');
      almost(phaseAt(2.0), held, 1e-9);
    });

    test('a faster locomotion track advances walkPhase faster', () => {
      const slow = precomputeRolePath(show({ lead: { locomotion: [{ t: 0, vx: 0.05 }] } }, { duration: 1 }), 'lead', ORIGIN);
      const fast = precomputeRolePath(show({ lead: { locomotion: [{ t: 0, vx: 0.20 }] } }, { duration: 1 }), 'lead', ORIGIN);
      const slowPhase = sampleRolePath(slow, 1.0).walkPhase;
      const fastPhase = sampleRolePath(fast, 1.0).walkPhase;
      assert.ok(fastPhase > slowPhase * 3, `4x speed should give roughly 4x phase (got ${fastPhase} vs ${slowPhase})`);
    });

    test('turning in place (no translation) does not advance walkPhase', () => {
      const doc = show({ lead: { locomotion: [{ t: 0, vyaw: 1.2 }] } }, { duration: 1 });
      const path = precomputeRolePath(doc, 'lead', ORIGIN);
      almost(sampleRolePath(path, 1.0).walkPhase, 0, 1e-9);
    });
  });

  describe('start marks', () => {
    test('defaultMarkFor spreads roles evenly, centred on the origin', () => {
      const [m0, m1, m2] = [0, 1, 2].map((i) => defaultMarkFor(i, 3));
      for (const m of [m0, m1, m2]) almost(m.x, 0);
      almost(m1.y, 0, 1e-9); // the middle of an odd formation sits on centre
      almost(m0.y + m2.y, 0, 1e-9); // symmetric about the centre
      assert.ok(m0.y < m1.y && m1.y < m2.y);
    });

    test('resolveMark prefers an explicit editor.marks entry over the spread default', () => {
      const doc = show({ lead: {}, wing: {} }, { duration: 1, editorMarks: { wing: { x: 2, y: 3, heading: 1 } } });
      assert.deepEqual(resolveMark(doc, 'wing', 1, 2), { x: 2, y: 3, heading: 1 });
      assert.deepEqual(resolveMark(doc, 'lead', 0, 2), defaultMarkFor(0, 2));
    });
  });
});

// ---------------------------------------------------------------------------
// Trails
// ---------------------------------------------------------------------------

describe('trails', () => {
  test('brightest at the current position, fading toward the start; selection boosts the floor', () => {
    const doc = show({ lead: { locomotion: [{ t: 0, vx: 0.1 }] } }, { duration: 2 });
    const path = precomputeRolePath(doc, 'lead', ORIGIN, 0.02);
    const trail = deriveTrail(path, 1.0, 50, false);
    assert.ok(trail.length > 1);
    almost(trail[trail.length - 1].brightness, 1, 1e-9);
    assert.ok(trail[0].brightness < trail[trail.length - 1].brightness);
    const boosted = deriveTrail(path, 1.0, 50, true);
    assert.ok(boosted[0].brightness > trail[0].brightness);
  });
});

// ---------------------------------------------------------------------------
// Role colour palette
// ---------------------------------------------------------------------------

describe('role colour palette', () => {
  const roles = ['lead', 'wing', 'left', 'right', 'centre'];

  function minCircularGapDeg(hues) {
    const sorted = [...hues].sort((a, b) => a - b);
    let min = Infinity;
    for (let i = 0; i < sorted.length; i++) {
      const a = sorted[i];
      const b = sorted[(i + 1) % sorted.length];
      const gap = i === sorted.length - 1 ? 360 - a + b : b - a;
      min = Math.min(min, gap);
    }
    return min;
  }

  test('N distinguishable hues, evenly spaced, skipping the muddy yellow-green band', () => {
    const palette = roleColorPalette(roles);
    assert.equal(palette.size, roles.length);
    const hues = roles.map((r) => palette.get(r).hue);
    for (const h of hues) assert.ok(h < 55 || h >= 100, `hue ${h} falls inside the excluded yellow-green band`);
    assert.ok(minCircularGapDeg(hues) > 20, `hues should be far enough apart: ${hues.join(', ')}`);
  });

  test('constant saturation/lightness across roles', () => {
    const palette = roleColorPalette(roles);
    for (const entry of palette.values()) {
      assert.equal(entry.saturation, PALETTE_SATURATION);
      assert.equal(entry.lightness, PALETTE_LIGHTNESS);
      assert.match(entry.hex, /^#[0-9a-f]{6}$/);
    }
  });

  test('deterministic and stable under reordering', () => {
    const p1 = roleColorPalette(roles);
    const p2 = roleColorPalette([...roles].reverse());
    const p3 = roleColorPalette(roles);
    for (const r of roles) {
      assert.equal(p2.get(r).hue, p1.get(r).hue, `${r} hue changed when the cast array was reordered`);
      assert.equal(p3.get(r).hue, p1.get(r).hue, 'palette is not a pure function of the role set');
    }
  });

  test('a larger cast still yields a fully distinguishable palette', () => {
    const many = ['lead', 'wing', 'left', 'right', 'centre', 'rear', 'flank', 'point', 'aux', 'spare'];
    const palette = roleColorPalette(many);
    const hues = [...palette.values()].map((v) => v.hue);
    assert.equal(new Set(hues.map((h) => Math.round(h * 100))).size, many.length, 'no two roles collided on a hue');
  });
});

// ---------------------------------------------------------------------------
// Rename-safe palette continuity (docs/viewer.md-adjacent: role-rename
// feature). roleColorPalette above is deliberately a pure function of the
// alphabetically-sorted *name set*, which is what makes it order-stable —
// but it means a rename, which changes that set, can shift another role's
// alphabetical rank (and hue) too. roleColorPaletteContinuous is the fix
// for a *live* editing session; roleColorPalette itself is untouched.
// ---------------------------------------------------------------------------

describe('singleRenameAt', () => {
  test('detects an in-place rename: same length, exactly one differing slot', () => {
    assert.deepEqual(singleRenameAt(['a', 'b', 'c'], ['a', 'zzz', 'c']), { from: 'b', to: 'zzz' });
    assert.deepEqual(singleRenameAt(['lead'], ['front']), { from: 'lead', to: 'front' });
  });

  test('returns null for anything that is not exactly one renamed slot', () => {
    assert.equal(singleRenameAt(['a', 'b'], ['a', 'b']), null); // nothing changed
    assert.equal(singleRenameAt(['a', 'b'], ['a', 'b', 'c']), null); // a role was added
    assert.equal(singleRenameAt(['a', 'b', 'c'], ['a', 'b']), null); // a role was removed
    assert.equal(singleRenameAt(['a', 'b'], ['b', 'a']), null); // reorder, not a rename
    assert.equal(singleRenameAt(['a', 'b', 'c'], ['x', 'y', 'c']), null); // two slots differ
    assert.equal(singleRenameAt(null, ['a']), null);
    assert.equal(singleRenameAt(['a'], null), null);
  });
});

describe('roleColorPaletteContinuous', () => {
  const roles = ['lead', 'wing', 'left'];

  test('a single in-place rename carries every colour forward, including the renamed role\'s own', () => {
    const p1 = roleColorPalette(roles);
    const renamed = ['lead', 'front', 'left']; // 'wing' -> 'front', same slot
    const p2 = roleColorPaletteContinuous(roles, p1, renamed);
    assert.equal(p2.get('lead'), p1.get('lead'), 'unaffected role should be the exact same colour object');
    assert.equal(p2.get('left'), p1.get('left'), 'unaffected role should be the exact same colour object');
    assert.deepEqual({ ...p2.get('front'), role: 'wing' }, p1.get('wing'), "renamed role keeps its own previous colour");
    assert.equal(p2.get('front').role, 'front');
    assert.equal(p2.has('wing'), false);
  });

  test('anything other than a single in-place rename falls back to a fresh roleColorPalette', () => {
    const p1 = roleColorPalette(roles);
    // add
    assert.deepEqual(roleColorPaletteContinuous(roles, p1, [...roles, 'tail']), roleColorPalette([...roles, 'tail']));
    // remove
    assert.deepEqual(roleColorPaletteContinuous(roles, p1, ['lead', 'left']), roleColorPalette(['lead', 'left']));
    // reorder (no rename) — same result either way since roleColorPalette is order-independent, but must not throw or drop entries
    assert.deepEqual(roleColorPaletteContinuous(roles, p1, [...roles].reverse()), roleColorPalette(roles));
    // no previous palette at all (first load)
    assert.deepEqual(roleColorPaletteContinuous(null, null, roles), roleColorPalette(roles));
  });

  test('two renames at once are not treated as a single in-place rename', () => {
    const p1 = roleColorPalette(roles);
    const bothRenamed = ['front', 'flank', 'left'];
    const p2 = roleColorPaletteContinuous(roles, p1, bothRenamed);
    assert.deepEqual(p2, roleColorPalette(bothRenamed));
  });
});

describe('StageViewer carries palette colours across a role rename', () => {
  test('setShow with an in-place-renamed cast keeps every other role\'s colour object identical', () => {
    const palettes = [];
    const viewer = new StageViewer(null, { render: () => {}, setPalette: (p) => palettes.push(p) });
    viewer.setShow(show({ lead: {}, wing: {}, left: {} }));
    const before = palettes.at(-1);

    const renamedDoc = show({ lead: {}, front: {}, left: {} }, { cast: ['lead', 'front', 'left'] });
    viewer.setShow(renamedDoc);
    const after = palettes.at(-1);

    assert.equal(after.get('lead'), before.get('lead'));
    assert.equal(after.get('left'), before.get('left'));
    assert.equal(after.get('front').hex, before.get('wing').hex, "renamed role keeps its own previous colour too");
    assert.equal(after.has('wing'), false);
  });
});

// ---------------------------------------------------------------------------
// Camera preset easing
// ---------------------------------------------------------------------------

describe('camera preset easing', () => {
  test('easeInOutCubic: exact endpoints, monotonic, and not a linear ramp', () => {
    almost(easeInOutCubic(0), 0, 1e-12);
    almost(easeInOutCubic(1), 1, 1e-12);
    let prev = -Infinity;
    for (let i = 0; i <= 40; i++) {
      const v = easeInOutCubic(i / 40);
      assert.ok(v >= prev - 1e-12, 'easeInOutCubic must be monotonic');
      prev = v;
    }
    assert.ok(Math.abs(easeInOutCubic(0.25) - 0.25) > 0.05, 'should ease in, not track t linearly, near the start');
    assert.ok(Math.abs(easeInOutCubic(0.75) - 0.75) > 0.05, 'should ease out, not track t linearly, near the end');
  });

  test('blendCameraStates: t=0 and t=1 reproduce the endpoints exactly', () => {
    const a = cameraPresetState('house');
    const b = cameraPresetState('top');
    const start = blendCameraStates(a, b, 0);
    const end = blendCameraStates(a, b, 1);
    for (const k of ['azimuth', 'elevation', 'radius', 'fovY']) {
      almost(start[k], a[k], 1e-9, `blend(0).${k}`);
      almost(end[k], b[k], 1e-9, `blend(1).${k}`);
    }
  });

  test('easeCamera moves monotonically toward the target preset and is not a linear blend', () => {
    const a = cameraPresetState('house');
    const b = cameraPresetState('top'); // strictly higher elevation than house
    let prevElevation = a.elevation;
    for (let i = 1; i <= 10; i++) {
      const state = easeCamera(a, b, i / 10);
      assert.ok(state.elevation >= prevElevation - 1e-9, 'elevation should move monotonically toward the preset');
      prevElevation = state.elevation;
    }
    const eased = easeCamera(a, b, 0.25).elevation;
    const linear = blendCameraStates(a, b, 0.25).elevation;
    assert.ok(Math.abs(eased - linear) > 1e-6, 'an eased transition must differ from a plain linear blend');
  });

  test('the three named presets are distinct; top looks straight down at the stage', () => {
    assert.deepEqual([...CAMERA_PRESET_NAMES], ['house', 'threeQuarter', 'top']);
    const house = cameraPresetState('house');
    const threeQuarter = cameraPresetState('threeQuarter');
    const top = cameraPresetState('top');
    assert.notEqual(house.elevation, threeQuarter.elevation);
    assert.notEqual(house.azimuth, threeQuarter.azimuth);
    assert.ok(top.elevation > Math.PI / 2 - 0.05, 'top preset should be nearly straight down');
    assert.throws(() => cameraPresetState('side'), RangeError);
  });

  test('StageViewer eases a preset switch via tickCamera rather than cutting to it', () => {
    let clock = 1000;
    const frames = [];
    const viewer = new StageViewer(null, { render: (f) => frames.push(f) }, { now: () => clock });
    viewer.setShow(show({ lead: {} }, { duration: 1 }));
    const before = viewer.getCameraState();
    assert.equal(viewer.getCameraPreset(), 'house');

    viewer.setCameraPreset('top');
    almost(viewer.getCameraState().elevation, before.elevation, 1e-9, 'camera should not jump before any tick');

    const stillGoing = viewer.tickCamera(clock + CAMERA_EASE_DURATION * 1000 * 0.25);
    assert.equal(stillGoing, true);
    const mid = viewer.getCameraState();
    const top = cameraPresetState('top');
    assert.ok(mid.elevation > before.elevation && mid.elevation < top.elevation, 'should be partway through the ease');

    const done = viewer.tickCamera(clock + CAMERA_EASE_DURATION * 1000);
    assert.equal(done, false, 'transition should report finished once its duration has elapsed');
    almost(viewer.getCameraState().elevation, top.elevation, 1e-6);
  });
});

// ---------------------------------------------------------------------------
// Event labels
// ---------------------------------------------------------------------------

describe('event label windowing', () => {
  test('a skill event is a label only inside its window, gone outside it', () => {
    const doc = show({ lead: { events: [{ t: 5.0, do: 'kick_left' }] } }, { duration: 10 });
    const { before, after } = DEFAULT_EVENT_LABEL_WINDOW;
    assert.equal(deriveEventLabels(doc, 5.0 - before - 0.01).length, 0, 'too early');
    assert.deepEqual(deriveEventLabels(doc, 5.0 - before), [{ role: 'lead', text: 'kick_left', t: 5.0, kind: 'skill' }]);
    assert.equal(deriveEventLabels(doc, 5.0 + 0.3).length, 1, 'still inside the window');
    assert.equal(deriveEventLabels(doc, 5.0 + after).length, 1, 'right at the trailing edge');
    assert.equal(deriveEventLabels(doc, 5.0 + after + 0.01).length, 0, 'too late');
  });

  test('sounds label too (dimmer is a renderer concern, but kind must say "sound"); mode events never label', () => {
    const doc = show({ lead: { events: [{ t: 2.0, sound: 'chirp' }, { t: 4.0, mode: 'idle_a' }] } }, { duration: 10 });
    const atSound = deriveEventLabels(doc, 2.0);
    assert.equal(atSound.length, 1);
    assert.equal(atSound[0].kind, 'sound');
    assert.equal(atSound[0].text, 'chirp');
    assert.equal(deriveEventLabels(doc, 4.0).length, 0, 'a mode event has no pose or label representation');
  });

  test('events across roles are windowed independently and returned sorted by fire time', () => {
    const doc = show({
      lead: { events: [{ t: 6.0, do: 'roulade' }] },
      wing: { events: [{ t: 6.05, sound: 'wheee' }] },
    }, { duration: 10 });
    const labels = deriveEventLabels(doc, 6.02, { before: 1, after: 1 });
    assert.equal(labels.length, 2);
    assert.deepEqual(labels.map((l) => l.role), ['lead', 'wing']);
  });

  test('a custom window narrows or widens what is currently visible', () => {
    const doc = show({ lead: { events: [{ t: 3.0, do: 'sit_toggle' }] } }, { duration: 10 });
    assert.equal(deriveEventLabels(doc, 3.5, { before: 0, after: 0.2 }).length, 0);
    assert.equal(deriveEventLabels(doc, 3.5, { before: 0, after: 0.6 }).length, 1);
  });
});

// ---------------------------------------------------------------------------
// StageViewer end to end (fake renderer, no canvas)
// ---------------------------------------------------------------------------

describe('StageViewer', () => {
  test('setShow/setTime/setSelectedRole render poses, trails, labels and marks for the whole cast', () => {
    const frames = [];
    const viewer = new StageViewer(null, { render: (f) => frames.push(f), setPalette: () => {} });
    const doc = show({
      lead: { locomotion: [{ t: 0, vx: 0.1 }], events: [{ t: 0.5, sound: 'chirp' }] },
      wing: {},
    }, { duration: 2 });

    viewer.setShow(doc);
    assert.equal(frames.length, 1, 'setShow renders once');
    viewer.setTime(0.5);
    assert.equal(frames.length, 2, 'setTime renders once');

    const frame = frames.at(-1);
    assert.equal(frame.t, 0.5);
    assert.deepEqual(frame.poses.map((p) => p.role), ['lead', 'wing']);
    const lead = frame.poses.find((p) => p.role === 'lead');
    almost(lead.x, 0.05, 1e-6); // 0.1 m/s for 0.5 s
    const wing = frame.poses.find((p) => p.role === 'wing');
    const wingDefault = defaultMarkFor(1, 2);
    almost(wing.x, wingDefault.x); almost(wing.y, wingDefault.y);
    assert.ok(frame.trails.has('lead') && frame.trails.has('wing'));
    assert.deepEqual(frame.labels.map((l) => l.text), ['chirp']);
    assert.equal(frame.selectedRole, null);
    assert.ok(frame.marks.has('lead') && frame.marks.has('wing'));
    assert.ok(frame.camera && Array.isArray(frame.camera.eye) && Array.isArray(frame.camera.target));

    viewer.setSelectedRole('lead');
    assert.equal(frames.at(-1).selectedRole, 'lead');
  });

  test('render is skipped before a show is loaded, and dispose is safe with no canvas', () => {
    const frames = [];
    const viewer = new StageViewer(null, { render: (f) => frames.push(f) });
    viewer.setTime(1); // no-op: nothing to render yet
    assert.equal(frames.length, 0);
    assert.doesNotThrow(() => viewer.dispose());
  });
});
