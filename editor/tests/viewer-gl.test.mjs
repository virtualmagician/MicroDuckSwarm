// Tests for editor/viewer-gl.js: matrix maths against hand-computed
// values, primitive generators (vertex/index counts, unit-length normals,
// bounds), and camera preset interpolation. No GL context anywhere here —
// importing viewer-gl.js (and transitively viewer-duck.js) must not touch
// a canvas or WebGL, which this file itself proves just by running.

import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  vec3, vec3Add, vec3Sub, vec3Scale, vec3Dot, vec3Cross, vec3Length, vec3Normalize, vec3Lerp,
  mat4Identity, mat4Multiply, mat4Invert, mat4Transpose, mat4Chain,
  mat4FromTranslation, mat4FromScale, mat4FromXRotation, mat4FromYRotation, mat4FromZRotation,
  mat4Translate, mat4RotateX, mat4RotateY, mat4RotateZ, mat4Scale,
  mat4Perspective, mat4LookAt, mat3NormalFromMat4, mat4TransformPoint, mat4TransformVec3,
  smoothstep, clamp, clamp01, lerp,
  uvSphereMesh, eggMesh, cylinderMesh, coneMesh, roundedBoxMesh, discMesh, torusMesh, extrudedQuadMesh,
  VERTEX_STRIDE,
  CAMERA_PRESETS, cameraEase, mixCamera, cameraEyePosition, cameraViewMatrix, cameraProjectionMatrix,
  orbitCamera, dollyCamera, mapPoseToWorld,
} from '../viewer-gl.js';

const EPS = 1e-5;

function approxEqual(a, b, eps = EPS, msg) {
  assert.ok(Math.abs(a - b) <= eps, msg || `expected ${a} ~= ${b} (eps ${eps})`);
}

function approxVec(a, b, eps = EPS, msg) {
  assert.equal(a.length, b.length, msg || 'vector length mismatch');
  for (let i = 0; i < a.length; i++) approxEqual(a[i], b[i], eps, `${msg || 'vector'}[${i}]: ${a[i]} !~ ${b[i]}`);
}

function approxMat4(a, b, eps = EPS, msg) {
  for (let i = 0; i < 16; i++) approxEqual(a[i], b[i], eps, `${msg || 'mat4'}[${i}]: ${a[i]} !~ ${b[i]}`);
}

// ---------------------------------------------------------------------------
// vec3
// ---------------------------------------------------------------------------

test('vec3: add/sub/scale/dot/cross/length/normalize/lerp', () => {
  const a = vec3(1, 2, 3);
  const b = vec3(4, -1, 2);
  approxVec(vec3Add(a, b), [5, 1, 5]);
  approxVec(vec3Sub(a, b), [-3, 3, 1]);
  approxVec(vec3Scale(a, 2), [2, 4, 6]);
  approxEqual(vec3Dot(a, b), 1 * 4 + 2 * -1 + 3 * 2); // = 4 - 2 + 6 = 8
  approxVec(vec3Cross([1, 0, 0], [0, 1, 0]), [0, 0, 1]);
  approxVec(vec3Cross([0, 1, 0], [0, 0, 1]), [1, 0, 0]);
  approxEqual(vec3Length([3, 4, 0]), 5);
  approxVec(vec3Normalize([0, 5, 0]), [0, 1, 0]);
  approxVec(vec3Normalize([0, 0, 0]), [0, 0, 0]); // degenerate: no NaN
  approxVec(vec3Lerp([0, 0, 0], [10, 20, 30], 0.5), [5, 10, 15]);
});

// ---------------------------------------------------------------------------
// mat4 — hand-computed values
// ---------------------------------------------------------------------------

test('mat4Identity is the identity, and is the multiplicative identity', () => {
  approxMat4(mat4Identity(), [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]);
  const m = mat4Chain(mat4FromTranslation([1, 2, 3]), mat4FromXRotation(0.4));
  approxMat4(mat4Multiply(mat4Identity(), m), m);
  approxMat4(mat4Multiply(m, mat4Identity()), m);
});

