"""dsfcar: the distributed car package (format 1), written and checked on synthetic files."""

import json
import struct
import tempfile
import unittest
import zipfile
from pathlib import Path

from dsfforza.build import car_package, dsfcar
from dsfforza.formats import xds
from dsfforza.formats.chnk import Entry, write_chunk
from dsfforza.formats.vehicles import VSCT_RECORD


def leaf(tag: str, payload: bytes, align: int = 4) -> Entry:
    return Entry(tag=tag, offset=0, size=len(payload), version=0, align_log2=align, name=None, payload=payload)


def node(tag: str, children: list[Entry], align: int = 12) -> Entry:
    return Entry(tag=tag, offset=0, size=0, version=0, align_log2=align, name=None, children=children)


def vehc(car: int | None, nonr_pad: int = 0) -> Entry:
    kids = [leaf("MODL", b"\0" * (16 + nonr_pad))]
    if car is not None:
        kids.append(leaf("VO3D", struct.pack("<I4sII", car, b"VO3D", 1, 0)))
    return node("VEHC", [node("NONR", kids, align=4)])


def car_sp(path: Path, car: int = 1, host: int = 205, nonr_pad: int = 0, *, vsct: bool = True) -> bytes:
    records = b"".join(VSCT_RECORD.pack(host, 1, 2, 3, b"host", *range(6)) for _ in range(3))
    entries = [vehc(car, nonr_pad), vehc(None), vehc(None)]
    if vsct:
        entries.append(leaf("VSCT", records))
    with path.open("wb") as f:
        write_chunk(f, None, entries)
    return path.read_bytes()


def dds(w: int = 512, h: int = 256, fourcc: bytes = b"DXT5") -> bytes:
    head = bytearray(128)
    head[:4] = b"DDS "
    struct.pack_into("<II", head, 12, h, w)
    head[84:88] = fourcc
    return bytes(head) + b"\0" * (w * h)


def png(w: int, h: int) -> bytes:
    return xds.png_bytes(w, h, bytearray(w * h * 4))


def manifest(**over) -> dict:
    m = {
        "format": dsfcar.FORMAT,
        "format_version": 1,
        "key": "forza.fh1.TEST_Car_10",
        "version": 1,
        "car": {"file": "car.sp", "sha256": "0" * 64, "model_id": 1, "host": 205, "sound_host": 205},
        "names": {
            "display": "Test Car (2010)",
            "make": "TEST",
            "model": "CAR",
            "family": None,
            "year": 2010,
            "maker_group": "TEST",
        },
        "garage": {"speed": 0.5, "strength": 0.25, "drift": 1},
        "requires": ["archive.packages"],
        "source": {"pipeline": "test"},
    }
    m.update(over)
    return m


class DsfcarTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.sp = car_sp(self.tmp / "car.sp")
        self.images = {dsfcar.PICTURE: dds(), dsfcar.BADGE: png(58, 58), dsfcar.PLATE: png(98, 34)}

    def write(self, m=None, sp=None, images=None) -> Path:
        return dsfcar.write(
            self.tmp / "p.dsfcar", m or manifest(), sp or self.sp, self.images if images is None else images
        )

    def rewrite(self, path: Path, member: str, data: bytes) -> None:
        """Replace one member of a package (to make it bad on purpose)."""
        with zipfile.ZipFile(path) as z:
            items = {n: z.read(n) for n in z.namelist()}
        items[member] = data
        with zipfile.ZipFile(path, "w") as z:
            for n, d in items.items():
                z.writestr(n, d)

    def test_round_trip(self):
        path = self.write()
        self.assertEqual(dsfcar.validate(path), [])
        m, sp, images = dsfcar.read(path)
        self.assertEqual(sp, self.sp)
        self.assertEqual(m["car"]["sha256"], dsfcar.sha256(self.sp))
        self.assertEqual(m["images"], {"picture": "picture.dds", "badge": "badge.png", "plate": "plate.png"})
        self.assertEqual(set(images), set(self.images))
        with zipfile.ZipFile(path) as z:
            self.assertTrue(all(i.compress_type == zipfile.ZIP_STORED for i in z.infolist()))

    def test_images_optional(self):
        path = self.write(images={})
        self.assertEqual(dsfcar.validate(path), [])
        self.assertEqual(dsfcar.read(path)[0]["images"], {})

    def test_nothing_global(self):
        for bad in ({"spool_id": 700}, {"glyphs": {"ModelGlyph": 300}}):
            with self.assertRaises(ValueError):
                self.write(manifest(**bad))
        m = manifest()
        m["car"]["ManufacturerGlyph"] = 259
        with self.assertRaises(ValueError):
            self.write(m)

    def test_manifest_fields(self):
        cases = [
            {"format": "zip"},
            {"format_version": 2},
            {"key": "Forza.fh1.X"},
            {"key": "forza.fh1"},
            {"version": 0},
            {"version": True},
            {"requires": []},
            {"requires": ["archive.packages", "teleport"]},
            {"garage": {"speed": 1.5}},
            {"garage": {"top_speed": 0.5}},
        ]
        for over in cases:
            with self.subTest(over=over):
                self.assertTrue(dsfcar.manifest_problems(manifest(**over)))
        for car in ({"model_id": 0}, {"model_id": 1000}, {"host": 304}, {"sound_host": -1}):
            m = manifest()
            m["car"].update(car)
            self.assertTrue(dsfcar.manifest_problems(m), car)
        m = manifest()
        del m["car"]["host"]
        self.assertTrue(dsfcar.manifest_problems(m))
        m = manifest()
        m["names"]["model"] = " "
        self.assertTrue(dsfcar.manifest_problems(m))
        self.assertEqual(dsfcar.manifest_problems(manifest()), [])

    def test_car_sp_checks(self):
        self.assertEqual(dsfcar.car_sp_problems(self.sp, 1, 205), [])
        self.assertTrue(any("model ID 1" in p for p in dsfcar.car_sp_problems(self.sp, 2, 205)))
        self.assertTrue(any("not the host" in p for p in dsfcar.car_sp_problems(self.sp, 1, 206)))
        no_vsct = car_sp(self.tmp / "n.sp", vsct=False)
        self.assertTrue(any("not VEHC, VEHC, VEHC, VSCT" in p for p in dsfcar.car_sp_problems(no_vsct)))
        big = car_sp(self.tmp / "b.sp", nonr_pad=dsfcar.NONR_LIMITS[0])
        self.assertTrue(any("slot buffer" in p for p in dsfcar.car_sp_problems(big)))
        self.assertTrue(dsfcar.car_sp_problems(b"nope" * 8))
        self.assertTrue(any("ends past the file" in p for p in dsfcar.car_sp_problems(self.sp[: len(self.sp) // 2])))

    def test_validate_members(self):
        path = self.write()
        self.rewrite(path, "car.sp", car_sp(self.tmp / "o.sp", car=2))
        problems = dsfcar.validate(path)
        self.assertTrue(any("SHA-256" in p for p in problems))
        self.assertTrue(any("model ID 2" in p for p in problems))
        path = self.write()
        self.rewrite(path, "extra.txt", b"hi")
        self.assertTrue(any("unexpected member" in p for p in dsfcar.validate(path)))
        path = self.write(images={dsfcar.BADGE: png(58, 58)})
        self.rewrite(path, dsfcar.PLATE, png(98, 34))
        self.assertTrue(any("not in images" in p for p in dsfcar.validate(path)))
        (self.tmp / "z.dsfcar").write_bytes(b"not a zip")
        self.assertTrue(dsfcar.validate(self.tmp / "z.dsfcar"))
        path = self.write()
        self.rewrite(path, "car.json", b"{")
        self.assertTrue(any("car.json" in p for p in dsfcar.validate(path)))

    def test_images_checked(self):
        bad = [
            {dsfcar.BADGE: png(99, 58)},  # wider than the glyph cell
            {dsfcar.PLATE: png(98, 35)},  # taller than nameplate()'s 34
            {dsfcar.BADGE: b"GIF89a"},
            {dsfcar.PICTURE: dds(256, 256)},
            {dsfcar.PICTURE: dds(fourcc=b"DXT1")},
        ]
        for images in bad:
            with self.subTest(images=list(images)):
                self.assertTrue(dsfcar.validate(self.write(images=images)))

    def test_install_sp(self):
        """A package's car.sp installed with the manager's ID: only the ID fields differ."""
        out = self.tmp / "game" / "dsfforza" / "cars" / "x.sp"
        self.assertEqual(car_package.install_sp(self.tmp / "car.sp", 700, out), 1)
        with out.open("rb") as f:
            from dsfforza.formats.chnk import read_chunk

            root = read_chunk(f, 0)
            self.assertEqual([e.tag for e in root.entries], ["VEHC", "VEHC", "VEHC", "VSCT"])
            self.assertEqual(car_package.car_id(f, root.entries[0]), 700)
        self.assertEqual(dsfcar.car_sp_problems(out.read_bytes(), 700, 205), [])
        with self.assertRaises(ValueError):
            car_package.install_sp(self.tmp / "car.sp", 1000, out)

    def test_cli_validate(self):
        good = self.write()
        with self.assertRaises(SystemExit) as done:
            car_package.main(["validate", str(good)])
        self.assertEqual(done.exception.code, 0)
        bad = self.tmp / "bad.dsfcar"
        bad.write_bytes(b"x")
        with self.assertRaises(SystemExit) as done:
            car_package.main(["validate", str(good), str(bad)])
        self.assertEqual(done.exception.code, 1)

    def test_manifest_is_json_text(self):
        with zipfile.ZipFile(self.write()) as z:
            m = json.loads(z.read("car.json").decode("utf-8"))
        self.assertEqual(m["key"], "forza.fh1.TEST_Car_10")


if __name__ == "__main__":
    unittest.main()
