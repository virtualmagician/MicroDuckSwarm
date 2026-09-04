// Entry point that makes the documented gate -- `node --test editor/tests`
// (docs/authoring.md section 3, .github/workflows/ci.yml) -- work on every
// Node version without npm packages:
//
//   * Node 20 treats a directory argument as a place to search and runs the
//     `*.test.mjs` files directly; this file does not match a test-file
//     name pattern, so it is simply ignored there.
//   * Node >= 21 treats the argument as a glob and runs whatever it matches
//     as a *test file*; a bare directory resolves (via package.json
//     "type": "module") to this file, which registers every suite in one
//     process.
//
// Either way, exactly the suites below run, once. Keep this list in sync
// when adding a test file.
import './sampler.test.mjs';
import './validator.test.mjs';
import './roundtrip.test.mjs';
import './integrate.test.mjs';
import './editops.test.mjs';
import './beatgrid.test.mjs';
import './formations.test.mjs';
import './viewer-gl.test.mjs';
import './viewer-pose.test.mjs';
import './rehearsal.test.mjs';
import './stl-parser.test.mjs';
import './bake-cache.test.mjs';
import './asset-probe.test.mjs';
import './create-preview.test.mjs';
