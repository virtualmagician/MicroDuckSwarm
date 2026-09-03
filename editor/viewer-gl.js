// viewer-gl.js — a small, hand-written WebGL2 layer for the stage viewer
// (docs/viewer.md). No dependencies, no CDN, no build step.
//
// This module owns:
//   - vec3 / mat4 maths (multiply, invert, transpose, perspective, lookAt,
//     translate/rotate/scale, normal matrix), written by hand.
//   - shader compile/link helpers with real error reporting.
//   - a mesh builder producing interleaved position+normal vertex data.
//   - primitive generators: uv-sphere, egg/capsule (independent x/y/z
//     radii), cylinder/tapered cylinder (+ cone), rounded box, flat
//     disc/ellipse, ring/torus, and an extruded quad (for the beak).
//   - camera preset data + pure interpolation/orbit/dolly maths.
//   - the StageRenderer class: GL context, depth-tested forward pass,
//     resize/DPR handling, ground + marks + trails + blob shadows + ducks.
//
// It knows nothing about .duckshow files, the sampler, or show time: it
// takes plain pose objects and colours in, and draws pixels. Coordinate
// convention (all local/model and world space in this file): right-handed,
// Y-up. +Z is "forward" (a duck at heading=0 faces +Z), +X is the duck's
// own left side (so world +X corresponds to a show's +y/"left" axis), +Y
// is up. The floor is the XZ plane at Y=0. See mapPoseToWorld() for the
// one place show-space (x forward, y left, heading CCW) is mapped into
// this render space — everything else in the file is plain render space.
//
// Nothing at module scope touches a GL context, canvas, window or
// document, so every maths/mesh function here is importable and testable
// with no browser and no GL context. StageRenderer imports viewer-duck.js
// for the duck itself (mesh building + drawing); viewer-duck.js imports
// this file's maths/primitives/mesh helpers back — a one-directional data
// dependency in practice (nothing here is touched until draw() actually
// runs), which is what lets `new StageRenderer(canvas)` draw ducks with no
// extra wiring, matching docs/authoring.md's "renderer.draw(poses)" API.

import {
  buildDuckAssets, disposeDuckAssets, drawDuck, updateWalkState,
} from './viewer-duck.js';

// ---------------------------------------------------------------------------
// Small scalar helpers
// ---------------------------------------------------------------------------

export function clamp(x, lo, hi) {
  return x < lo ? lo : (x > hi ? hi : x);
}

export function clamp01(x) {
  return clamp(x, 0, 1);
}

export function lerp(a, b, t) {
  return a + (b - a) * t;
}

/** Hermite smoothstep, clamped to [0,1] first. */
export function smoothstep(x) {
  const t = clamp01(x);
  return t * t * (3 - 2 * t);
}

export const DEG2RAD = Math.PI / 180;
export const RAD2DEG = 180 / Math.PI;

// ---------------------------------------------------------------------------
// vec3 — plain 3-element arrays, functional (return new arrays).
// ---------------------------------------------------------------------------

export function vec3(x = 0, y = 0, z = 0) {
  return [x, y, z];
}

export function vec3Clone(a) {
  return [a[0], a[1], a[2]];
}

export function vec3Add(a, b) {
  return [a[0] + b[0], a[1] + b[1], a[2] + b[2]];
}

