"""car_gui: what the car packages get in the generated GUI files (plan), string and code
allocation, png_rgba; synthetic inputs, no game data."""

import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

import numpy as np

from dsfforza.build import car_gui
from dsfforza.formats import xds
from dsfforza.game import gui_assets


def png_filtered(width: int, height: int, rgba: bytes) -> bytes:
    """An RGBA PNG whose rows use filters 0..4 in turn (as other encoders write them)."""
    stride, rows, prev = width * 4, b"", bytearray(width * 4)
    for y in range(height):
        line = bytearray(rgba[y * stride : (y + 1) * stride])
        f = y % 5
        out = bytearray(stride)
        for i in range(stride):
            a = line[i - 4] if i >= 4 else 0
            b = prev[i]
            c = prev[i - 4] if i >= 4 else 0
            if f == 0:
                pred = 0
            elif f == 1:
                pred = a
            elif f == 2:
                pred = b
            elif f == 3:
                pred = (a + b) // 2
            else:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if pa <= pb and pa <= pc else b if pb <= pc else c
            out[i] = (line[i] - pred) & 255
        rows += bytes([f]) + bytes(out)
        prev = line

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


class PngTest(unittest.TestCase):
    def test_round_trip_and_filters(self):
        rng = np.random.default_rng(5)
        px = bytes(rng.integers(0, 256, 7 * 6 * 4, dtype=np.uint8))
        self.assertEqual(xds.png_rgba(xds.png_bytes(7, 6, bytearray(px))), (7, 6, bytearray(px)))
        self.assertEqual(xds.png_rgba(png_filtered(7, 6, px)), (7, 6, bytearray(px)))
        with self.assertRaises(ValueError):
            xds.png_rgba(b"GIF89a")


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        # A maker and a model DSF has; vanilla strings up to 700001, one of ours ("BMW").
        self.makes = {"NISSAN": (54, "ID:242768")}
        self.models = {("NISSAN", "370Z"): (10, "ID:240000")}
        self.strings = {242768: "NISSAN", 240000: "370Z", 700000: "BMW", 700001: "SOMETHING"}
        patcher = mock.patch.object(gui_assets, "vanilla_codes", return_value=(self.makes, self.models))
        patcher.start()
        self.addCleanup(patcher.stop)

    def car(self, i: int, media: str, make: str, model: str, *, images: bool = True) -> dict:
        folder = self.tmp / media
        folder.mkdir()
        if images:
            (folder / "badge.png").write_bytes(xds.png_bytes(4, 3, bytearray(48)))
            (folder / "plate.png").write_bytes(xds.png_bytes(5, 2, bytearray(40)))
            (folder / "picture.dds").write_bytes(b"DDS " + bytes(124))
        manifest = {"names": {"maker_group": make, "model": model, "family": None}, "source": {"media": media}}
        return {"id": i, "key": f"forza.fh1.{media}", "manifest": manifest, "folder": folder}

    def test_plan(self):
        cars = [
            self.car(3, "NIS_370Z_10", "NISSAN", "370Z"),
            self.car(1, "BMW_M3_91", "BMW", "M3"),
            self.car(2, "BMW_M5_95", "BMW", "M5"),
            self.car(4, "XYZ_Car_10", "XYZ", "CAR", images=False),
        ]
        used_codes = {65, 256, 300}  # the font's: new 16-bit codes go past 300
        hud, glyphs, strings, pictures = car_gui.plan(
            cars, self.strings, used_codes, set(self.strings), emblem={c["id"] for c in cars}
        )
        # A vanilla maker and model: the game's badge and plate, no glyphs or strings.
        self.assertEqual(hud[3]["Manufacturer"], 54)
        self.assertEqual(hud[3]["ModelName"], "ID:240000")
        self.assertNotIn("ManufacturerGlyph", hud[3])
        # BMW: one badge for both cars, the ManufacturerName ID the files already had.
        self.assertEqual(hud[1]["ManufacturerGlyph"], 301)
        self.assertEqual(hud[2]["ManufacturerGlyph"], 301)
        self.assertEqual(hud[1]["ManufacturerName"], "ID:700000")
        self.assertEqual((hud[1]["ModelGlyph"], hud[2]["ModelGlyph"]), (302, 303))
        self.assertEqual(glyphs[301].kind, "badge")
        self.assertEqual(glyphs[302].kind, "plate")
        self.assertEqual((glyphs[302].w, glyphs[302].h), (5, 2))
        # New strings skip the IDs in use; the maker list gets the badge's one-character string.
        self.assertEqual(hud[1]["ModelName"], "ID:700002")
        self.assertEqual(strings[700002], "M3")
        self.assertEqual(strings[gui_assets.GLYPH_STRING_BASE + 301], chr(301))
        self.assertNotIn(700000, strings)
        # No images: names only.
        self.assertNotIn("ManufacturerGlyph", hud[4])
        self.assertNotIn("Picture1", hud[4])
        self.assertIn("ManufacturerName", hud[4])
        self.assertEqual(sorted(pictures), ["dsfforza_bmw_m3_91", "dsfforza_bmw_m5_95", "dsfforza_nis_370z_10"])
        self.assertEqual(hud[1]["Picture1"], "dsfforza_bmw_m3_91")
        self.assertTrue(all(e[gui_assets.EMBLEM_FIELD] == 1 for e in hud.values()))
        hud, *_ = car_gui.plan(cars[:1], self.strings, used_codes, set(self.strings), emblem=set())
        self.assertNotIn(gui_assets.EMBLEM_FIELD, hud[3])

    def test_family_plate(self):
        self.models[("NISSAN", "SKYLINE")] = (11, "ID:240001")
        car = self.car(5, "NIS_SkylineX_99", "NISSAN", "SKYLINE GT-R X")
        car["manifest"]["names"]["family"] = "Skyline"
        hud, *_ = car_gui.plan([car], self.strings, set(), set(self.strings), emblem=set())
        self.assertEqual((hud[5]["Model"], hud[5]["ModelName"]), (11, "ID:240001"))

    def test_hud_lua_and_clear(self):
        game = self.tmp / "game"
        (game / car_gui.GENERATED).mkdir(parents=True)
        car_gui.write_hud_lua(game / car_gui.HUD_LUA, {2: {"ModelName": "ID:700002", "ModelGlyph": 302}})
        text = (game / car_gui.HUD_LUA).read_text()
        self.assertIn('[2] = { ModelName = "ID:700002", ModelGlyph = 302 },', text)
        self.assertEqual(car_gui.generate(game, []), {"cars": 0})  # nothing enabled: no generated files
        self.assertFalse((game / car_gui.GENERATED).exists())
        self.assertFalse((game / car_gui.HUD_LUA).exists())