test('mat4Multiply: hand-computed 2x2-ish case via translate*scale', () => {
  // T(1,2,3) * S(2,3,4) applied to point (1,1,1) should give (1*2+1, 1*3+2, 1*4+3) = (3,5,7)
  const t = mat4FromTranslation([1, 2, 3]);
  const s = mat4FromScale([2, 3, 4]);
  const m = mat4Multiply(t, s);
  const p = mat4TransformPoint(m, [1, 1, 1]);
  approxVec(p, [3, 5, 7]);
  // And the reverse order gives a different, also hand-computable result:
  // S(2,3,4) * T(1,2,3) applied to (1,1,1): translate first -> (2,3,4), then scale -> (4,9,16)
  const m2 = mat4Multiply(s, t);
  approxVec(mat4TransformPoint(m2, [1, 1, 1]), [4, 9, 16]);
});

test('mat4RotateY: quarter turn maps +Z to +X (right-handed convention used throughout)', () => {
  const m = mat4FromYRotation(Math.PI / 2);
  approxVec(mat4TransformPoint(m, [0, 0, 1]), [1, 0, 0], 1e-6);
  approxVec(mat4TransformPoint(m, [1, 0, 0]), [0, 0, -1], 1e-6);
});

test('mat4RotateX / mat4RotateZ: quarter turns against hand-computed axes', () => {
  approxVec(mat4TransformPoint(mat4FromXRotation(Math.PI / 2), [0, 1, 0]), [0, 0, 1], 1e-6);
  approxVec(mat4TransformPoint(mat4FromZRotation(Math.PI / 2), [1, 0, 0]), [0, 1, 0], 1e-6);
});

test('mat4Translate/mat4RotateX/mat4Scale compose in the local frame (post-multiply)', () => {
  // Rotate 90 deg around X, then translate 1 unit further along the
  // now-rotated local Y axis (which points along world +Z): should land at
  // world (0, 0, 1) relative to whatever the base already was.
  const base = mat4FromXRotation(Math.PI / 2);
  const m = mat4Translate(base, [0, 1, 0]);
  approxVec(mat4TransformPoint(m, [0, 0, 0]), [0, 0, 1], 1e-6);
});

test('mat4Transpose: hand-computed swap of a non-symmetric matrix', () => {
  const m = new Float32Array([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16]);
  const t = mat4Transpose(m);
  // column-major: m[col*4+row]; transposing swaps row/col, i.e. t[c*4+r] = m[r*4+c]
  for (let c = 0; c < 4; c++) {
    for (let r = 0; r < 4; r++) {
      approxEqual(t[c * 4 + r], m[r * 4 + c]);
    }
  }
  approxMat4(mat4Transpose(mat4Identity()), mat4Identity());
});

test('mat4Invert: round-trips (A * invert(A) = I) for translate/rotate/scale/combined', () => {
  const cases = [
    mat4FromTranslation([1, -2, 3.5]),
    mat4FromXRotation(0.7),
    mat4FromYRotation(-1.2),
    mat4FromZRotation(2.4),
    mat4FromScale([2, 0.5, 3]),
    mat4Chain(mat4FromTranslation([1, 2, -3]), mat4FromYRotation(0.9), mat4FromScale([1.5, 2, 0.7]), mat4FromXRotation(-0.4)),
  ];
  for (const m of cases) {
    const inv = mat4Invert(m);
    assert.ok(inv, 'expected an invertible matrix');
    approxMat4(mat4Multiply(m, inv), mat4Identity(), 1e-4);
    approxMat4(mat4Multiply(inv, m), mat4Identity(), 1e-4);
  }
});

test('mat4Invert: hand-computed inverse of a pure translation', () => {
  const m = mat4FromTranslation([3, -4, 5]);
  const inv = mat4Invert(m);
  approxMat4(inv, mat4FromTranslation([-3, 4, -5]));
});

test('mat4Invert: returns null for a singular matrix', () => {
  const singular = mat4FromScale([1, 0, 1]); // zero scale on Y collapses the matrix
  assert.equal(mat4Invert(singular), null);
});

