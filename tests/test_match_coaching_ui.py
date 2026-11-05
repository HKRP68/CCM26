"""Exercise the result-screen renderer with literal, untrusted text."""

from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which("node"), "Node is needed for the result-screen renderer")
class CoachingUITests(unittest.TestCase):
    def test_renderer_limits_points_escapes_text_and_clears_stale_coaching(self):
        source = Path(__file__).resolve().parents[1] / "static/cricket/app.js"
        script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
  constructor(tag, text = '') { this.tag = tag; this.text = text; this.children = []; this.hidden = false;
    this.classList = {toggle: (name, value) => { if (name === 'hidden') this.hidden = value; }}; }
  replaceChildren() { this.children = []; this.text = ''; }
  appendChild(child) { this.children.push(child); return child; }
  set textContent(value) { this.text = value; this.children = []; }
  get textContent() { return this.text + this.children.map(child => child.textContent).join(''); }
  set innerHTML(value) { throw new Error('Coaching must not parse user text as HTML'); }
}
const section = new Element('section');
const document = {getElementById: () => section, createElement: tag => new Element(tag),
  createTextNode: text => new Element('#text', text)};
const code = fs.readFileSync(process.argv[1], 'utf8');
const start = code.indexOf('function renderPostMatchCoaching(');
const end = code.indexOf('function renderResultScreen()', start);
const context = vm.createContext({document});
vm.runInContext(code.slice(start, end), context);
const render = context.renderPostMatchCoaching;
render({turning_points: [{text: '<script>literal</script>'}, {text: 'Second'}, {text: 'Third'}],
        tips: [{team: '<img>', text: 'Practice singles.'}]});
assert.equal(section.hidden, false);
assert.ok(section.textContent.includes('<script>literal</script>'));
assert.ok(section.textContent.includes('<img>: Practice singles.'));
const list = section.children.find(child => child.tag === 'ol');
assert.equal(list.children.length, 2);
assert.ok(!section.textContent.includes('Third'));
render(null);
assert.equal(section.hidden, true);
assert.equal(section.children.length, 0);
render({tips: [{team: 'Next match', text: 'Different tip.'}]});
assert.equal(section.hidden, false);
assert.ok(!section.textContent.includes('Second'));
assert.ok(section.textContent.includes('Different tip.'));
"""
        result = subprocess.run([shutil.which("node"), "-e", script, str(source)],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