if __name__ == "__main__":
    unittest.main()


class EmblemTest(unittest.TestCase):
    """Each package's emblem: the most common one goes into the Fonts.fchunk copy, and the cars
    that carry it get GarageEmblem."""

    def setUp(self):
        from dsfforza.build import dsfcar

        self.tmp = Path(tempfile.mkdtemp())
        self.game = self.tmp / "game"
        (self.game / "media").mkdir(parents=True)
        (self.game / car_gui.FONTS).write_bytes(b"fonts")
        self.cars = []
        for i, name in enumerate(("XYZ_Car_10", "XYZ_Van_11", "XYZ_Bus_12"), start=7):
            folder = self.tmp / name
            folder.mkdir()
            names = {"maker_group": "XYZ", "model": name.split("_")[1].upper(), "family": None}
            manifest = {"names": names, "source": {"media": name}}
            self.cars.append({"id": i, "key": f"forza.fh1.{name}", "manifest": manifest, "folder": folder})
        self.emblem_file = dsfcar.EMBLEM
        for name, value in (("vanilla_codes", ({}, {})), ("read_strings", {})):
            patcher = mock.patch.object(gui_assets, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def carry(self, index: int, data: bytes) -> None:
        (Path(self.cars[index]["folder"]) / self.emblem_file).write_bytes(data)

    def generate(self, *, font_has_it: bool):
        def add_glyphs(*args):
            args[1].write_bytes(b"copy")  # (fonts, out, glyphs, textures)
            return {}

        with (
            mock.patch.object(car_gui, "font_state", return_value=(set(), font_has_it)),
            mock.patch.object(gui_assets, "add_glyphs", side_effect=add_glyphs) as add,
            mock.patch(
                "dsfforza.build.dsfcar.emblem_problems", side_effect=lambda d: [] if d.startswith(b"DDS") else ["bad"]
            ),
        ):
            report = car_gui.generate(self.game, self.cars, log=lambda _: None)
        hud = (self.game / car_gui.HUD_LUA).read_text()
        shown = [c["id"] for c in self.cars if f"[{c['id']}] = {{" in hud and self._has(hud, c["id"])]
        return report, add, shown

    @staticmethod
    def _has(hud: str, i: int) -> bool:
        line = next(line for line in hud.splitlines() if line.strip().startswith(f"[{i}] = "))
        return "GarageEmblem = 1" in line

    def test_each_package_carries_it(self):
        for i in range(3):
            self.carry(i, b"DDS emblem")
        report, add, shown = self.generate(font_has_it=False)
        self.assertEqual(add.call_args.args[2:], ({}, {gui_assets.FORZA_LOGO_UID: b"DDS emblem"}))  # no glyphs
        self.assertTrue(report["emblem"])
        self.assertEqual(shown, [7, 8, 9])
        redirect = (self.game / car_gui.GENERATED / car_gui.REDIRECT).read_text()
        self.assertIn(r"media\Fonts.fchunk|dsfforza\generated\Fonts.fchunk", redirect)

    def test_a_package_without_one_keeps_the_disc(self):
        self.carry(0, b"DDS emblem")
        self.carry(2, b"not a dds")  # a bad one counts as none
        _, add, shown = self.generate(font_has_it=False)
        self.assertEqual(add.call_args.args[3], {gui_assets.FORZA_LOGO_UID: b"DDS emblem"})
        self.assertEqual(shown, [7])

    def test_the_most_common_emblem_goes_in(self):
        self.carry(0, b"DDS other")
        self.carry(1, b"DDS emblem")
        self.carry(2, b"DDS emblem")
        _, add, shown = self.generate(font_has_it=False)
        self.assertEqual(add.call_args.args[3], {gui_assets.FORZA_LOGO_UID: b"DDS emblem"})
        self.assertEqual(shown, [8, 9])

    def test_a_font_that_has_it_gets_no_second_copy(self):
        self.carry(1, b"DDS emblem")
        report, add, shown = self.generate(font_has_it=True)
        add.assert_not_called()  # no glyphs, nothing to add: the game's font as it is
        self.assertFalse(report["emblem"])
        self.assertEqual(shown, [8])

    def test_no_emblem_no_field(self):
        _, add, shown = self.generate(font_has_it=False)
        add.assert_not_called()
        self.assertEqual(shown, [])


VANILLA_FONTS = Path(__file__).resolve().parents[1] / "out" / "vanilla" / "media" / "Fonts.fchunk"


@unittest.skipUnless(VANILLA_FONTS.exists(), "needs out/vanilla/media/Fonts.fchunk (tools/worktree-setup.ps1)")
class VanillaFontEmblemTest(unittest.TestCase):
    def test_emblem_round_trip(self):
        """add_glyphs with the emblem alone: the copy has it (font_texture reads it back, font_state
        sees it) and the vanilla glyphs are the same; the vanilla font has none."""
        from dsfforza.build import dsfcar

        emblem = gui_assets.picture_dds(gui_assets.forza_emblem_image(gui_assets.maker_badge("FORZA")))
        self.assertEqual(dsfcar.emblem_problems(emblem), [])
        self.assertIsNone(gui_assets.font_texture(VANILLA_FONTS, gui_assets.FORZA_LOGO_UID))
        out = Path(tempfile.mkdtemp()) / "Fonts.fchunk"
        gui_assets.add_glyphs(VANILLA_FONTS, out, {}, {gui_assets.FORZA_LOGO_UID: emblem})
        self.assertEqual(gui_assets.font_texture(out, gui_assets.FORZA_LOGO_UID), emblem)
        codes, has = car_gui.font_state(out)
        self.assertTrue(has)
        self.assertEqual(codes, car_gui.font_state(VANILLA_FONTS)[0])


class PictureRootLimitTest(unittest.TestCase):
    """The packages' gui.sp gets one SPOI per picture (issue #540: 721 pictures 5 to a SPOI crashed
    the garage), and a shared SPOI never holds more than a garage picture slot does."""

    def test_slot_capacity(self):
        # 12 range nodes per "GUI Image large" slot, 3 for its setup, 2 per texture (gui_assets).
        self.assertEqual(gui_assets.PICTURE_SLOT_NODES, 12)
        self.assertEqual(gui_assets.PICTURES_PER_SPOI, 4)

    def test_generate_passes_the_plugin_limit(self):
        tmp = Path(tempfile.mkdtemp())
        game = tmp / "game"
        (game / "media").mkdir(parents=True)

        def add_pictures(source, out, pictures, **kwargs):
            out.write_bytes(b"gui")
            return {}

        with (
            mock.patch.object(car_gui, "font_state", return_value=(set(), False)),
            mock.patch.object(car_gui, "plan", return_value=({}, {}, {}, {"dsfforza_x": b"DDS"})),
            mock.patch.object(gui_assets, "read_strings", return_value={}),
            mock.patch.object(gui_assets, "add_pictures", side_effect=add_pictures) as add,
        ):
            car_gui.generate(game, [{"id": 1, "key": "k", "manifest": {}, "folder": tmp}], log=lambda _: None)
        self.assertEqual(add.call_args.kwargs["root_limit"], car_gui.GUI_ROOT_LIMIT)


VANILLA_GUI = Path(__file__).resolve().parents[1] / "out" / "vanilla" / "Install" / "gui.sp"


@unittest.skipUnless(VANILLA_GUI.exists(), "needs out/vanilla/Install/gui.sp (tools/worktree-setup.ps1)")
class SharedPictureSpoiTest(unittest.TestCase):
    def setUp(self):
        from dsfforza.formats import chnk

        with VANILLA_GUI.open("rb") as stream:
            self.vanilla = len(chnk.read_chunk(stream, 0).entries)
        self.out = Path(tempfile.mkdtemp()) / "gui.sp"
        self.addCleanup(lambda: self.out.unlink(missing_ok=True))

    def pictures(self, n):
        return {f"dsfforza_test_{i}": b"DDS " + bytes([i]) * 16 for i in range(n)}

    def test_at_most_a_slot_to_a_spoi(self):
        from dsfforza.formats import chnk

        # Two free root entries: 8 pictures go 4 to a SPOI, each readable by name.
        report = gui_assets.add_pictures(VANILLA_GUI, self.out, self.pictures(8), root_limit=self.vanilla + 2)
        self.assertEqual(report["pictures_per_item"], 4)
        with self.out.open("rb") as stream:
            root = chnk.read_chunk(stream, 0)
            records = gui_assets.read_tables(stream, root)["records"]
            per_root = {}
            for _, index, _ in records:
                per_root[index] = per_root.get(index, 0) + 1
            self.assertEqual(max(per_root.values()), 4)
            for name, dds in self.pictures(8).items():
                self.assertEqual(gui_assets.find_picture(stream, root, name), dds)

    def test_more_than_a_slot_holds_is_refused(self):
        with self.assertRaises(ValueError):
            gui_assets.add_pictures(VANILLA_GUI, self.out, self.pictures(9), root_limit=self.vanilla + 2)
        self.assertFalse(self.out.exists())