test('mat4Perspective: hand-computed entries for a known fov/aspect/near/far', () => {
  const fovY = Math.PI / 2; // 90 degrees -> f = 1/tan(45deg) = 1
  const aspect = 2;
  const near = 1, far = 10;
  const m = mat4Perspective(fovY, aspect, near, far);
  approxEqual(m[0], 0.5); // f/aspect = 1/2
  approxEqual(m[5], 1); // f
  approxEqual(m[10], (far + near) / (near - far)); // -11/9
  approxEqual(m[11], -1);
  approxEqual(m[14], (2 * far * near) / (near - far)); // -20/9
  approxEqual(m[15], 0);
  // A point on the near plane's centre maps to clip z=-1 -> NDC z=-1.
  const pNear = mat4TransformPoint(m, [0, 0, -near]);
  approxEqual(pNear[2], -1, 1e-4);
  const pFar = mat4TransformPoint(m, [0, 0, -far]);
  approxEqual(pFar[2], 1, 1e-4);
});

test('mat4LookAt: hand-computed view matrix for a simple axis-aligned case', () => {
  // Eye at (0,0,5) looking at the origin, up = +Y: camera space should be
  // world space unchanged in X/Y but with Z negated and shifted by -5, i.e.
  // this is just "translate by (0,0,-5)".
  const view = mat4LookAt([0, 0, 5], [0, 0, 0], [0, 1, 0]);
  approxMat4(view, mat4FromTranslation([0, 0, -5]), 1e-6);
  // The eye itself must map to the origin in camera space.
  approxVec(mat4TransformPoint(view, [0, 0, 5]), [0, 0, 0], 1e-5);
});

test('mat4LookAt: falls back to a stable basis when up is parallel to the view direction', () => {
  const view = mat4LookAt([0, 5, 0], [0, 0, 0], [0, 1, 0]); // straight down, up == view dir
  assert.ok(Number.isFinite(view[0]) && !Number.isNaN(view[0]));
  // Still maps the eye to the camera-space origin.
  approxVec(mat4TransformPoint(view, [0, 5, 0]), [0, 0, 0], 1e-4);
});

test('mat3NormalFromMat4: identity and pure non-uniform scale (reciprocal diagonal)', () => {
  const nIdentity = mat3NormalFromMat4(mat4Identity());
  approxVec(Array.from(nIdentity), [1, 0, 0, 0, 1, 0, 0, 0, 1]);

  const scale = mat4FromScale([2, 1, 4]);
  const n = mat3NormalFromMat4(scale);
  // inverse-transpose of diag(2,1,4) is diag(1/2,1,1/4)
  approxVec(Array.from(n), [0.5, 0, 0, 0, 1, 0, 0, 0, 0.25]);
});

test('mat3NormalFromMat4: a transformed normal stays perpendicular to a transformed tangent under non-uniform scale + rotation', () => {
  const m = mat4Chain(mat4FromYRotation(0.6), mat4FromScale([3, 1, 0.4]));
  const normalMat = mat3NormalFromMat4(m);
  const applyMat3 = (mat3, v) => [
    mat3[0] * v[0] + mat3[3] * v[1] + mat3[6] * v[2],
    mat3[1] * v[0] + mat3[4] * v[1] + mat3[7] * v[2],
    mat3[2] * v[0] + mat3[5] * v[1] + mat3[8] * v[2],
  ];
  // Two perpendicular vectors on a surface (a normal and a tangent):
  const localNormal = [1, 0, 0];
  const localTangent = [0, 0, 1];
  assert.ok(Math.abs(vec3Dot(localNormal, localTangent)) < 1e-9);
  const worldNormal = vec3Normalize(applyMat3(normalMat, localNormal));
  const worldTangent = vec3Normalize(mat4TransformVec3(m, localTangent));
  approxEqual(vec3Dot(worldNormal, worldTangent), 0, 1e-4);
});

// ---------------------------------------------------------------------------
// Primitive generators
// ---------------------------------------------------------------------------

function checkUnitNormals(mesh, eps = 1e-4) {
  for (let i = 0; i < mesh.vertexCount; i++) {
    const nx = mesh.vertices[i * VERTEX_STRIDE + 3];
    const ny = mesh.vertices[i * VERTEX_STRIDE + 4];
    const nz = mesh.vertices[i * VERTEX_STRIDE + 5];
    const len = Math.hypot(nx, ny, nz);
    approxEqual(len, 1, eps, `vertex ${i} normal length ${len}`);
  }
}