export function vec3Sub(a, b) {
  return [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
}

export function vec3Scale(a, s) {
  return [a[0] * s, a[1] * s, a[2] * s];
}

export function vec3Negate(a) {
  return [-a[0], -a[1], -a[2]];
}

export function vec3Dot(a, b) {
  return a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
}

export function vec3Cross(a, b) {
  return [
    a[1] * b[2] - a[2] * b[1],
    a[2] * b[0] - a[0] * b[2],
    a[0] * b[1] - a[1] * b[0],
  ];
}

export function vec3Length(a) {
  return Math.hypot(a[0], a[1], a[2]);
}

export function vec3Normalize(a) {
  const len = vec3Length(a);
  return len > 1e-12 ? [a[0] / len, a[1] / len, a[2] / len] : [0, 0, 0];
}

export function vec3Lerp(a, b, t) {
  return [lerp(a[0], b[0], t), lerp(a[1], b[1], t), lerp(a[2], b[2], t)];
}

export function vec3Distance(a, b) {
  return vec3Length(vec3Sub(a, b));
}

// ---------------------------------------------------------------------------
// mat4 — column-major Float32Array(16), standard OpenGL/WebGL layout:
//   [ 0  4  8 12 ]
//   [ 1  5  9 13 ]
//   [ 2  6 10 14 ]
//   [ 3  7 11 15 ]
// so index = column * 4 + row. mat4Multiply(a, b) means "a composed with
// b", i.e. transforming a point p as mat4Multiply(a, b) applied to p is
// the same as applying b first, then a (out = a * b, as GLSL/OpenGL read
// it left to right against a column vector on the right).
// ---------------------------------------------------------------------------

export function mat4Identity() {
  return new Float32Array([
    1, 0, 0, 0,
    0, 1, 0, 0,
    0, 0, 1, 0,
    0, 0, 0, 1,
  ]);
}

export function mat4Clone(a) {
  return new Float32Array(a);
}

export function mat4Multiply(a, b) {
  const out = new Float32Array(16);
  for (let col = 0; col < 4; col++) {
    const b0 = b[col * 4 + 0];
    const b1 = b[col * 4 + 1];
    const b2 = b[col * 4 + 2];
    const b3 = b[col * 4 + 3];
    out[col * 4 + 0] = a[0] * b0 + a[4] * b1 + a[8] * b2 + a[12] * b3;
    out[col * 4 + 1] = a[1] * b0 + a[5] * b1 + a[9] * b2 + a[13] * b3;
    out[col * 4 + 2] = a[2] * b0 + a[6] * b1 + a[10] * b2 + a[14] * b3;
    out[col * 4 + 3] = a[3] * b0 + a[7] * b1 + a[11] * b2 + a[15] * b3;
  }
  return out;
}

/** Multiply any number of mat4s left to right: mat4Chain(a,b,c) = a*b*c. */
export function mat4Chain(...mats) {
  if (mats.length === 0) return mat4Identity();
  return mats.reduce((acc, m) => mat4Multiply(acc, m));
}

export function mat4Transpose(a) {
  const out = new Float32Array(16);
  for (let c = 0; c < 4; c++) {
    for (let r = 0; r < 4; r++) {
      out[r * 4 + c] = a[c * 4 + r];
    }
  }
  return out;
}

/** Full 4x4 inverse via 2x2 cofactor pairs. Returns null when singular. */
export function mat4Invert(a) {
  const a00 = a[0], a01 = a[1], a02 = a[2], a03 = a[3];
  const a10 = a[4], a11 = a[5], a12 = a[6], a13 = a[7];
  const a20 = a[8], a21 = a[9], a22 = a[10], a23 = a[11];
  const a30 = a[12], a31 = a[13], a32 = a[14], a33 = a[15];

  const b00 = a00 * a11 - a01 * a10;
  const b01 = a00 * a12 - a02 * a10;
  const b02 = a00 * a13 - a03 * a10;
  const b03 = a01 * a12 - a02 * a11;
  const b04 = a01 * a13 - a03 * a11;
  const b05 = a02 * a13 - a03 * a12;
  const b06 = a20 * a31 - a21 * a30;
  const b07 = a20 * a32 - a22 * a30;
  const b08 = a20 * a33 - a23 * a30;
  const b09 = a21 * a32 - a22 * a31;
  const b10 = a21 * a33 - a23 * a31;
  const b11 = a22 * a33 - a23 * a32;

  let det = b00 * b11 - b01 * b10 + b02 * b09 + b03 * b08 - b04 * b07 + b05 * b06;
  if (Math.abs(det) < 1e-15) return null;
  det = 1 / det;

  const out = new Float32Array(16);
  out[0] = (a11 * b11 - a12 * b10 + a13 * b09) * det;
  out[1] = (a02 * b10 - a01 * b11 - a03 * b09) * det;
  out[2] = (a31 * b05 - a32 * b04 + a33 * b03) * det;
  out[3] = (a22 * b04 - a21 * b05 - a23 * b03) * det;
  out[4] = (a12 * b08 - a10 * b11 - a13 * b07) * det;
  out[5] = (a00 * b11 - a02 * b08 + a03 * b07) * det;
  out[6] = (a32 * b02 - a30 * b05 - a33 * b01) * det;
  out[7] = (a20 * b05 - a22 * b02 + a23 * b01) * det;
  out[8] = (a10 * b10 - a11 * b08 + a13 * b06) * det;
  out[9] = (a01 * b08 - a00 * b10 - a03 * b06) * det;
  out[10] = (a30 * b04 - a31 * b02 + a33 * b00) * det;
  out[11] = (a21 * b02 - a20 * b04 - a23 * b00) * det;
  out[12] = (a11 * b07 - a10 * b09 - a12 * b06) * det;
  out[13] = (a00 * b09 - a01 * b07 + a02 * b06) * det;
  out[14] = (a31 * b01 - a30 * b03 - a32 * b00) * det;
  out[15] = (a20 * b03 - a21 * b01 + a22 * b00) * det;
  return out;
}

export function mat4FromTranslation(v) {
  return new Float32Array([
    1, 0, 0, 0,
    0, 1, 0, 0,
    0, 0, 1, 0,
    v[0], v[1], v[2], 1,
  ]);
}

export function mat4FromScale(v) {
  const sx = typeof v === 'number' ? v : v[0];
  const sy = typeof v === 'number' ? v : v[1];
  const sz = typeof v === 'number' ? v : v[2];
  return new Float32Array([
    sx, 0, 0, 0,
    0, sy, 0, 0,
    0, 0, sz, 0,
    0, 0, 0, 1,
  ]);
}

export function mat4FromXRotation(rad) {
  const c = Math.cos(rad), s = Math.sin(rad);
  return new Float32Array([
    1, 0, 0, 0,
    0, c, s, 0,
    0, -s, c, 0,
    0, 0, 0, 1,
  ]);
}

export function mat4FromYRotation(rad) {
  const c = Math.cos(rad), s = Math.sin(rad);
  return new Float32Array([
    c, 0, -s, 0,
    0, 1, 0, 0,
    s, 0, c, 0,
    0, 0, 0, 1,
  ]);
}

export function mat4FromZRotation(rad) {
  const c = Math.cos(rad), s = Math.sin(rad);
  return new Float32Array([
    c, s, 0, 0,
    -s, c, 0, 0,
    0, 0, 1, 0,
    0, 0, 0, 1,
  ]);
}

export function mat4Translate(a, v) {
  return mat4Multiply(a, mat4FromTranslation(v));
}

export function mat4Scale(a, v) {
  return mat4Multiply(a, mat4FromScale(v));
}

export function mat4RotateX(a, rad) {
  return mat4Multiply(a, mat4FromXRotation(rad));
}

export function mat4RotateY(a, rad) {
  return mat4Multiply(a, mat4FromYRotation(rad));
}

export function mat4RotateZ(a, rad) {
  return mat4Multiply(a, mat4FromZRotation(rad));
}

/**
 * Right-handed perspective projection, GL clip space (z in [-1, 1]).
 * fovY in radians. far may be Infinity for an infinite far plane.
 */
export function mat4Perspective(fovY, aspect, near, far) {
  const f = 1 / Math.tan(fovY / 2);
  const out = new Float32Array(16);
  out[0] = f / aspect;
  out[5] = f;
  out[11] = -1;
  if (far === Infinity) {
    out[10] = -1;
    out[14] = -2 * near;
  } else {
    const nf = 1 / (near - far);
    out[10] = (far + near) * nf;
    out[14] = 2 * far * near * nf;
  }
  return out;
}

/** Standard view matrix: eye/center/up (each a vec3) -> world-to-camera. */
export function mat4LookAt(eye, center, up) {
  let z = vec3Normalize(vec3Sub(eye, center)); // camera looks down -z
  let x = vec3Cross(up, z);
  const xlen = vec3Length(x);
  if (xlen < 1e-8) {
    // up is parallel to the view direction: fall back to a stable axis.
    x = Math.abs(z[1]) < 0.999 ? vec3Cross([0, 1, 0], z) : vec3Cross([1, 0, 0], z);
  }
  x = vec3Normalize(x);
  const y = vec3Cross(z, x);

  const out = new Float32Array(16);
  out[0] = x[0]; out[1] = y[0]; out[2] = z[0]; out[3] = 0;
  out[4] = x[1]; out[5] = y[1]; out[6] = z[1]; out[7] = 0;
  out[8] = x[2]; out[9] = y[2]; out[10] = z[2]; out[11] = 0;
  out[12] = -vec3Dot(x, eye);
  out[13] = -vec3Dot(y, eye);
  out[14] = -vec3Dot(z, eye);
  out[15] = 1;
  return out;
}

/**
 * The 3x3 normal matrix for a *rigid* model matrix — rotation + translation
 * only, no scale of any kind. The inverse-transpose of an orthonormal
 * rotation is itself, so this is just m's upper-left 3x3: no cofactors, no
 * determinant, no division, unlike mat3NormalFromMat4 below. Correct only
 * where the caller knows the whole hierarchy is scale-free (e.g. the duck
 * rig in viewer-duck.js, built entirely from mat4FromTranslation/
 * mat4FromXRotation/mat4FromYRotation/mat4FromZRotation — never
 * mat4FromScale/mat4Scale); reach for mat3NormalFromMat4 instead the
 * moment any part in that hierarchy gains non-uniform scale.
 */
export function mat3FromMat4Rigid(m) {
  return new Float32Array([m[0], m[1], m[2], m[4], m[5], m[6], m[8], m[9], m[10]]);
}

/**
 * The 3x3 normal matrix for a model(-view) matrix m: inverse-transpose of
 * its upper-left 3x3, so lighting stays correct under non-uniform scale.
 * Returns a column-major Float32Array(9); falls back to identity when m's
 * upper 3x3 is singular.
 */
export function mat3NormalFromMat4(m) {
  // Upper-left 3x3 of m, read with this file's column-major convention
  // (index = col*4 + row): a_rc means row r, column c.
  const a00 = m[0], a10 = m[1], a20 = m[2];
  const a01 = m[4], a11 = m[5], a21 = m[6];
  const a02 = m[8], a12 = m[9], a22 = m[10];

  // Cofactors C[r][c] of that 3x3.
  const c00 = a11 * a22 - a12 * a21;
  const c01 = -(a10 * a22 - a12 * a20);
  const c02 = a10 * a21 - a11 * a20;
  const c10 = -(a01 * a22 - a02 * a21);
  const c11 = a00 * a22 - a02 * a20;
  const c12 = -(a00 * a21 - a01 * a20);
  const c20 = a01 * a12 - a02 * a11;
  const c21 = -(a00 * a12 - a02 * a10);
  const c22 = a00 * a11 - a01 * a10;

  let det = a00 * c00 + a01 * c01 + a02 * c02; // cofactor expansion along row 0
  if (Math.abs(det) < 1e-15) {
    return new Float32Array([1, 0, 0, 0, 1, 0, 0, 0, 1]);
  }
  det = 1 / det;

  // inverse(A)^T = cofactor(A) / det (adj(A) = cofactor(A)^T and
  // inv(A) = adj(A)/det, so inv(A)^T = cofactor(A)/det directly — no
  // separate transpose step needed). Stored column-major (index = col*3
  // + row) to match this file's mat4 convention.
  return new Float32Array([
    c00 * det, c10 * det, c20 * det,
    c01 * det, c11 * det, c21 * det,
    c02 * det, c12 * det, c22 * det,
  ]);
}

/** Transform a point (w=1, perspective divide applied) by mat4 m. */
export function mat4TransformPoint(m, v) {
  const x = v[0], y = v[1], z = v[2];
  const w = m[3] * x + m[7] * y + m[11] * z + m[15];
  const invW = w !== 0 ? 1 / w : 1;
  return [
    (m[0] * x + m[4] * y + m[8] * z + m[12]) * invW,
    (m[1] * x + m[5] * y + m[9] * z + m[13]) * invW,
    (m[2] * x + m[6] * y + m[10] * z + m[14]) * invW,
  ];
}

/** Transform a direction (w=0, no translation) by mat4 m. */
export function mat4TransformVec3(m, v) {
  const x = v[0], y = v[1], z = v[2];
  return [
    m[0] * x + m[4] * y + m[8] * z,
    m[1] * x + m[5] * y + m[9] * z,
    m[2] * x + m[6] * y + m[10] * z,
  ];
}

// ---------------------------------------------------------------------------
// Mesh builder — interleaved position+normal vertex data.
// ---------------------------------------------------------------------------

/** Floats per vertex, and the offset (in floats) of each attribute. */
export const VERTEX_STRIDE = 6;
export const VERTEX_POSITION_OFFSET = 0;
export const VERTEX_NORMAL_OFFSET = 3;

/**
 * Interleave flat position/normal arrays (xyz per vertex, same vertex
 * count) plus a triangle index list into the mesh shape every primitive
 * generator below returns: { vertices, indices, vertexCount, indexCount }.
 * `indices` is Uint16Array when it fits, otherwise Uint32Array.
 */
function buildMesh(positions, normals, indices) {
  const vertexCount = positions.length / 3;
  const vertices = new Float32Array(vertexCount * VERTEX_STRIDE);
  for (let i = 0; i < vertexCount; i++) {
    vertices[i * 6 + 0] = positions[i * 3 + 0];
    vertices[i * 6 + 1] = positions[i * 3 + 1];
    vertices[i * 6 + 2] = positions[i * 3 + 2];
    vertices[i * 6 + 3] = normals[i * 3 + 0];
    vertices[i * 6 + 4] = normals[i * 3 + 1];
    vertices[i * 6 + 5] = normals[i * 3 + 2];
  }
  const IndexArray = vertexCount > 65535 ? Uint32Array : Uint16Array;
  return {
    vertices,
    indices: new IndexArray(indices),
    vertexCount,
    indexCount: indices.length,
  };
}

/**
 * Push one flat-shaded quad (4 corner points, in a consistent loop around
 * the quad's boundary) into positions/normals/indices as two triangles.
 * The face normal is computed from the quad's own geometry and then
 * oriented to point away from `centroid` (the enclosing shape's rough
 * centre) — robust to which rotational sense the 4 points were listed in,
 * which matters for hand-authored hexahedra like the beak below.
 */
function quadFace(positions, normals, indices, p0, p1, p2, p3, centroid) {
  const e1 = vec3Sub(p1, p0);
  const e2 = vec3Sub(p3, p0);
  let n = vec3Normalize(vec3Cross(e1, e2));
  const faceCenter = vec3Scale(vec3Add(vec3Add(p0, p1), vec3Add(p2, p3)), 0.25);
  if (vec3Dot(n, vec3Sub(faceCenter, centroid)) < 0) n = vec3Negate(n);
  const base = positions.length / 3;
  for (const p of [p0, p1, p2, p3]) {
    positions.push(p[0], p[1], p[2]);
    normals.push(n[0], n[1], n[2]);
  }
  indices.push(base, base + 1, base + 2, base, base + 2, base + 3);
}

// ---------------------------------------------------------------------------
// Primitive generators. All build shapes centred on the local origin, in
// the Y-up convention described at the top of the file (Y-up locally too —
// a cylinder stands along Y, a disc lies flat in the XZ plane).
// ---------------------------------------------------------------------------

/** UV sphere of the given radius. latBands rings of latBands+1, lonBands+1 columns. */
export function uvSphereMesh(radius = 1, latBands = 16, lonBands = 24) {
  const positions = [];
  const normals = [];
  for (let lat = 0; lat <= latBands; lat++) {
    const theta = (lat * Math.PI) / latBands; // 0 (north/+Y) .. PI (south/-Y)
    const sinT = Math.sin(theta), cosT = Math.cos(theta);
    for (let lon = 0; lon <= lonBands; lon++) {
      const phi = (lon * 2 * Math.PI) / lonBands;
      const sinP = Math.sin(phi), cosP = Math.cos(phi);
      const nx = sinT * cosP, ny = cosT, nz = sinT * sinP;
      positions.push(nx * radius, ny * radius, nz * radius);
      normals.push(nx, ny, nz);
    }
  }
  const indices = [];
  for (let lat = 0; lat < latBands; lat++) {
    for (let lon = 0; lon < lonBands; lon++) {
      const a = lat * (lonBands + 1) + lon;
      const b = a + lonBands + 1;
      indices.push(a, b, a + 1, b, b + 1, a + 1);
    }
  }
  return buildMesh(positions, normals, indices);
}

/**
 * "Capsule/egg": an ellipsoid with independent x/y/z radii, optionally
 * tapered toward the top and/or bottom pole (0 = pure ellipsoid) so it can
 * read as an egg rather than a ball — used for the duck's body. Y is the
 * pole axis. Same vertex/index topology as uvSphereMesh.
 */
export function eggMesh({
  rx = 1, ry = 1, rz = 1, topTaper = 0, bottomTaper = 0, latBands = 16, lonBands = 24,
} = {}) {
  const positions = [];
  const normals = [];
  for (let lat = 0; lat <= latBands; lat++) {
    const theta = (lat * Math.PI) / latBands;
    const sinT = Math.sin(theta), cosT = Math.cos(theta); // cosT: +1 top pole, -1 bottom pole
    const taper = cosT >= 0 ? 1 - topTaper * cosT : 1 + bottomTaper * cosT;
    const t = Math.max(0.05, taper);
    const rxEff = rx * t, rzEff = rz * t;
    for (let lon = 0; lon <= lonBands; lon++) {
      const phi = (lon * 2 * Math.PI) / lonBands;
      const cosP = Math.cos(phi), sinP = Math.sin(phi);
      const ux = sinT * cosP, uy = cosT, uz = sinT * sinP;
      const px = ux * rxEff, py = uy * ry, pz = uz * rzEff;
      positions.push(px, py, pz);
      const nx = px / (rxEff * rxEff);
      const ny = py / (ry * ry);
      const nz = pz / (rzEff * rzEff);
      const n = vec3Normalize([nx, ny, nz]);
      normals.push(n[0], n[1], n[2]);
    }
  }
  const indices = [];
  for (let lat = 0; lat < latBands; lat++) {
    for (let lon = 0; lon < lonBands; lon++) {
      const a = lat * (lonBands + 1) + lon;
      const b = a + lonBands + 1;
      indices.push(a, b, a + 1, b, b + 1, a + 1);
    }
  }
  return buildMesh(positions, normals, indices);
}

/**
 * A cylinder standing along Y, from y=-height/2 to y=+height/2, with
 * independent top/bottom radii (radiusTop=0 gives a cone). Optional flat
 * end caps.
 */
export function cylinderMesh({
  radiusTop = 1, radiusBottom = 1, height = 1, radialSegments = 16, capTop = true, capBottom = true,
} = {}) {
  const positions = [];
  const normals = [];
  const halfH = height / 2;
  const slope = (radiusBottom - radiusTop) / height;
  const ringLen = radialSegments + 1;
  for (let ring = 0; ring < 2; ring++) {
    const y = ring === 0 ? -halfH : halfH;
    const r = ring === 0 ? radiusBottom : radiusTop;
    for (let i = 0; i <= radialSegments; i++) {
      const phi = (i * 2 * Math.PI) / radialSegments;
      const c = Math.cos(phi), s = Math.sin(phi);
      positions.push(r * c, y, r * s);
      const n = vec3Normalize([c, slope, s]);
      normals.push(n[0], n[1], n[2]);
    }
  }
  const indices = [];
  for (let i = 0; i < radialSegments; i++) {
    const a = i, b = i + ringLen, c = i + 1, d = i + 1 + ringLen;
    indices.push(a, b, c, b, d, c);
  }
  let count = 2 * ringLen;
  const addCap = (y, radius, faceUp) => {
    if (radius <= 0) return;
    const centerIdx = count;
    positions.push(0, y, 0);
    normals.push(0, faceUp ? 1 : -1, 0);
    count += 1;
    const rimStart = count;
    for (let i = 0; i <= radialSegments; i++) {
      const phi = (i * 2 * Math.PI) / radialSegments;
      positions.push(radius * Math.cos(phi), y, radius * Math.sin(phi));
      normals.push(0, faceUp ? 1 : -1, 0);
    }
    count += ringLen;
    for (let i = 0; i < radialSegments; i++) {
      if (faceUp) indices.push(centerIdx, rimStart + i, rimStart + i + 1);
      else indices.push(centerIdx, rimStart + i + 1, rimStart + i);
    }
  };
  if (capBottom) addCap(-halfH, radiusBottom, false);
  if (capTop) addCap(halfH, radiusTop, true);
  return buildMesh(positions, normals, indices);
}

/** A cone standing along Y (apex at +height/2), via cylinderMesh(radiusTop=0). */
export function coneMesh({ radius = 1, height = 1, radialSegments = 16, capBottom = true } = {}) {
  return cylinderMesh({ radiusTop: 0, radiusBottom: radius, height, radialSegments, capTop: false, capBottom });
}

/**
 * A box with its edges and corners rounded off by `radius`, built by
 * generating a subdivided cube surface and remapping each vertex p to
 * q + normalize(p - q) * radius, where q = p clamped to the box shrunk by
 * radius on every axis. On a flat face away from any edge this reduces to
 * the identity (p stays put, normal is the flat face normal); near an
 * edge/corner it rounds smoothly. `segments` subdivides each of the 6
 * faces into segments x segments quads.
 */
export function roundedBoxMesh({
  hx = 1, hy = 1, hz = 1, radius = 0.2, segments = 3,
} = {}) {
  const r = Math.max(0, Math.min(radius, hx, hy, hz));
  const h = [hx, hy, hz];
  const inner = [hx - r, hy - r, hz - r];
  const faces = [
    { n: 0, sign: 1, u: 1, v: 2 },
    { n: 0, sign: -1, u: 1, v: 2 },
    { n: 1, sign: 1, u: 0, v: 2 },
    { n: 1, sign: -1, u: 0, v: 2 },
    { n: 2, sign: 1, u: 0, v: 1 },
    { n: 2, sign: -1, u: 0, v: 1 },
  ];
  const positions = [];
  const normals = [];
  const indices = [];
  for (const f of faces) {
    const base = positions.length / 3;
    for (let j = 0; j <= segments; j++) {
      const t = (j / segments) * 2 - 1;
      for (let i = 0; i <= segments; i++) {
        const s = (i / segments) * 2 - 1;
        const p = [0, 0, 0];
        p[f.n] = f.sign * h[f.n];
        p[f.u] = s * h[f.u];
        p[f.v] = t * h[f.v];
        if (r > 1e-6) {
          const q = [
            clamp(p[0], -inner[0], inner[0]),
            clamp(p[1], -inner[1], inner[1]),
            clamp(p[2], -inner[2], inner[2]),
          ];
          const d = vec3Sub(p, q);
          const n = vec3Normalize(d);
          const fp = vec3Add(q, vec3Scale(n, r));
          positions.push(fp[0], fp[1], fp[2]);
          normals.push(n[0], n[1], n[2]);
        } else {
          positions.push(p[0], p[1], p[2]);
          const n = [0, 0, 0];
          n[f.n] = f.sign;
          normals.push(n[0], n[1], n[2]);
        }
      }
    }
    const rowLen = segments + 1;
    for (let j = 0; j < segments; j++) {
      for (let i = 0; i < segments; i++) {
        const a = base + j * rowLen + i;
        const b = a + 1;
        const c = a + rowLen;
        const d = c + 1;
        indices.push(a, c, b, b, c, d);
      }
    }
  }
  return buildMesh(positions, normals, indices);
}

/**
 * A flat disc/ellipse in the XZ plane at y=0, normal +Y, centre vertex
 * plus a rim of `segments` points (independent x/z radii).
 */
export function discMesh({ rx = 1, rz = 1, segments = 32 } = {}) {
  const positions = [0, 0, 0];
  const normals = [0, 1, 0];
  for (let i = 0; i <= segments; i++) {
    const phi = (i * 2 * Math.PI) / segments;
    positions.push(rx * Math.cos(phi), 0, rz * Math.sin(phi));
    normals.push(0, 1, 0);
  }
  const indices = [];
  for (let i = 0; i < segments; i++) indices.push(0, i + 1, i + 2);
  return buildMesh(positions, normals, indices);
}

/** A torus (donut) centred on the origin, tube circle in the XZ plane, axis Y. */
export function torusMesh({
  majorRadius = 1, minorRadius = 0.25, majorSegments = 24, minorSegments = 12,
} = {}) {
  const positions = [];
  const normals = [];
  const ringLen = minorSegments + 1;
  for (let j = 0; j <= majorSegments; j++) {
    const u = (j * 2 * Math.PI) / majorSegments;
    const cu = Math.cos(u), su = Math.sin(u);
    for (let i = 0; i <= minorSegments; i++) {
      const v = (i * 2 * Math.PI) / minorSegments;
      const cv = Math.cos(v), sv = Math.sin(v);
      const nx = cv * cu, ny = sv, nz = cv * su;
      positions.push(majorRadius * cu + minorRadius * nx, minorRadius * ny, majorRadius * su + minorRadius * nz);
      normals.push(nx, ny, nz);
    }
  }
  const indices = [];
  for (let j = 0; j < majorSegments; j++) {
    for (let i = 0; i < minorSegments; i++) {
      const a = j * ringLen + i, b = a + ringLen, c = a + 1, d = b + 1;
      indices.push(a, b, c, b, d, c);
    }
  }
  return buildMesh(positions, normals, indices);
}

/**
 * A flat-shaded hexahedron running along +Z from a base rectangle at z=0
 * to a (usually smaller) tip rectangle at z=length — an "extruded quad",
 * used for the duck's beak. The base rectangle spans
 * x in [-width/2, width/2], y in [-thicknessBottom, thicknessTop] (so a
 * beak *half* can be built by leaving one of thicknessTop/thicknessBottom
 * at 0, hinging at y=0); the tip rectangle is the base scaled by tipWidth
 * / tipThicknessTop / tipThicknessBottom.
 */
export function extrudedQuadMesh({
  length = 1, width = 1, thicknessTop = 0.15, thicknessBottom = 0.15,
  tipWidth = 0.4, tipThicknessTop = 0.4, tipThicknessBottom = 0.4,
} = {}) {
  const hw = width / 2;
  const p = {
    bTL: [-hw, thicknessTop, 0], bTR: [hw, thicknessTop, 0],
    bBR: [hw, -thicknessBottom, 0], bBL: [-hw, -thicknessBottom, 0],
    tTL: [-hw * tipWidth, thicknessTop * tipThicknessTop, length],
    tTR: [hw * tipWidth, thicknessTop * tipThicknessTop, length],
    tBR: [hw * tipWidth, -thicknessBottom * tipThicknessBottom, length],
    tBL: [-hw * tipWidth, -thicknessBottom * tipThicknessBottom, length],
  };
  let centroid = [0, 0, 0];
  for (const k of Object.keys(p)) centroid = vec3Add(centroid, p[k]);
  centroid = vec3Scale(centroid, 1 / 8);

  const positions = [];
  const normals = [];
  const indices = [];
  const face = (a, b, c, d) => quadFace(positions, normals, indices, p[a], p[b], p[c], p[d], centroid);
  face('bTL', 'bTR', 'tTR', 'tTL'); // top
  face('bBL', 'tBL', 'tBR', 'bBR'); // bottom
  face('bTL', 'tTL', 'tBL', 'bBL'); // left
  face('bTR', 'bBR', 'tBR', 'tTR'); // right
  face('bTL', 'bBL', 'bBR', 'bTR'); // base cap
  face('tTL', 'tTR', 'tBR', 'tBL'); // tip cap
  return buildMesh(positions, normals, indices);
}

// ---------------------------------------------------------------------------
// Camera — pure data + maths. Spherical orbit around a target point.
// Presets per docs/viewer.md "Camera": house (1, audience/default),
// three-quarter (2), top (3). up is explicit per preset because the top
// preset looks straight down, where world-up (+Y) would be degenerate.
// ---------------------------------------------------------------------------

// azimuth 0 puts the eye on +Z looking back toward -Z, which is the side a
// duck's face points at heading=0 (mapPoseToWorld: "a duck at heading=0
// faces +Z", and the key/fill/rim directions below are lit assuming the
// audience sits on that +Z side) — so the house/top azimuths are 0, not
// 180, and threeQuarter is 227-180=47 (still off to one side, same
// elevation/distance/fovY, just facing the correct hemisphere). Getting
// this backwards is the single worst thing that can happen to this file:
// it points "the view that matters" at every duck's tail.
// Distances sit closer than a literal "row fifteen" would put them: the
// panel this renders into is a few hundred CSS px in the sidebar, and a
// preset a real front-of-house seat away leaves every duck a couple dozen
// pixels tall — too small to judge the thing this view exists to judge
// (silhouette, facing, whether a gesture reads at all). A near-front seat
// keeps the audience framing honest while actually being legible here.
export const CAMERA_PRESETS = Object.freeze({
  house: Object.freeze({
    target: [0, 0.16, 0], azimuth: 0 * DEG2RAD, elevation: 15 * DEG2RAD, distance: 3.3, fovY: 45 * DEG2RAD, up: [0, 1, 0],
  }),
  threeQuarter: Object.freeze({
    target: [0, 0.14, 0], azimuth: 47 * DEG2RAD, elevation: 33 * DEG2RAD, distance: 4.1, fovY: 42 * DEG2RAD, up: [0, 1, 0],
  }),
  top: Object.freeze({
    target: [0, 0, 0], azimuth: 0 * DEG2RAD, elevation: 89.4 * DEG2RAD, distance: 5.6, fovY: 38 * DEG2RAD, up: [0, 0, 1],
  }),
});

export const CAMERA_TRANSITION_MS = 500;

/** Spherical (target, azimuth, elevation, distance) -> world-space eye position. */
export function cameraEyePosition(cam) {
  const ca = Math.cos(cam.azimuth), sa = Math.sin(cam.azimuth);
  const ce = Math.cos(cam.elevation), se = Math.sin(cam.elevation);
  return [
    cam.target[0] + cam.distance * ce * sa,
    cam.target[1] + cam.distance * se,
    cam.target[2] + cam.distance * ce * ca,
  ];
}

export function cameraViewMatrix(cam) {
  return mat4LookAt(cameraEyePosition(cam), cam.target, cam.up || [0, 1, 0]);
}

export function cameraProjectionMatrix(cam, aspect, near = 0.05, far = 60) {
  return mat4Perspective(cam.fovY, aspect, near, far);
}

function shortestAngleLerp(a, b, t) {
  const twoPi = Math.PI * 2;
  let diff = (b - a) % twoPi;
  if (diff > Math.PI) diff -= twoPi;
  else if (diff < -Math.PI) diff += twoPi;
  return a + diff * t;
}

/**
 * Linear (well, shortest-path for azimuth) interpolation between two
 * camera states at t in [0,1]. t is expected to already be eased by the
 * caller (see cameraEase). The `up` vector snaps at the halfway point
 * rather than being blended, since blending two different up vectors has
 * no useful meaning mid-transition.
 */
export function mixCamera(a, b, t) {
  return {
    target: vec3Lerp(a.target, b.target, t),
    azimuth: shortestAngleLerp(a.azimuth, b.azimuth, t),
    elevation: lerp(a.elevation, b.elevation, t),
    distance: lerp(a.distance, b.distance, t),
    fovY: lerp(a.fovY, b.fovY, t),
    up: t < 0.5 ? (a.up || [0, 1, 0]) : (b.up || [0, 1, 0]),
  };
}

/** Ease used for camera preset transitions ("a half-second ease"). */
export function cameraEase(t) {
  return smoothstep(t);
}

/** Orbit-with-drag: rotate around the target, clamped elevation. */
export function orbitCamera(cam, deltaAzimuth, deltaElevation, elevationRange = [3 * DEG2RAD, 89 * DEG2RAD]) {
  return {
    ...cam,
    azimuth: cam.azimuth + deltaAzimuth,
    elevation: clamp(cam.elevation + deltaElevation, elevationRange[0], elevationRange[1]),
  };
}

/** Dolly-with-scroll: move the eye toward/away from the target. */
export function dollyCamera(cam, factor, distanceRange = [1.2, 14]) {
  return { ...cam, distance: clamp(cam.distance * factor, distanceRange[0], distanceRange[1]) };
}

// ---------------------------------------------------------------------------
// GL helpers (shader compile/link, mesh upload). Only reached at call
// time, never at module scope, so importing this file never touches GL.
// ---------------------------------------------------------------------------

function annotateSource(source) {
  return source.split('\n').map((line, i) => `${String(i + 1).padStart(4, ' ')}: ${line}`).join('\n');
}

export function compileShader(gl, type, source) {
  const shader = gl.createShader(type);
  gl.shaderSource(shader, source);
  gl.compileShader(shader);
  if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
    const log = gl.getShaderInfoLog(shader);
    gl.deleteShader(shader);
    const kind = type === gl.VERTEX_SHADER ? 'vertex' : 'fragment';
    throw new Error(`${kind} shader compile error:\n${log}\n--- source ---\n${annotateSource(source)}`);
  }
  return shader;
}

