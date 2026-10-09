import importlib.util
import pathlib
import tempfile
import unittest

SCRIPT_PATH = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "homebrew_formula.py"
SPEC = importlib.util.spec_from_file_location("homebrew_formula", SCRIPT_PATH)
assert SPEC and SPEC.loader
homebrew_formula = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(homebrew_formula)


class HomebrewFormulaTest(unittest.TestCase):
    def setUp(self):
        self.source = (
            "https://github.com/corral-dev/corral/archive/refs/tags/v0.24.144.tar.gz",
            "source-sha",
            False,
        )
        self.macos = (
            "https://github.com/corral-dev/corral/releases/download/v0.24.144/corral-macos-universal2.whl",
            "mac-sha",
            True,
        )
        self.linux_x86 = (
            "https://github.com/corral-dev/corral/releases/download/v0.24.144/corral-manylinux-x86_64.whl",
            "x86-sha",
            True,
        )
        self.linux_arm = (
            "https://github.com/corral-dev/corral/releases/download/v0.24.144/corral-manylinux-aarch64.whl",
            "arm-sha",
            True,
        )

    def formula(self, macos=None, linux_x86=None, linux_arm=None):
        return homebrew_formula.build_formula(
            "0.24.144",
            {
                "macos": macos or self.macos,
                "linux_x86_64": linux_x86 or self.linux_x86,
                "linux_aarch64": linux_arm or self.linux_arm,
            },
        )

    def test_all_wheels_do_not_declare_rust_toolchain(self):
        formula = self.formula()

        self.assertIn("corral-macos-universal2.whl", formula)
        self.assertIn("corral-manylinux-x86_64.whl", formula)
        self.assertIn("corral-manylinux-aarch64.whl", formula)
        self.assertNotIn('depends_on "maturin"', formula)
        self.assertNotIn('depends_on "rust"', formula)
        self.assertIn('Dir["*.whl"].first', formula)

    def test_linux_source_fallback_scopes_build_dependencies_to_linux(self):
        formula = self.formula(linux_x86=self.source, linux_arm=self.source)

        expected = """  on_linux do
    depends_on "maturin" => :build
    depends_on "rust" => :build
  end"""
        self.assertIn(expected, formula)
        self.assertIn("archive/refs/tags/v0.24.144.tar.gz", formula)
        self.assertEqual(formula.count('depends_on "maturin"'), 1)
        self.assertEqual(formula.count('depends_on "rust"'), 1)

    def test_all_source_fallback_declares_build_dependencies_once(self):
        formula = self.formula(
            macos=self.source,
            linux_x86=self.source,
            linux_arm=self.source,
        )

        self.assertIn('  depends_on "maturin" => :build', formula)
        self.assertIn('  depends_on "rust" => :build', formula)
        self.assertEqual(formula.count('depends_on "maturin"'), 1)
        self.assertEqual(formula.count('depends_on "rust"'), 1)
        self.assertIn("该平台无预编译包：源码编译兜底", formula)

    def test_existing_formula_version_reads_source_and_wheel_urls(self):
        with tempfile.TemporaryDirectory() as tmp:
            formula = pathlib.Path(tmp) / "corral.rb"
            formula.write_text(
                '  url "https://github.com/corral-dev/corral/archive/refs/tags/v0.24.120.tar.gz"\n'
                '    url "https://github.com/corral-dev/corral/releases/download/v0.24.144/corral.whl"\n',
                encoding="utf-8",
            )

            self.assertEqual(
                homebrew_formula.existing_formula_version(formula),
                "0.24.144",
            )

    def test_parse_sums_accepts_binary_marker(self):
        self.assertEqual(
            homebrew_formula.parse_sums("abc  file.whl\ndef *binary.whl\n"),
            {"file.whl": "abc", "binary.whl": "def"},
        )


if __name__ == "__main__":
    unittest.main()