function positionBounds(mesh) {
  let min = [Infinity, Infinity, Infinity];
  let max = [-Infinity, -Infinity, -Infinity];
  for (let i = 0; i < mesh.vertexCount; i++) {
    for (let axis = 0; axis < 3; axis++) {
      const v = mesh.vertices[i * VERTEX_STRIDE + axis];
      if (v < min[axis]) min[axis] = v;
      if (v > max[axis]) max[axis] = v;
    }
  }
  return { min, max };
}

test('uvSphereMesh: vertex/index counts, unit normals, bounds == radius', () => {
  const latBands = 8, lonBands = 12, radius = 2.5;
  const mesh = uvSphereMesh(radius, latBands, lonBands);
  assert.equal(mesh.vertexCount, (latBands + 1) * (lonBands + 1));
  assert.equal(mesh.indexCount, latBands * lonBands * 6);
  checkUnitNormals(mesh);
  const { min, max } = positionBounds(mesh);
  for (let axis = 0; axis < 3; axis++) {
    approxEqual(max[axis], radius, 1e-3);
    approxEqual(min[axis], -radius, 1e-3);
  }
  // Every vertex should lie exactly on the sphere.
  for (let i = 0; i < mesh.vertexCount; i++) {
    const x = mesh.vertices[i * VERTEX_STRIDE + 0];
    const y = mesh.vertices[i * VERTEX_STRIDE + 1];
    const z = mesh.vertices[i * VERTEX_STRIDE + 2];
    approxEqual(Math.hypot(x, y, z), radius, 1e-3);
  }
});

test('uvSphereMesh: small hand-checkable case (2x2 grid) has plausible indices', () => {
  const mesh = uvSphereMesh(1, 2, 2);
  assert.equal(mesh.vertexCount, 3 * 3);
  assert.equal(mesh.indexCount, 2 * 2 * 6);
  // North pole (lat=0) is the same point (0,radius,0) regardless of lon.
  approxVec([mesh.vertices[0], mesh.vertices[1], mesh.vertices[2]], [0, 1, 0], 1e-6);
});

test('eggMesh: pure ellipsoid (no taper) has independent x/y/z bounds and unit normals', () => {
  const rx = 1, ry = 2, rz = 0.5;
  const latBands = 10, lonBands = 12; // divisible by 4 so the +/-x and +/-z extrema land on exact samples
  const mesh = eggMesh({ rx, ry, rz, topTaper: 0, bottomTaper: 0, latBands, lonBands });
  assert.equal(mesh.vertexCount, (latBands + 1) * (lonBands + 1));
  assert.equal(mesh.indexCount, latBands * lonBands * 6);
  checkUnitNormals(mesh);
  const { min, max } = positionBounds(mesh);
  approxEqual(max[0], rx, 1e-3); approxEqual(min[0], -rx, 1e-3);
  approxEqual(max[1], ry, 1e-3); approxEqual(min[1], -ry, 1e-3);
  approxEqual(max[2], rz, 1e-3); approxEqual(min[2], -rz, 1e-3);
});

test('eggMesh: top taper narrows the top pole ring without moving the bottom', () => {
  const mesh = eggMesh({ rx: 1, ry: 1, rz: 1, topTaper: 0.5, bottomTaper: 0, latBands: 8, lonBands: 12 });
  // First ring (lat=0, the top pole) should have shrunk x/z extent; the
  // last ring (lat=latBands, bottom pole) should still reach the full radius.
  const lonBands = 12;
  const ring = (lat) => {
    let maxR = 0;
    for (let lon = 0; lon <= lonBands; lon++) {
      const idx = lat * (lonBands + 1) + lon;
      const x = mesh.vertices[idx * VERTEX_STRIDE + 0];
      const z = mesh.vertices[idx * VERTEX_STRIDE + 2];
      maxR = Math.max(maxR, Math.hypot(x, z));
    }
    return maxR;
  };
  // lat=1 is just below the very pole (radius 0 there regardless); compare
  // a ring near the top against the equivalent ring near the bottom.
  const nearTop = ring(2);
  const nearBottom = ring(6); // latBands=8, symmetric ring near the bottom
  assert.ok(nearTop < nearBottom, `expected the tapered top ring (${nearTop}) narrower than the untapered bottom ring (${nearBottom})`);
});