/**
 * Compile + link a program. Returns { program, uniforms, attribs } where
 * uniforms/attribs are name -> location maps built from the linked
 * program's active lists (so callers never guess a location by hand).
 */
export function linkProgram(gl, vsSource, fsSource) {
  const vs = compileShader(gl, gl.VERTEX_SHADER, vsSource);
  const fs = compileShader(gl, gl.FRAGMENT_SHADER, fsSource);
  const program = gl.createProgram();
  gl.attachShader(program, vs);
  gl.attachShader(program, fs);
  gl.linkProgram(program);
  gl.deleteShader(vs);
  gl.deleteShader(fs);
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
    const log = gl.getProgramInfoLog(program);
    gl.deleteProgram(program);
    throw new Error(`program link error:\n${log}`);
  }
  const uniforms = {};
  const uCount = gl.getProgramParameter(program, gl.ACTIVE_UNIFORMS);
  for (let i = 0; i < uCount; i++) {
    const info = gl.getActiveUniform(program, i);
    const name = info.name.replace(/\[0\]$/, '');
    uniforms[name] = gl.getUniformLocation(program, name);
  }
  const attribs = {};
  const aCount = gl.getProgramParameter(program, gl.ACTIVE_ATTRIBUTES);
  for (let i = 0; i < aCount; i++) {
    const info = gl.getActiveAttrib(program, i);
    attribs[info.name] = gl.getAttribLocation(program, info.name);
  }
  return { program, uniforms, attribs };
}

