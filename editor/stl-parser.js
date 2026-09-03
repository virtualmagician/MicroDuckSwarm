// stl-parser.js — a small binary-STL reader (docs/viewer.md "real meshes
// when available"). No dependencies, no CDN.
//
// Every mesh in pollen-robotics/microduck_rl's onshape-to-robot export
// (assets/microduck/mjcf/assets/*.stl, gitignored, user-supplied — never
// vendored into this repo) is binary STL: an 80-byte free-form header, a
// little-endian uint32 triangle count, then that many 50-byte facet
// records (a normal + 3 vertices, all float32, plus a 2-byte "attribute
// byte count" this reader ignores). This module reads exactly that
// format and nothing else — ASCII STL ("solid ... facet normal ...")
// is a different, text-based format this repo has never seen a Pollen
// asset use, so it is detected and reported rather than misparsed as
// binary garbage.
//
// Output shape matches viewer-gl.js's uploadMesh()/bindMeshAttribs()
// contract exactly (VERTEX_STRIDE=6: interleaved x,y,z,nx,ny,nz per
// vertex, non-indexed — an identity index buffer, since STL itself never
// shares vertices between triangles) so `uploadMesh(gl, parseBinarySTL(buf))`
// works with no adaptation.

const HEADER_BYTES = 80;
const COUNT_BYTES = 4;
const FACET_BYTES = 50; // 12 (normal) + 3*12 (vertices) + 2 (attribute byte count)
export const VERTEX_STRIDE = 6; // must match viewer-gl.js's own constant of the same name

/** True if `buffer` starts with the ASCII-STL "solid" keyword (case-sensitive, as the format requires). */
export function looksLikeAsciiSTL(buffer) {
  if (buffer.byteLength < 5) return false;
  const bytes = new Uint8Array(buffer, 0, 5);
  let s = '';
  for (let i = 0; i < 5; i++) s += String.fromCharCode(bytes[i]);
  return s === 'solid';
}

/**
 * Parse a binary STL ArrayBuffer into { vertices, indices, vertexCount,
 * indexCount, triangleCount }. Throws (never returns partial/garbage data)
 * on a truncated file, a header/triangle-count size mismatch, or an ASCII
 * STL. The face normal is always recomputed from the triangle's own
 * winding (right-hand rule) rather than trusted from the file: onshape's
 * exporter, like many CAD tools, sometimes writes a zero vector there,
 * and a per-vertex flat normal derived from the actual geometry is never
 * wrong the way a stale or zeroed stored one can be.
 */
export function parseBinarySTL(buffer) {
  if (!(buffer instanceof ArrayBuffer)) throw new TypeError('parseBinarySTL expects an ArrayBuffer');
  // Checked before the length floor below: a short ASCII fixture/file
  // (its "solid" keyword sits in the first 5 bytes) should be reported as
  // "wrong format", not misdiagnosed as merely truncated binary.
  if (looksLikeAsciiSTL(buffer)) {
    throw new Error('ASCII STL is not supported by this parser — only binary STL (every Pollen mesh this project has seen is binary)');
  }
  if (buffer.byteLength < HEADER_BYTES + COUNT_BYTES) {
    throw new Error(`STL too short (${buffer.byteLength} bytes) to hold an 80-byte header + triangle count`);
  }
  const view = new DataView(buffer);
  const triangleCount = view.getUint32(HEADER_BYTES, true);
  const expectedBytes = HEADER_BYTES + COUNT_BYTES + triangleCount * FACET_BYTES;
  if (buffer.byteLength < expectedBytes) {
    throw new Error(
      `STL size mismatch: header claims ${triangleCount} triangles (needs ${expectedBytes} bytes) but the buffer is only ${buffer.byteLength} bytes`,
    );
  }

  const vertexCount = triangleCount * 3;
  const vertices = new Float32Array(vertexCount * VERTEX_STRIDE);
  let offset = HEADER_BYTES + COUNT_BYTES;
  for (let t = 0; t < triangleCount; t++) {
    offset += 12; // stored facet normal — skipped, recomputed below
    const p0x = view.getFloat32(offset, true), p0y = view.getFloat32(offset + 4, true), p0z = view.getFloat32(offset + 8, true);
    offset += 12;
    const p1x = view.getFloat32(offset, true), p1y = view.getFloat32(offset + 4, true), p1z = view.getFloat32(offset + 8, true);
    offset += 12;
    const p2x = view.getFloat32(offset, true), p2y = view.getFloat32(offset + 4, true), p2z = view.getFloat32(offset + 8, true);
    offset += 12;
    offset += 2; // attribute byte count — unused

    const ux = p1x - p0x, uy = p1y - p0y, uz = p1z - p0z;
    const vx = p2x - p0x, vy = p2y - p0y, vz = p2z - p0z;
    let nx = uy * vz - uz * vy, ny = uz * vx - ux * vz, nz = ux * vy - uy * vx;
    const len = Math.hypot(nx, ny, nz);
    if (len > 1e-12) { nx /= len; ny /= len; nz /= len; } else { nx = 0; ny = 0; nz = 1; } // degenerate (zero-area) triangle — arbitrary but harmless

    const base = t * 3 * VERTEX_STRIDE;
    vertices[base + 0] = p0x; vertices[base + 1] = p0y; vertices[base + 2] = p0z;
    vertices[base + 3] = nx; vertices[base + 4] = ny; vertices[base + 5] = nz;
    vertices[base + 6] = p1x; vertices[base + 7] = p1y; vertices[base + 8] = p1z;
    vertices[base + 9] = nx; vertices[base + 10] = ny; vertices[base + 11] = nz;
    vertices[base + 12] = p2x; vertices[base + 13] = p2y; vertices[base + 14] = p2z;
    vertices[base + 15] = nx; vertices[base + 16] = ny; vertices[base + 17] = nz;
  }

  const IndexArray = vertexCount > 65535 ? Uint32Array : Uint16Array;
  const indices = new IndexArray(vertexCount);
  for (let i = 0; i < vertexCount; i++) indices[i] = i;

  return { vertices, indices, vertexCount, indexCount: vertexCount, triangleCount };
}