test('cylinderMesh: side-only vertex/index counts and unit normals for a straight cylinder', () => {
  const radialSegments = 10;
  const mesh = cylinderMesh({ radiusTop: 1, radiusBottom: 1, height: 2, radialSegments, capTop: false, capBottom: false });
  assert.equal(mesh.vertexCount, 2 * (radialSegments + 1));
  assert.equal(mesh.indexCount, radialSegments * 6);
  checkUnitNormals(mesh);
  const { min, max } = positionBounds(mesh);
  approxEqual(max[1], 1, 1e-3); approxEqual(min[1], -1, 1e-3); // +/- height/2
  approxEqual(max[0], 1, 1e-3); approxEqual(min[0], -1, 1e-3); // +/- radius
});

test('cylinderMesh: capped tapered cylinder counts include both fans, and a cone (radiusTop=0) omits the top cap', () => {
  const radialSegments = 8;
  const capped = cylinderMesh({ radiusTop: 0.5, radiusBottom: 1, height: 1, radialSegments, capTop: true, capBottom: true });
  const ringLen = radialSegments + 1;
  const expectedVerts = 2 * ringLen + 2 * (1 + ringLen); // side rings + two cap fans (centre + rim each)
  assert.equal(capped.vertexCount, expectedVerts);
  assert.equal(capped.indexCount, radialSegments * 6 + 2 * radialSegments * 3);
  checkUnitNormals(capped);

  const cone = cylinderMesh({ radiusTop: 0, radiusBottom: 1, height: 1, radialSegments, capTop: true, capBottom: true });
  // radiusTop=0 -> addCap() skips the (zero-area) top fan entirely.
  const expectedConeVerts = 2 * ringLen + (1 + ringLen); // only the bottom fan
  assert.equal(cone.vertexCount, expectedConeVerts);
  checkUnitNormals(cone);
});

test('coneMesh: apex at +height/2, base radius at -height/2, unit normals', () => {
  const mesh = coneMesh({ radius: 2, height: 4, radialSegments: 16 });
  checkUnitNormals(mesh);
  const { min, max } = positionBounds(mesh);
  approxEqual(max[1], 2, 1e-3);
  approxEqual(min[1], -2, 1e-3);
  approxEqual(max[0], 2, 1e-3);
});

test('roundedBoxMesh: vertex/index count formula, bounds, and unit normals', () => {
  const segments = 4;
  const hx = 1, hy = 2, hz = 0.5;
  const mesh = roundedBoxMesh({ hx, hy, hz, radius: 0.2, segments });
  assert.equal(mesh.vertexCount, 6 * (segments + 1) * (segments + 1));
  assert.equal(mesh.indexCount, 6 * segments * segments * 6);
  checkUnitNormals(mesh);
  const { min, max } = positionBounds(mesh);
  approxEqual(max[0], hx, 1e-3); approxEqual(min[0], -hx, 1e-3);
  approxEqual(max[1], hy, 1e-3); approxEqual(min[1], -hy, 1e-3);
  approxEqual(max[2], hz, 1e-3); approxEqual(min[2], -hz, 1e-3);
});

test('roundedBoxMesh: radius=0 degenerates to a flat-faced box with exact axis-aligned normals', () => {
  const mesh = roundedBoxMesh({ hx: 1, hy: 1, hz: 1, radius: 0, segments: 2 });
  checkUnitNormals(mesh, 1e-6);
  for (let i = 0; i < mesh.vertexCount; i++) {
    const nx = mesh.vertices[i * VERTEX_STRIDE + 3];
    const ny = mesh.vertices[i * VERTEX_STRIDE + 4];
    const nz = mesh.vertices[i * VERTEX_STRIDE + 5];
    // Exactly one component should be +/-1 and the others 0 (a flat face normal).
    const axisComponents = [nx, ny, nz].filter((v) => Math.abs(Math.abs(v) - 1) < 1e-6).length;
    assert.equal(axisComponents, 1, `expected exactly one unit axis component, got (${nx},${ny},${nz})`);
  }
});