/** Upload a mesh (see buildMesh) to GL buffers. */
export function uploadMesh(gl, mesh) {
  const vbo = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, vbo);
  gl.bufferData(gl.ARRAY_BUFFER, mesh.vertices, gl.STATIC_DRAW);
  const ibo = gl.createBuffer();
  gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, ibo);
  gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, mesh.indices, gl.STATIC_DRAW);
  return {
    vbo,
    ibo,
    indexCount: mesh.indexCount,
    indexType: mesh.indices instanceof Uint32Array ? gl.UNSIGNED_INT : gl.UNSIGNED_SHORT,
  };
}

/** Bind a GL mesh's position (and optionally normal) attributes for drawing. */
export function bindMeshAttribs(gl, glMesh, posLoc, normalLoc = -1) {
  gl.bindBuffer(gl.ARRAY_BUFFER, glMesh.vbo);
  const stride = VERTEX_STRIDE * 4;
  if (posLoc >= 0) {
    gl.enableVertexAttribArray(posLoc);
    gl.vertexAttribPointer(posLoc, 3, gl.FLOAT, false, stride, VERTEX_POSITION_OFFSET * 4);
  }
  if (normalLoc >= 0) {
    gl.enableVertexAttribArray(normalLoc);
    gl.vertexAttribPointer(normalLoc, 3, gl.FLOAT, false, stride, VERTEX_NORMAL_OFFSET * 4);
  }
  gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, glMesh.ibo);
}

