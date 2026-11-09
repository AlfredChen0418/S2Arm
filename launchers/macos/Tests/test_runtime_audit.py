import pathlib, shutil, subprocess, sys, tempfile, unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import runtime_audit


class MinimumMacosTests(unittest.TestCase):
    def test_only_macos_slices_count(self):
        text = "x:\nLoad command 8\n      cmd LC_BUILD_VERSION\n platform IOS\n    minos 18.0\n"
        self.assertEqual(runtime_audit.minimum_macos(text), [])
        self.assertEqual(runtime_audit.minimum_macos(" platform MACOS\n    minos 26.0\n      sdk 26.4\n"), ["26.0"])

    def test_version_order_is_numeric(self):
        self.assertGreater(runtime_audit.version_tuple("15.10"), runtime_audit.version_tuple("15.6"))

    @unittest.skipUnless(sys.platform == "darwin" and shutil.which("clang"), "needs Xcode")
    def test_real_binaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = pathlib.Path(tmp) / "A.app"; app.mkdir()
            source = pathlib.Path(tmp) / "a.c"; source.write_text("int main(void){return 0;}\n")
            def compile_for(minimum, name):
                subprocess.run(["clang", f"-mmacosx-version-min={minimum}", str(source), "-o", str(app / name)], check=True)
            compile_for("15.0", "ok")
            self.assertEqual(runtime_audit.verify_minimum_macos(app, "15.0"), 1)
            compile_for("26.0", "new")
            with self.assertRaises(RuntimeError) as caught:
                runtime_audit.verify_minimum_macos(app, "15.0")
            self.assertIn("new (macOS 26.0)", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