test('roundedBoxMesh: a corner vertex is pulled inward by the bevel radius on every axis', () => {
  // With radius > 0, no vertex should sit at the sharp (unrounded) corner
  // (hx,hy,hz) — the corner-most vertices land at inner + radius/sqrt(3)-ish,
  // strictly less than hx on at least one axis at the true corner sample.
  const hx = 1, hy = 1, hz = 1, radius = 0.3;
  const mesh = roundedBoxMesh({ hx, hy, hz, radius, segments: 2 });
  let sawExactCorner = false;
  for (let i = 0; i < mesh.vertexCount; i++) {
    const x = mesh.vertices[i * VERTEX_STRIDE + 0];
    const y = mesh.vertices[i * VERTEX_STRIDE + 1];
    const z = mesh.vertices[i * VERTEX_STRIDE + 2];
    if (Math.abs(x) > hx - 1e-6 && Math.abs(y) > hy - 1e-6 && Math.abs(z) > hz - 1e-6) sawExactCorner = true;
  }
  assert.equal(sawExactCorner, false, 'rounding should pull every corner sample strictly inside the sharp corner');
});

test('discMesh: vertex/index counts, flat +Y normals, independent x/z radii bounds', () => {
  const segments = 20, rx = 3, rz = 1.5;
  const mesh = discMesh({ rx, rz, segments });
  assert.equal(mesh.vertexCount, segments + 2); // centre + (segments+1) rim points
  assert.equal(mesh.indexCount, segments * 3);
  const { min, max } = positionBounds(mesh);
  approxEqual(max[0], rx, 1e-3); approxEqual(min[0], -rx, 1e-3);
  approxEqual(max[2], rz, 1e-3); approxEqual(min[2], -rz, 1e-3);
  approxEqual(max[1], 0); approxEqual(min[1], 0);
  for (let i = 0; i < mesh.vertexCount; i++) {
    approxVec(
      [mesh.vertices[i * VERTEX_STRIDE + 3], mesh.vertices[i * VERTEX_STRIDE + 4], mesh.vertices[i * VERTEX_STRIDE + 5]],
      [0, 1, 0],
    );
  }
});

test('torusMesh: vertex/index counts, unit normals, distance-from-axis and height bounds', () => {
  const majorSegments = 16, minorSegments = 8, majorRadius = 2, minorRadius = 0.4;
  const mesh = torusMesh({ majorRadius, minorRadius, majorSegments, minorSegments });
  assert.equal(mesh.vertexCount, (majorSegments + 1) * (minorSegments + 1));
  assert.equal(mesh.indexCount, majorSegments * minorSegments * 6);
  checkUnitNormals(mesh);
  for (let i = 0; i < mesh.vertexCount; i++) {
    const x = mesh.vertices[i * VERTEX_STRIDE + 0];
    const y = mesh.vertices[i * VERTEX_STRIDE + 1];
    const z = mesh.vertices[i * VERTEX_STRIDE + 2];
    const distFromAxis = Math.hypot(x, z);
    assert.ok(distFromAxis >= majorRadius - minorRadius - 1e-3 && distFromAxis <= majorRadius + minorRadius + 1e-3);
    assert.ok(Math.abs(y) <= minorRadius + 1e-3);
  }
});

test('extrudedQuadMesh: fixed hexahedron counts (6 faces x 4 verts), unit normals, bounds', () => {
  const length = 1, width = 0.6, thicknessTop = 0.2, thicknessBottom = 0.1;
  const mesh = extrudedQuadMesh({ length, width, thicknessTop, thicknessBottom, tipWidth: 0.5, tipThicknessTop: 0.5, tipThicknessBottom: 0.5 });
  assert.equal(mesh.vertexCount, 24);
  assert.equal(mesh.indexCount, 36);
  checkUnitNormals(mesh);
  const { min, max } = positionBounds(mesh);
  approxEqual(min[2], 0, 1e-6); // base at z=0
  approxEqual(max[2], length, 1e-6); // tip at z=length
  approxEqual(max[0], width / 2, 1e-6);
  approxEqual(min[0], -width / 2, 1e-6);
  approxEqual(max[1], thicknessTop, 1e-6);
  approxEqual(min[1], -thicknessBottom, 1e-6);
});