export function drawMesh(gl, glMesh) {
  gl.drawElements(gl.TRIANGLES, glMesh.indexCount, glMesh.indexType, 0);
}

function freeGlMesh(gl, glMesh) {
  gl.deleteBuffer(glMesh.vbo);
  gl.deleteBuffer(glMesh.ibo);
}

// ---------------------------------------------------------------------------
// Shaders
// ---------------------------------------------------------------------------

const LIT_VS = `#version 300 es
layout(location = 0) in vec3 aPosition;
layout(location = 1) in vec3 aNormal;
uniform mat4 uModel;
uniform mat4 uViewProj;
uniform mat3 uNormalMatrix;
out vec3 vNormal;
out vec3 vWorldPos;
void main() {
  vec4 world = uModel * vec4(aPosition, 1.0);
  vWorldPos = world.xyz;
  vNormal = normalize(uNormalMatrix * aNormal);
  gl_Position = uViewProj * world;
}
`;

// Neutral studio lighting for a light ground (docs/viewer.md "Light"): a
// soft key from high front-left, a cooler fill from the right, and enough
// ambient that shadowed sides stay readable rather than going dark — a
// light floor makes a black shadow side read as a defect, not mood.
// Lambert + a low-exponent specular; no PBR.
const LIT_FS = `#version 300 es
precision highp float;
in vec3 vNormal;
in vec3 vWorldPos;
uniform vec3 uEyePos;
uniform vec3 uBaseColor;
uniform float uShininess;
uniform float uSpecularStrength;
uniform float uRimBoost;
out vec4 fragColor;

const vec3 KEY_DIR = vec3(0.4266, 0.7584, 0.4929);
const vec3 KEY_COLOR = vec3(1.04, 1.00, 0.92);
const vec3 FILL_DIR = vec3(-0.6449, 0.4104, 0.6449);
const vec3 FILL_COLOR = vec3(0.55, 0.66, 0.86);
const vec3 RIM_DIR = vec3(-0.6172, 0.1543, -0.7715);
const vec3 RIM_COLOR = vec3(0.85, 0.88, 1.0);

void main() {
  vec3 N = normalize(vNormal);
  vec3 V = normalize(uEyePos - vWorldPos);
  float keyD = max(dot(N, KEY_DIR), 0.0);
  float fillD = max(dot(N, FILL_DIR), 0.0);
  // A wider (lower-exponent) fresnel lobe so the rim separates a rounded
  // silhouette like the body/head, not just knife edges.
  float fresnel = pow(1.0 - max(dot(N, V), 0.0), 2.0);
  float rimD = fresnel * max(dot(N, RIM_DIR), 0.0);

  // Ambient raised well above the old dark-stage value, and the fill
  // carries more weight than the key alone would want, so a shadowed
  // panel or hip on the far side of a duck is still legible against a
  // light floor rather than reading as a black cutout.
  vec3 ambient = uBaseColor * 0.20;
  vec3 diffuse = uBaseColor * (KEY_COLOR * keyD * 0.92 + FILL_COLOR * fillD * 0.40);

  vec3 H = normalize(KEY_DIR + V);
  float spec = pow(max(dot(N, H), 0.0), uShininess) * uSpecularStrength * keyD;

  // Rim/edge light dialled back from the dark-stage version: a strong rim
  // exists to pull a shape off a black background, which a light floor no
  // longer needs — kept subtle, and still boosted on selection.
  vec3 color = ambient + diffuse + KEY_COLOR * spec + RIM_COLOR * rimD * (0.35 + uRimBoost);
  fragColor = vec4(color, 1.0);
}
`;

// Studio floor palette (docs/viewer.md "Ground"): a light neutral grey,
// not the old dark stage. GROUND_EDGE_COLOR is the exact colour the floor
// fades to at its far edge *and* the GL clear colour (see gl.clearColor()
// in _render) — one JS-side source of truth so the disc's boundary is
// never a visible seam, just the room fading into itself. Grid/axis
// colours are plain JS arrays interpolated into the GLSL source below so
// there is one place to retune the palette, not two.
const GROUND_FLOOR_COLOR = [0.800, 0.800, 0.793];
const GROUND_EDGE_COLOR = [0.636, 0.636, 0.629];
const GRID_LINE_COLOR = [0.500, 0.500, 0.490];
const GRID_AXIS_WARM_COLOR = [0.740, 0.470, 0.330]; // +X axis
const GRID_AXIS_COOL_COLOR = [0.330, 0.480, 0.720]; // +Z axis
const GRID_MINOR_FADE_NEAR_M = 4.2; // camera-to-point distance where the
const GRID_MINOR_FADE_FAR_M = 9.0;  // 10 cm grid starts, finishes fading out

function glsl3(rgb) {
  return rgb.map((v) => v.toFixed(4)).join(', ');
}

