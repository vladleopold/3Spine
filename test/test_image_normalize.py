import base64
import importlib.util
import os
import sys
import tempfile
import unittest
from io import BytesIO

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".codespace"))

from image_normalize import normalize_tree
from fetch_assets import atlas_page_refs, pick_assets
from unpack_atlases import unpack


class ImagePipelineTest(unittest.TestCase):
    def setUp(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow is not installed")
        self.Image = Image

    def _webp(self):
        buf = BytesIO()
        self.Image.new("RGBA", (8, 8), (20, 100, 220, 255)).save(buf, "WEBP")
        return buf.getvalue()

    def test_nested_webp_and_data_uri_get_png_fallbacks_and_unpack(self):
        with tempfile.TemporaryDirectory() as root:
            nested = os.path.join(root, "provider", "packs", "720_webp", "fx")
            os.makedirs(nested)
            webp = self._webp()
            with open(os.path.join(nested, "sheet.webp"), "wb") as fh:
                fh.write(webp)
            with open(os.path.join(root, "data.image.webp.hash.txt"), "wb") as fh:
                fh.write(b"data:image/webp;base64," + base64.b64encode(webp))
            with open(os.path.join(nested, "hero.atlas"), "w", encoding="utf-8") as fh:
                fh.write("sheet.webp\nsize: 8, 8\nformat: RGBA8888\nfilter: Linear, Linear\n"
                         "tile\n  rotate: false\n  xy: 0, 0\n  size: 8, 8\n")

            report = normalize_tree(root)
            self.assertEqual(report["converted"], 1)
            self.assertEqual(report["data_uri"], 1)
            self.assertTrue(os.path.isfile(os.path.join(nested, "sheet.png")))
            self.assertTrue(os.path.isfile(os.path.join(root, "data.image.webp.hash.png")))

            out = os.path.join(root, "out")
            count, _ = unpack(root, output=out)
            self.assertGreaterEqual(count, 1)
            tiles = [os.path.join(dp, f) for dp, _ds, fs in os.walk(root)
                     for f in fs if f == "tile.png"]
            self.assertTrue(tiles)

    def test_unknown_provider_paths_and_query_strings_are_kept(self):
        atlas = ("textures/nested/sheet.avif\nsize: 8, 8\n"
                 "region\n  xy: 0, 0\n  size: 8, 8\n")
        self.assertEqual(atlas_page_refs(atlas), ["textures/nested/sheet.avif"])
        picked = pick_assets({
            "https://unknown.example/a/hero.atlas?v=2",
            "https://unknown.example/a/hero.skel?hash=abc",
            "https://unknown.example/a/textures/hero.ktx2?rev=4",
            "https://unknown.example/a/textures/hero.avif#x",
        }, {"json", "atlas", "png"})
        self.assertEqual(len(picked["atlas"]), 1)
        self.assertEqual(len(picked["skel"]), 1)
        self.assertEqual(len(picked["png"]), 2)

    def test_3oaks_manifest_accepts_full_srcs_paths(self):
        script = os.path.join(os.path.dirname(__file__), "..", ".codespace",
                              "3oaks", "extract_3oaks.py")
        spec = importlib.util.spec_from_file_location("extract_3oaks_test", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        source = (b'{name:"hero",srcs:"./assets/packs/720_webp/hero.skel"}'
                  b'{name:"cfg",srcs:"./configs/not-spine.json"}')
        names = {m.group(1).decode() for m in module.NAME_RE.finditer(source)}
        self.assertIn("hero.skel", names)
        self.assertIn("not-spine.json", names)


if __name__ == "__main__":
    unittest.main()