test('extrudedQuadMesh: a beak half (thicknessBottom=0) stays on one side of the hinge plane', () => {
  const mesh = extrudedQuadMesh({ length: 1, width: 0.5, thicknessTop: 0.3, thicknessBottom: 0, tipWidth: 0.4, tipThicknessTop: 0.4, tipThicknessBottom: 0.4 });
  for (let i = 0; i < mesh.vertexCount; i++) {
    const y = mesh.vertices[i * VERTEX_STRIDE + 1];
    assert.ok(y >= -1e-6, `expected every vertex at y >= 0, got ${y}`);
  }
});

// ---------------------------------------------------------------------------
// Camera preset interpolation
// ---------------------------------------------------------------------------

test('CAMERA_PRESETS: house, threeQuarter and top exist with the expected fields', () => {
  for (const name of ['house', 'threeQuarter', 'top']) {
    const cam = CAMERA_PRESETS[name];
    assert.ok(cam, `missing preset ${name}`);
    assert.equal(cam.target.length, 3);
    assert.equal(typeof cam.azimuth, 'number');
    assert.equal(typeof cam.elevation, 'number');
    assert.ok(cam.distance > 0);
    assert.ok(cam.fovY > 0 && cam.fovY < Math.PI);
    assert.equal(cam.up.length, 3);
  }
});

test('cameraEase: 0 -> 0, 1 -> 1, smoothstep symmetry at 0.5', () => {
  approxEqual(cameraEase(0), 0);
  approxEqual(cameraEase(1), 1);
  approxEqual(cameraEase(0.5), 0.5);
  assert.ok(cameraEase(0.25) < 0.25); // eased in, starts slower than linear
  assert.ok(cameraEase(0.75) > 0.75); // eased out, ends slower than linear
});

test('mixCamera: t=0 and t=1 return the endpoints, t=0.5 is the midpoint on every linear field', () => {
  const a = { target: [0, 0, 0], azimuth: 0, elevation: 0.2, distance: 4, fovY: 0.8, up: [0, 1, 0] };
  const b = { target: [2, 4, 6], azimuth: Math.PI / 2, elevation: 0.6, distance: 8, fovY: 1.2, up: [0, 0, 1] };
  const at0 = mixCamera(a, b, 0);
  approxVec(at0.target, a.target);
  approxEqual(at0.azimuth, a.azimuth);
  approxEqual(at0.elevation, a.elevation);
  approxEqual(at0.distance, a.distance);
  approxEqual(at0.fovY, a.fovY);

  const at1 = mixCamera(a, b, 1);
  approxVec(at1.target, b.target);
  approxEqual(at1.azimuth, b.azimuth);
  approxEqual(at1.elevation, b.elevation);
  approxEqual(at1.distance, b.distance);
  approxEqual(at1.fovY, b.fovY);

  const mid = mixCamera(a, b, 0.5);
  approxVec(mid.target, [1, 2, 3]);
  approxEqual(mid.elevation, 0.4);
  approxEqual(mid.distance, 6);
  approxEqual(mid.fovY, 1.0);
});

test('mixCamera: azimuth interpolates the short way around the wrap at 0/2*PI', () => {
  const a = { target: [0, 0, 0], azimuth: (350 * Math.PI) / 180, elevation: 0, distance: 1, fovY: 1, up: [0, 1, 0] };
  const b = { target: [0, 0, 0], azimuth: (10 * Math.PI) / 180, elevation: 0, distance: 1, fovY: 1, up: [0, 1, 0] };
  const mid = mixCamera(a, b, 0.5);
  // The short way from 350deg to 10deg passes through 0/360, landing at 0deg
  // (or equivalently 360deg) -- NOT through 180deg, which the naive lerp
  // ((350+10)/2) would give.
  const deg = ((mid.azimuth * 180) / Math.PI + 360) % 360;
  const distFrom0 = Math.min(deg, 360 - deg);
  assert.ok(distFrom0 < 1e-3, `expected azimuth near 0deg/360deg, got ${deg}deg`);
});