const GROUND_VS = `#version 300 es
layout(location = 0) in vec3 aPosition;
uniform mat4 uViewProj;
out vec2 vXZ;
void main() {
  vXZ = aPosition.xz;
  gl_Position = uViewProj * vec4(aPosition, 1.0);
}
`;

// A real measuring surface, not texture (docs/viewer.md "Ground"): 1 m
// major lines that always persist, 10 cm minor lines that fade out as the
// camera dollies away (so a distant view never solidifies into a grey
// wash of sub-pixel cells), and the two stage axes tinted warm (+X) /
// cool (+Z) so orientation reads from any camera. Every line — grid and
// axis alike — is sized in screen-space via fwidth() rather than a fixed
// world-space threshold, so it stays crisp (not aliased) at any zoom.
const GROUND_FS = `#version 300 es
precision highp float;
in vec2 vXZ;
uniform float uFalloffRadius;
uniform vec3 uEyePos;
out vec4 fragColor;

const vec3 FLOOR_COLOR = vec3(${glsl3(GROUND_FLOOR_COLOR)});
const vec3 EDGE_COLOR  = vec3(${glsl3(GROUND_EDGE_COLOR)});
const vec3 LINE_COLOR  = vec3(${glsl3(GRID_LINE_COLOR)});
const vec3 AXIS_WARM   = vec3(${glsl3(GRID_AXIS_WARM_COLOR)});
const vec3 AXIS_COOL   = vec3(${glsl3(GRID_AXIS_COOL_COLOR)});
const float MINOR_PITCH = 0.1; // 10 cm — duck-scale spacing reference
const float MAJOR_PITCH = 1.0; // 1 m — the reference you count in
const float MINOR_FADE_NEAR = ${GRID_MINOR_FADE_NEAR_M.toFixed(2)};
const float MINOR_FADE_FAR  = ${GRID_MINOR_FADE_FAR_M.toFixed(2)};

// Anti-aliased coverage [0,1] of the grid at the given pitch, using the
// on-screen derivative of the grid coordinate as the line's half-width —
// this is what keeps both grid scales crisp instead of moire-ing as the
// camera moves, unlike thresholding the raw world-space distance to a line.
float gridLine(vec2 worldXZ, float pitch) {
  vec2 coord = worldXZ / pitch;
  vec2 d = abs(fract(coord - 0.5) - 0.5) / max(fwidth(coord), vec2(1e-4));
  return 1.0 - clamp(min(d.x, d.y), 0.0, 1.0);
}

void main() {
  vec3 color = FLOOR_COLOR;

  float minorLine = gridLine(vXZ, MINOR_PITCH);
  float majorLine = gridLine(vXZ, MAJOR_PITCH);

  float camDist = length(vec3(vXZ.x, 0.0, vXZ.y) - uEyePos);
  float minorFade = 1.0 - smoothstep(MINOR_FADE_NEAR, MINOR_FADE_FAR, camDist);
  minorLine *= minorFade;

  // Major clearly visible, minor lighter (docs/viewer.md): same ink, two
  // strengths, so a major line under a minor one always reads as major.
  color = mix(color, LINE_COLOR, minorLine * 0.34);
  color = mix(color, LINE_COLOR, majorLine * 0.85);

  // Stage axes: fixed ~3px screen-space width, drawn on top of the grid
  // and never distance-faded — orientation must stay legible from any
  // camera, including a dollied-out one where the minor grid has gone.
  float distToXAxis = abs(vXZ.y) / max(fwidth(vXZ.y), 1e-4); // Z=0 line, i.e. the +X axis
  float distToZAxis = abs(vXZ.x) / max(fwidth(vXZ.x), 1e-4); // X=0 line, i.e. the +Z axis
  float xAxisMask = 1.0 - clamp(distToXAxis / 1.6, 0.0, 1.0);
  float zAxisMask = 1.0 - clamp(distToZAxis / 1.6, 0.0, 1.0);
  color = mix(color, AXIS_WARM, xAxisMask * 0.60);
  color = mix(color, AXIS_COOL, zAxisMask * 0.60);

  // Soft radial fade to exactly the clear colour, completed well inside
  // the disc's own edge, so the mesh boundary is never a visible seam —
  // the room just fades into itself (docs/viewer.md: "fading gently at
  // the far edge so it does not end in a hard line").
  float r = length(vXZ) / uFalloffRadius;
  float falloff = 1.0 - smoothstep(0.45, 0.92, r);
  fragColor = vec4(mix(EDGE_COLOR, color, falloff), 1.0);
}
`;

// Shared "floor disc" vertex shader for blob shadows and start-mark rings:
// unlit, positioned by a model matrix, local XZ passed through for a
// radial falloff in the fragment stage.
const FLOOR_DISC_VS = `#version 300 es
layout(location = 0) in vec3 aPosition;
uniform mat4 uModel;
uniform mat4 uViewProj;
out vec2 vLocalXZ;
void main() {
  vLocalXZ = aPosition.xz;
  gl_Position = uViewProj * uModel * vec4(aPosition, 1.0);
}
`;

// Neutral grey, never black (docs/viewer.md "Contact shadows matter MORE
// on a light floor... soft, neutral grey, never black"): on a light floor
// a black contact shadow reads as a hole punched in it, not grounding.
const SHADOW_COLOR = [0.320, 0.310, 0.300];

const SHADOW_FS = `#version 300 es
precision highp float;
in vec2 vLocalXZ;
uniform float uStrength;
out vec4 fragColor;
void main() {
  float r = length(vLocalXZ);
  float a = 1.0 - smoothstep(0.0, 1.0, r);
  // Dark right at the contact point, gone quickly outward, so it reads as
  // a real contact shadow hugging the feet rather than a wide soft wash.
  a = pow(a, 1.8) * uStrength;
  fragColor = vec4(${glsl3(SHADOW_COLOR)}, a);
}
`;

// A hairline annulus near the outer edge of the unit disc, not a fat donut
// — docs/viewer.md "restrained" start marks. uColor already arrives dimmed
// (see _drawMarks): this only shapes it.
const MARK_FS = `#version 300 es
precision highp float;
in vec2 vLocalXZ;
uniform vec3 uColor;
uniform float uOpacity;
out vec4 fragColor;
void main() {
  float r = length(vLocalXZ);
  float ring = smoothstep(0.90, 0.955, r) - smoothstep(0.955, 0.99, r);
  fragColor = vec4(uColor, ring * uOpacity);
}
`;

const LINE_VS = `#version 300 es
layout(location = 0) in vec3 aPosition;
layout(location = 1) in float aBrightness;
uniform mat4 uViewProj;
uniform vec3 uColor;
out vec4 vColor;
void main() {
  // Raised floor from the dark-stage 0.12 — a thin role-coloured line at
  // low alpha all but disappears against a light grey floor, unlike a
  // near-black one where it stayed visible by contrast alone.
  vColor = vec4(uColor, 0.22 + 0.72 * aBrightness);
  gl_Position = uViewProj * vec4(aPosition, 1.0);
}
`;

const LINE_FS = `#version 300 es
precision highp float;
in vec4 vColor;
out vec4 fragColor;
void main() {
  fragColor = vColor;
}
`;

// ---------------------------------------------------------------------------
// StageRenderer — pose in, pixels out. Owns the GL context and every mesh/
// shader above; renders on demand (draw()), never runs a permanent rAF
// loop (only a short, self-stopping one during a camera preset ease).
// ---------------------------------------------------------------------------

const GROUND_RADIUS_M = 9;

function now() {
  return (typeof performance !== 'undefined' && performance.now) ? performance.now() : Date.now();
}

/**
 * Map a duckshow pose's ground-plane fields into this file's render space.
 * Show space: x forward, y left, heading CCW (docs/viewer.md, duckshow-core
 * integrate()). Render space: +Z forward, +X left, +Y up (top of file).
 * The root sits at floor level (y=0) — hip height and pose.bodyZ (crouch)
 * are applied inside viewer-duck.js's own hierarchy, below the hips, so
 * the feet (not the hips) are what's placed on pose.x/pose.y.
 */
export function mapPoseToWorld(pose) {
  return {
    position: [pose.y || 0, 0, pose.x || 0],
    headingRotationY: pose.heading || 0,
  };
}

