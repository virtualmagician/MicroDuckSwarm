// Binary-STL parsing against a hand-built fixture (docs/viewer.md "real
// meshes when available") — no real Pollen asset is committed to this
// repo, so the fixture is constructed byte-for-byte in this file.
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { parseBinarySTL, looksLikeAsciiSTL, VERTEX_STRIDE } from '../stl-parser.js';

const HEADER_BYTES = 80;
const COUNT_BYTES = 4;
const FACET_BYTES = 50;

/** Build a binary-STL ArrayBuffer from a list of triangles, each [ [x,y,z]x3 ], with a given (possibly wrong/zero) stored normal per facet. */
function buildStl(triangles, { storedNormal = [0, 0, 0], headerText = 'fixture' } = {}) {
  const buf = new ArrayBuffer(HEADER_BYTES + COUNT_BYTES + triangles.length * FACET_BYTES);
  const view = new DataView(buf);
  for (let i = 0; i < headerText.length && i < HEADER_BYTES; i++) view.setUint8(i, headerText.charCodeAt(i));
  view.setUint32(HEADER_BYTES, triangles.length, true);
  let offset = HEADER_BYTES + COUNT_BYTES;
  for (const tri of triangles) {
    view.setFloat32(offset, storedNormal[0], true); view.setFloat32(offset + 4, storedNormal[1], true); view.setFloat32(offset + 8, storedNormal[2], true);
    offset += 12;
    for (const [x, y, z] of tri) {
      view.setFloat32(offset, x, true); view.setFloat32(offset + 4, y, true); view.setFloat32(offset + 8, z, true);
      offset += 12;
    }
    view.setUint16(offset, 0, true); // attribute byte count
    offset += 2;
  }
  return buf;
}

test('parses a single triangle: vertex count, positions, and a recomputed CCW normal', () => {
  // A right triangle in the XY plane: (0,0,0) -> (1,0,0) -> (0,1,0).
  // Right-hand winding around that loop gives face normal +Z.
  const buf = buildStl([[[0, 0, 0], [1, 0, 0], [0, 1, 0]]]);
  const mesh = parseBinarySTL(buf);
  assert.equal(mesh.triangleCount, 1);
  assert.equal(mesh.vertexCount, 3);
  assert.equal(mesh.indexCount, 3);
  assert.equal(mesh.vertices.length, 3 * VERTEX_STRIDE);
  assert.deepEqual(Array.from(mesh.indices), [0, 1, 2]);
  // positions
  assert.deepEqual(Array.from(mesh.vertices.slice(0, 3)), [0, 0, 0]);
  assert.deepEqual(Array.from(mesh.vertices.slice(6, 9)), [1, 0, 0]);
  assert.deepEqual(Array.from(mesh.vertices.slice(12, 15)), [0, 1, 0]);
  // normal (same for all 3 vertices of a flat facet) — recomputed, not the zero stored value
  for (const base of [3, 9, 15]) {
    assert.ok(Math.abs(mesh.vertices[base]) < 1e-9);
    assert.ok(Math.abs(mesh.vertices[base + 1]) < 1e-9);
    assert.ok(Math.abs(mesh.vertices[base + 2] - 1) < 1e-9);
  }
});

test('a garbage stored normal is ignored in favour of the geometric one', () => {
  const buf = buildStl([[[0, 0, 0], [1, 0, 0], [0, 1, 0]]], { storedNormal: [9, -9, 42] });
  const mesh = parseBinarySTL(buf);
  assert.ok(Math.abs(mesh.vertices[5] - 1) < 1e-9); // still +Z, not the stored garbage
});

test('two triangles: triangle/vertex counts and per-facet independent vertices (no sharing)', () => {
  const buf = buildStl([
    [[0, 0, 0], [1, 0, 0], [0, 1, 0]],
    [[0, 0, 1], [0, 0, 0], [1, 0, 0]], // shares two positions with triangle 0 but must NOT share indices
  ]);
  const mesh = parseBinarySTL(buf);
  assert.equal(mesh.triangleCount, 2);
  assert.equal(mesh.vertexCount, 6);
  assert.deepEqual(Array.from(mesh.indices), [0, 1, 2, 3, 4, 5]);
});

test('Uint16 indices below the 65536-vertex threshold, Uint32 at/above it', () => {
  const small = parseBinarySTL(buildStl([[[0, 0, 0], [1, 0, 0], [0, 1, 0]]]));
  assert.ok(small.indices instanceof Uint16Array);

  const manyTriangles = Math.ceil(65536 / 3) + 1; // vertexCount just over 65535
  const tris = Array.from({ length: manyTriangles }, (_, i) => [[i, 0, 0], [i, 1, 0], [i, 0, 1]]);
  const big = parseBinarySTL(buildStl(tris));
  assert.ok(big.vertexCount > 65535);
  assert.ok(big.indices instanceof Uint32Array);
});

test('a degenerate (zero-area) triangle gets a harmless fallback normal, not NaN/Infinity', () => {
  const buf = buildStl([[[0, 0, 0], [0, 0, 0], [0, 0, 0]]]);
  const mesh = parseBinarySTL(buf);
  for (let i = 0; i < mesh.vertices.length; i++) assert.ok(Number.isFinite(mesh.vertices[i]));
});

test('rejects a buffer too short to hold a header + triangle count', () => {
  assert.throws(() => parseBinarySTL(new ArrayBuffer(10)), /too short/);
});

test('rejects a truncated facet section (size mismatch)', () => {
  const buf = buildStl([[[0, 0, 0], [1, 0, 0], [0, 1, 0]], [[0, 0, 1], [1, 0, 1], [0, 1, 1]]]);
  const truncated = buf.slice(0, buf.byteLength - 10); // header claims 2 triangles, body only has room for 1.8
  assert.throws(() => parseBinarySTL(truncated), /size mismatch/);
});

test('rejects an ASCII STL rather than misreading it as binary', () => {
  const text = 'solid fixture\nfacet normal 0 0 1\nouter loop\nendloop\nendfacet\nendsolid fixture\n';
  const bytes = new TextEncoder().encode(text);
  const buf = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
  assert.throws(() => parseBinarySTL(buf), /ASCII STL/);
});

test('looksLikeAsciiSTL detects the "solid" keyword and nothing else', () => {
  const ascii = new TextEncoder().encode('solid x');
  assert.equal(looksLikeAsciiSTL(ascii.buffer), true);
  const binary = buildStl([[[0, 0, 0], [1, 0, 0], [0, 1, 0]]]); // header starts with "fixture", not "solid"
  assert.equal(looksLikeAsciiSTL(binary), false);
  assert.equal(looksLikeAsciiSTL(new ArrayBuffer(2)), false); // too short to tell
});

test('parseBinarySTL rejects non-ArrayBuffer input', () => {
  assert.throws(() => parseBinarySTL('not a buffer'), TypeError);
});
