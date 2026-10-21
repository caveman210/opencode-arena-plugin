"""The published manifest is a contract.

opencode reads it to find the plugin, npm reads it to build the tarball, and
the README tells the user what to write in the config. These tests keep the
three in step, so a rename or a dropped file cannot slip through.
"""
import json
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)

# Everything the package needs at runtime. A file that is not in the files
# list does not ship, and the plugin breaks in the hands of the user.
RUNTIME_FILES = [
    "SKILL.md",
    "arena-models.json",
    "bracket.py",
    "rubric.md",
    "strategies.json",
    "plugin/arena-router.ts",
    "opencode/agents/arena.md",
    "opencode/commands/arena.md",
    "bin/arena.js",
    "scripts/install.mjs",
    "scripts/check.mjs",
    "README.md",
    "LICENSE",
]


def read_json(name):
    with open(os.path.join(PKG, name), encoding="utf-8") as handle:
        return json.load(handle)


def read_text(name):
    with open(os.path.join(PKG, name), encoding="utf-8") as handle:
        return handle.read()


class ManifestTest(unittest.TestCase):
    def setUp(self):
        self.manifest = read_json("package.json")

    def test_name_is_the_published_name(self):
        self.assertEqual(self.manifest["name"], "opencode-arena-plugin")

    def test_name_is_unscoped_and_usable(self):
        name = self.manifest["name"]
        self.assertFalse(name.startswith("@"), "the package is unscoped on purpose")
        self.assertRegex(name, r"^[a-z0-9][a-z0-9._-]*$")

    def test_version_is_a_release_version(self):
        self.assertRegex(self.manifest["version"], r"^\d+\.\d+\.\d+$")

    def test_readme_mentions_the_package_name(self):
        self.assertIn('"%s"' % self.manifest["name"], read_text("README.md"))

    def test_skill_mentions_the_package_name(self):
        self.assertIn(self.manifest["name"], read_text("SKILL.md"))

    def test_the_plugin_entrypoint_ships(self):
        exports = self.manifest["exports"]
        for key in (".", "./server"):
            target = exports[key]
            self.assertTrue(os.path.isfile(os.path.join(PKG, target)), target)

    def test_opencode_is_told_which_versions_work(self):
        self.assertIn("opencode", self.manifest["engines"])
        self.assertIn("node", self.manifest["engines"])

    def test_no_os_restriction(self):
        # An os field makes npm refuse the install on every other platform.
        self.assertNotIn("os", self.manifest)

    def test_it_publishes_public(self):
        self.assertEqual(self.manifest["publishConfig"]["access"], "public")

    def test_the_package_page_has_the_links(self):
        for field in ("repository", "homepage", "bugs"):
            self.assertIn(field, self.manifest)

    def test_the_cli_is_there(self):
        self.assertIn("arena", self.manifest["bin"])

    def test_every_runtime_file_ships(self):
        files = self.manifest["files"]
        for name in RUNTIME_FILES:
            self.assertIn(name, files, "%s is not in the files list" % name)
            self.assertTrue(os.path.isfile(os.path.join(PKG, name)), name)

    def test_every_listed_file_exists(self):
        for name in self.manifest["files"]:
            self.assertTrue(os.path.exists(os.path.join(PKG, name)), name)

    def test_the_postinstall_never_fails_an_install(self):
        # opencode installs plugins with install scripts off, and a broken
        # postinstall must never break the install of the package.
        self.assertIn("||", self.manifest["scripts"]["postinstall"])

    def test_the_plugin_exports_the_opencode_shape(self):
        source = read_text("plugin/arena-router.ts")
        self.assertRegex(source, r"export\s+default\s*\{[^}]*\bid\s*:")
        self.assertIn("satisfies PluginModule", source)


class PublishedDocsTest(unittest.TestCase):
    def test_the_readme_shows_the_one_line_install(self):
        readme = read_text("README.md")
        self.assertIn('"plugin": ["opencode-arena-plugin"]', readme)

    def test_the_readme_says_what_is_needed(self):
        readme = read_text("README.md").lower()
        self.assertIn("python 3", readme)
        self.assertIn("wsl", readme)


if __name__ == "__main__":
    unittest.main()