export class StageRenderer {
  constructor(canvas, opts = {}) {
    this.canvas = canvas;
    const gl = canvas.getContext('webgl2', {
      antialias: true, alpha: false, depth: true, ...opts.contextAttributes,
    });
    if (!gl) throw new Error('WebGL2 is not available on this canvas');
    this.gl = gl;

    this._cast = [];
    this._castByRole = new Map();
    this._marks = [];
    this._trails = {};
    this._trailBuffers = new Map();
    this._selected = null;
    this._camera = { ...CAMERA_PRESETS.house };
    this._cameraFrom = null;
    this._cameraTo = null;
    this._cameraStart = 0;
    this._cameraRAF = null;
    this._lastPoses = [];
    this._walkStates = new Map();
    this._lastFrameTime = now();
    this._aspect = 1;
    this._disposed = false;
    this._contextLost = false;
    this._onContextLost = typeof opts.onContextLost === 'function' ? opts.onContextLost : null;
    this._onContextRestored = typeof opts.onContextRestored === 'function' ? opts.onContextRestored : null;

    // A real context loss (GPU driver reset, integrated/discrete GPU
    // switch, sleep/wake, or eviction once too many WebGL contexts are
    // open) leaves every further gl.* call a silent no-op — without this,
    // a long rehearsal session's stage canvas just freezes with no signal
    // anything is wrong, and the browser never attempts to restore it
    // because the default action (give up) is never prevented.
    this._handleContextLost = (ev) => {
      ev.preventDefault();
      this._contextLost = true;
      if (this._cameraRAF) { cancelAnimationFrame(this._cameraRAF); this._cameraRAF = null; }
      // Every GL object from the dead context (trail VBOs included) is a
      // useless zombie handle once restored — drop the JS-side references
      // so the next live setTrails() recreates them instead of trying to
      // bufferSubData into a buffer that no longer exists.
      this._trailBuffers.clear();
      if (this._onContextLost) this._onContextLost();
    };
    this._handleContextRestored = () => {
      this._contextLost = false;
      this._initGL(opts);
      this._resize();
      this._render(this._lastPoses);
      if (this._onContextRestored) this._onContextRestored();
    };
    canvas.addEventListener('webglcontextlost', this._handleContextLost, false);
    canvas.addEventListener('webglcontextrestored', this._handleContextRestored, false);

    this._initGL(opts);
    this._resize();
  }

  _initGL(opts) {
    const gl = this.gl;
    this._lit = linkProgram(gl, LIT_VS, LIT_FS);
    this._ground = linkProgram(gl, GROUND_VS, GROUND_FS);
    this._floorDiscShadow = linkProgram(gl, FLOOR_DISC_VS, SHADOW_FS);
    this._floorDiscMark = linkProgram(gl, FLOOR_DISC_VS, MARK_FS);
    this._line = linkProgram(gl, LINE_VS, LINE_FS);

    this._groundMesh = uploadMesh(gl, discMesh({ rx: GROUND_RADIUS_M, rz: GROUND_RADIUS_M, segments: 64 }));
    this._unitDisc = uploadMesh(gl, discMesh({ rx: 1, rz: 1, segments: 28 }));

    this._duckAssets = buildDuckAssets(gl);
  }

  setCast(cast) {
    this._cast = (cast || []).map((c) => ({ ...c }));
    this._castByRole = new Map(this._cast.map((c) => [c.role, c]));
    return this;
  }

  setMarks(marks) {
    this._marks = (marks || []).map((m) => ({ ...m }));
    return this;
  }

  /**
   * Trails are re-sampled at DEFAULT_DT (50 Hz) but drawn at up to 60 fps
   * (every playing/scrubbing paintViewer() tick), so most calls here carry
   * data that is unchanged, or changed only by its newest point or two.
   * Buffers are therefore kept per role across calls and updated in place
   * (bufferSubData, growing only when a trail needs more room than it's
   * ever needed before) instead of the old delete+create+bufferData every
   * single call — that was real driver-side buffer-object churn on the
   * "60 fps with ten ducks" / render-on-change path (docs/viewer.md #4).
   */
  setTrails(trails) {
    const gl = this.gl;
    this._trails = trails || {};
    const roleKeys = new Set(Object.keys(this._trails));
    // Drop buffers only for roles no longer present at all (removed from
    // the cast) — a role with a momentarily-empty trail keeps its buffer,
    // just drawn with count 0, since it typically has points again next frame.
    for (const [role, buf] of this._trailBuffers) {
      if (!roleKeys.has(role)) { gl.deleteBuffer(buf.vbo); this._trailBuffers.delete(role); }
    }
    for (const [role, points] of Object.entries(this._trails)) {
      const n = points ? points.length : 0;
      let buf = this._trailBuffers.get(role);
      if (this._contextLost) {
        // Don't allocate a GL buffer against a dead context — just track
        // the count so _drawTrails's size check stays coherent; the next
        // live setTrails call after webglcontextrestored uploads in full.
        if (buf) buf.count = n;
        continue;
      }
      if (!buf) { buf = { vbo: gl.createBuffer(), capacity: 0, count: 0 }; this._trailBuffers.set(role, buf); }
      buf.count = n;
      if (n === 0) continue;
      const data = new Float32Array(n * 4);
      for (let i = 0; i < n; i++) {
        data[i * 4 + 0] = points[i].y || 0;
        data[i * 4 + 1] = 0.003;
        data[i * 4 + 2] = points[i].x || 0;
        // duckshow-viewer.js's deriveTrail already computes this per point —
        // brightest near the current position, fading toward the start, with
        // a raised floor on the selected role's oldest points so they still
        // read clearly (docs/viewer.md "Trails") — recomputing a plain
        // positional ramp here would silently drop that selected-role boost.
        data[i * 4 + 3] = typeof points[i].brightness === 'number' ? points[i].brightness : (n > 1 ? i / (n - 1) : 1);
      }
      gl.bindBuffer(gl.ARRAY_BUFFER, buf.vbo);
      if (n > buf.capacity) {
        gl.bufferData(gl.ARRAY_BUFFER, data, gl.DYNAMIC_DRAW);
        buf.capacity = n;
      } else {
        gl.bufferSubData(gl.ARRAY_BUFFER, 0, data);
      }
    }
    return this;
  }

  setSelected(role) {
    this._selected = role || null;
    return this;
  }

  /**
   * presetOrOrbit: one of 'house' | 'threeQuarter' | 'top' (starts a
   * half-second ease from the current camera), or a partial camera-state
   * object ({azimuth, elevation, distance, target, fovY}) applied
   * immediately with no easing (for live orbit/dolly interaction).
   */
  setCamera(presetOrOrbit) {
    if (typeof presetOrOrbit === 'string') {
      const preset = CAMERA_PRESETS[presetOrOrbit];
      if (!preset) throw new Error(`unknown camera preset: ${presetOrOrbit}`);
      if (this._cameraRAF) { cancelAnimationFrame(this._cameraRAF); this._cameraRAF = null; }
      this._cameraFrom = { ...this._camera };
      this._cameraTo = { ...preset };
      this._cameraStart = now();
      this._runCameraTransition();
    } else {
      if (this._cameraRAF) { cancelAnimationFrame(this._cameraRAF); this._cameraRAF = null; }
      this._cameraFrom = null;
      this._cameraTo = null;
      this._camera = { ...this._camera, ...presetOrOrbit };
      if (!this._contextLost) this._render(this._lastPoses);
    }
    return this;
  }

  _runCameraTransition() {
    const step = () => {
      if (this._disposed) return;
      if (this._contextLost) { this._camera = this._cameraTo; this._cameraRAF = null; return; } // resumes cleanly at rest once restored
      const t = clamp01((now() - this._cameraStart) / CAMERA_TRANSITION_MS);
      this._camera = mixCamera(this._cameraFrom, this._cameraTo, cameraEase(t));
      this._render(this._lastPoses);
      if (t < 1) {
        this._cameraRAF = (typeof requestAnimationFrame === 'function')
          ? requestAnimationFrame(step)
          : null;
        if (!this._cameraRAF) this._camera = this._cameraTo; // no rAF (e.g. headless): snap
      } else {
        this._cameraRAF = null;
        this._camera = this._cameraTo;
      }
    };
    step();
  }

  /** Recompute the GL viewport from the canvas's CSS size and devicePixelRatio. */
  resize() {
    this._resize();
    return this;
  }

  _resize() {
    const canvas = this.canvas;
    const dpr = (typeof window !== 'undefined' && window.devicePixelRatio) || 1;
    const cssW = canvas.clientWidth;
    const cssH = canvas.clientHeight;
    if (cssW > 0 && cssH > 0) {
      const w = Math.max(1, Math.round(cssW * dpr));
      const h = Math.max(1, Math.round(cssH * dpr));
      if (canvas.width !== w || canvas.height !== h) {
        canvas.width = w;
        canvas.height = h;
      }
    } else if (!(canvas.width > 0) || !(canvas.height > 0)) {
      // No CSS size yet (hidden panel, or a bare offscreen canvas with
      // neither clientWidth/Height nor a backing size) — fall back to
      // something so gl.viewport/pick still have a size to work with,
      // without reusing an already-DPR-scaled backing size as if it were
      // a fresh CSS size (that would compound on every call).
      canvas.width = canvas.width || 300;
      canvas.height = canvas.height || 150;
    }
    this.gl.viewport(0, 0, canvas.width, canvas.height);
    this._aspect = canvas.width / Math.max(1, canvas.height);
  }

  /** Render the given poses (array of {role, x, y, heading, ...}) right now. */
  draw(poses) {
    this._lastPoses = poses || [];
    if (this._contextLost) return; // resumes on its own once webglcontextrestored fires
    this._resize();
    const t = now();
    const dt = Math.max(0, Math.min(0.25, (t - this._lastFrameTime) / 1000));
    this._lastFrameTime = t;
    this._render(this._lastPoses, dt);
  }

  /** True while the GL context is lost and awaiting webglcontextrestored. */
  isContextLost() { return this._contextLost; }