test('mixCamera: up snaps to the destination halfway through, never blends', () => {
  const a = { target: [0, 0, 0], azimuth: 0, elevation: 0, distance: 1, fovY: 1, up: [0, 1, 0] };
  const b = { target: [0, 0, 0], azimuth: 0, elevation: 0, distance: 1, fovY: 1, up: [0, 0, 1] };
  approxVec(mixCamera(a, b, 0.49).up, [0, 1, 0]);
  approxVec(mixCamera(a, b, 0.51).up, [0, 0, 1]);
});

test('cameraEyePosition/cameraViewMatrix/cameraProjectionMatrix: eye sits `distance` away from target, on the sphere implied by azimuth/elevation', () => {
  const cam = CAMERA_PRESETS.house;
  const eye = cameraEyePosition(cam);
  approxEqual(vec3Length_(eye, cam.target), cam.distance, 1e-4);
  // The view matrix must map the eye itself to the camera-space origin.
  const view = cameraViewMatrix(cam);
  approxVec(mat4TransformPoint(view, eye), [0, 0, 0], 1e-4);
  // Projection matrix is a plain perspective (sanity: finite entries, m[11]=-1).
  const proj = cameraProjectionMatrix(cam, 16 / 9);
  approxEqual(proj[11], -1);
});
function vec3Length_(a, b) {
  return Math.hypot(a[0] - b[0], a[1] - b[1], a[2] - b[2]);
}

test('orbitCamera: adds deltas and clamps elevation to the given range', () => {
  const cam = { target: [0, 0, 0], azimuth: 0.2, elevation: 0.5, distance: 5, fovY: 1, up: [0, 1, 0] };
  const orbited = orbitCamera(cam, 0.3, 0.1, [0.1, 1.0]);
  approxEqual(orbited.azimuth, 0.5);
  approxEqual(orbited.elevation, 0.6);
  const clampedHigh = orbitCamera(cam, 0, 10, [0.1, 1.0]);
  approxEqual(clampedHigh.elevation, 1.0);
  const clampedLow = orbitCamera(cam, 0, -10, [0.1, 1.0]);
  approxEqual(clampedLow.elevation, 0.1);
});

test('dollyCamera: scales distance and clamps to the given range', () => {
  const cam = { target: [0, 0, 0], azimuth: 0, elevation: 0, distance: 5, fovY: 1, up: [0, 1, 0] };
  approxEqual(dollyCamera(cam, 2, [1, 20]).distance, 10);
  approxEqual(dollyCamera(cam, 0.01, [1, 20]).distance, 1);
  approxEqual(dollyCamera(cam, 100, [1, 20]).distance, 20);
});

// ---------------------------------------------------------------------------
// mapPoseToWorld — the one place show-space is mapped into render space.
// ---------------------------------------------------------------------------

test('mapPoseToWorld: show x/y map to render Z/X, heading passes through, root sits at floor level', () => {
  const world = mapPoseToWorld({ x: 1.5, y: -0.4, heading: 0.3, bodyZ: 0.02 });
  approxVec(world.position, [-0.4, 0, 1.5]);
  approxEqual(world.headingRotationY, 0.3);
});

test('mapPoseToWorld: missing fields default to zero without throwing', () => {
  const world = mapPoseToWorld({});
  approxVec(world.position, [0, 0, 0]);
  approxEqual(world.headingRotationY, 0);
});

// ---------------------------------------------------------------------------
// Scalar helpers
// ---------------------------------------------------------------------------

test('clamp/clamp01/lerp/smoothstep', () => {
  approxEqual(clamp(5, 0, 3), 3);
  approxEqual(clamp(-5, 0, 3), 0);
  approxEqual(clamp01(1.5), 1);
  approxEqual(lerp(0, 10, 0.3), 3);
  approxEqual(smoothstep(-1), 0);
  approxEqual(smoothstep(2), 1);
  approxEqual(smoothstep(0.5), 0.5);
});