  /** Unproject a canvas-space (CSS px) point onto the floor plane (y=0); returns show-space {x,y} or null. */
  pick(x, y) {
    const canvas = this.canvas;
    const w = canvas.clientWidth || canvas.width || 1;
    const h = canvas.clientHeight || canvas.height || 1;
    const ndcX = (x / w) * 2 - 1;
    const ndcY = 1 - (y / h) * 2;
    const view = cameraViewMatrix(this._camera);
    const proj = cameraProjectionMatrix(this._camera, this._aspect);
    const viewProj = mat4Multiply(proj, view);
    const inv = mat4Invert(viewProj);
    if (!inv) return null;
    const near = mat4TransformPoint(inv, [ndcX, ndcY, -1]);
    const far = mat4TransformPoint(inv, [ndcX, ndcY, 1]);
    const dir = vec3Normalize(vec3Sub(far, near));
    if (Math.abs(dir[1]) < 1e-6) return null;
    const tHit = -near[1] / dir[1];
    if (tHit < 0) return null;
    const hit = vec3Add(near, vec3Scale(dir, tHit));
    return { x: hit[2], y: hit[0] };
  }

  _render(poses, dt = 0) {
    const gl = this.gl;
    const cam = this._camera;
    const view = cameraViewMatrix(cam);
    const proj = cameraProjectionMatrix(cam, this._aspect);
    const viewProj = mat4Multiply(proj, view);
    const eye = cameraEyePosition(cam);

    gl.enable(gl.DEPTH_TEST);
    gl.depthFunc(gl.LEQUAL);
    gl.disable(gl.CULL_FACE);
    // Exactly GROUND_EDGE_COLOR (see the constant above GROUND_FS): the
    // clear colour is what shows *beyond* the ground mesh, and the ground
    // shader's own radial falloff fades the floor to this same colour
    // before it reaches the disc's edge — so the boundary is never a
    // visible seam, light-grey room fading into light-grey background.
    gl.clearColor(GROUND_EDGE_COLOR[0], GROUND_EDGE_COLOR[1], GROUND_EDGE_COLOR[2], 1);
    gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);

    this._drawGround(viewProj, eye);
    this._drawMarks(viewProj);
    this._drawTrails(viewProj);
    this._drawShadows(viewProj, poses);
    this._drawDucks(viewProj, eye, poses, dt);
  }

  _drawGround(viewProj, eye) {
    const gl = this.gl;
    const { program, uniforms } = this._ground;
    gl.useProgram(program);
    gl.uniformMatrix4fv(uniforms.uViewProj, false, viewProj);
    gl.uniform1f(uniforms.uFalloffRadius, GROUND_RADIUS_M);
    gl.uniform3f(uniforms.uEyePos, eye[0], eye[1], eye[2]);
    bindMeshAttribs(gl, this._groundMesh, 0, -1);
    drawMesh(gl, this._groundMesh);
  }

  _drawMarks(viewProj) {
    if (this._marks.length === 0) return;
    const gl = this.gl;
    const { program, uniforms } = this._floorDiscMark;
    gl.useProgram(program);
    gl.uniformMatrix4fv(uniforms.uViewProj, false, viewProj);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
    gl.depthMask(false);
    bindMeshAttribs(gl, this._unitDisc, 0, -1);
    for (const mark of this._marks) {
      const cast = this._castByRole.get(mark.role);
      const color = colorToRgb(mark.color || (cast && cast.color) || '#cccccc');
      // Dimmed below the raw role hue — a whisper that a mark is there,
      // not a glowing donut — but less aggressively than on the old dark
      // floor, since role hues are now deep/saturated rather than pastel
      // and a light grey floor already reduces their apparent brightness.
      const dim = 0.78;
      const model = mat4Chain(
        mat4FromTranslation([mark.y || 0, 0.002, mark.x || 0]),
        mat4FromScale(0.22),
      );
      gl.uniformMatrix4fv(uniforms.uModel, false, model);
      gl.uniform3f(uniforms.uColor, color[0] * dim, color[1] * dim, color[2] * dim);
      gl.uniform1f(uniforms.uOpacity, mark.role === this._selected ? 0.55 : 0.35);
      drawMesh(gl, this._unitDisc);
    }
    gl.depthMask(true);
    gl.disable(gl.BLEND);
  }

  _drawTrails(viewProj) {
    if (this._trailBuffers.size === 0) return;
    const gl = this.gl;
    const { program, uniforms, attribs } = this._line;
    gl.useProgram(program);
    gl.uniformMatrix4fv(uniforms.uViewProj, false, viewProj);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
    gl.depthMask(false);
    const posLoc = attribs.aPosition;
    const brightLoc = attribs.aBrightness;
    for (const [role, buf] of this._trailBuffers.entries()) {
      const cast = this._castByRole.get(role);
      const color = colorToRgb((cast && cast.color) || '#cccccc');
      const boost = role === this._selected ? 1.3 : 1.0;
      gl.uniform3f(uniforms.uColor, Math.min(1, color[0] * boost), Math.min(1, color[1] * boost), Math.min(1, color[2] * boost));
      gl.bindBuffer(gl.ARRAY_BUFFER, buf.vbo);
      gl.enableVertexAttribArray(posLoc);
      gl.vertexAttribPointer(posLoc, 3, gl.FLOAT, false, 16, 0);
      if (brightLoc >= 0) {
        gl.enableVertexAttribArray(brightLoc);
        gl.vertexAttribPointer(brightLoc, 1, gl.FLOAT, false, 16, 12);
      }
      gl.drawArrays(gl.LINE_STRIP, 0, buf.count);
    }
    gl.depthMask(true);
    gl.disable(gl.BLEND);
  }

  _drawShadows(viewProj, poses) {
    if (poses.length === 0) return;
    const gl = this.gl;
    const { program, uniforms } = this._floorDiscShadow;
    gl.useProgram(program);
    gl.uniformMatrix4fv(uniforms.uViewProj, false, viewProj);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
    gl.depthMask(false);
    bindMeshAttribs(gl, this._unitDisc, 0, -1);
    for (const pose of poses) {
      const world = mapPoseToWorld(pose);
      // Tighter than the body's own footprint so it reads as a real
      // contact shadow hugging the feet, not a wide wash. uStrength is
      // lower than the old dark-stage value because SHADOW_COLOR is now a
      // neutral grey rather than black — a soft grey shadow can afford a
      // touch more coverage without ever reading as a hole in the floor.
      const model = mat4Chain(
        mat4FromTranslation([world.position[0], 0.0015, world.position[2]]),
        mat4FromScale([0.10, 1, 0.075]),
      );
      gl.uniformMatrix4fv(uniforms.uModel, false, model);
      gl.uniform1f(uniforms.uStrength, 0.55);
      drawMesh(gl, this._unitDisc);
    }
    gl.depthMask(true);
    gl.disable(gl.BLEND);
  }

  _drawDucks(viewProj, eye, poses, dt) {
    if (!this._duckAssets || poses.length === 0) return;
    const gl = this.gl;
    const { program, uniforms } = this._lit;
    gl.useProgram(program);
    gl.uniformMatrix4fv(uniforms.uViewProj, false, viewProj);
    gl.uniform3f(uniforms.uEyePos, eye[0], eye[1], eye[2]);

    for (const pose of poses) {
      const cast = this._castByRole.get(pose.role);
      const color = colorToRgb((cast && cast.color) || '#e8dfc8');
      let walkState = this._walkStates.get(pose.role);
      walkState = updateWalkState(walkState, pose.walkPhase || 0, dt, pose.resting);
      this._walkStates.set(pose.role, walkState);

      const world = mapPoseToWorld(pose);
      const rootModel = mat4Chain(
        mat4FromTranslation(world.position),
        mat4FromYRotation(world.headingRotationY),
      );
      const rimBoost = pose.role === this._selected ? 0.9 : 0.0;

      drawDuck(gl, this._lit, this._duckAssets, rootModel, pose, walkState, color, rimBoost);
    }
  }

  dispose() {
    this._disposed = true;
    if (this._cameraRAF) cancelAnimationFrame(this._cameraRAF);
    this.canvas.removeEventListener('webglcontextlost', this._handleContextLost);
    this.canvas.removeEventListener('webglcontextrestored', this._handleContextRestored);
    const gl = this.gl;
    for (const p of [this._lit, this._ground, this._floorDiscShadow, this._floorDiscMark, this._line]) {
      if (p) gl.deleteProgram(p.program);
    }
    freeGlMesh(gl, this._groundMesh);
    freeGlMesh(gl, this._unitDisc);
    for (const buf of this._trailBuffers.values()) gl.deleteBuffer(buf.vbo);
    disposeDuckAssets(gl, this._duckAssets);
  }
}

/** '#rrggbb' | [r,g,b] (0..1) -> [r,g,b] (0..1). */
export function colorToRgb(color) {
  if (Array.isArray(color)) return color;
  const hex = String(color).replace('#', '');
  const n = parseInt(hex.length === 3 ? hex.split('').map((c) => c + c).join('') : hex, 16);
  return [((n >> 16) & 255) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255];
}
