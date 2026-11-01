"""car_package: a per-car package for dsfforza.asi [archive] packages (synthetic files, no game data)."""

import struct
import tempfile
import unittest
from pathlib import Path

from dsfforza.build import car_package
from dsfforza.formats.chnk import Entry, read_chunk, write_chunk
from dsfforza.formats.vehicles import VSCT_RECORD


def leaf(tag: str, payload: bytes, align: int = 4) -> Entry:
    return Entry(tag=tag, offset=0, size=len(payload), version=0, align_log2=align, name=None, payload=payload)


def node(tag: str, children: list[Entry], align: int = 12) -> Entry:
    return Entry(tag=tag, offset=0, size=0, version=0, align_log2=align, name=None, children=children)


def vehc(car: int | None) -> Entry:
    kids = [leaf("MODL", b"\0" * 16)]
    if car is not None:
        kids.append(leaf("VO3D", struct.pack("<I4sII", car, b"VO3D", 1, 0)))
    return node("VEHC", [node("NONR", kids, align=4)])


def vsct_record(car: int, name: bytes) -> bytes:
    return VSCT_RECORD.pack(car, 1, 2, 3, name, *range(6))


class CarPackageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.chnk = self.tmp / "car.chnk"
        with self.chnk.open("wb") as f:
            write_chunk(f, None, [vehc(1), vehc(None), vehc(None)])
        records = b"".join(vsct_record(c, n) for c in (205, 266) for n in (b"host", b"host", b"host"))
        table = b"VSCT" + struct.pack("<HH", 1, 6) + records
        self.dsf = self.tmp / "dngvehicles.sp"
        with self.dsf.open("wb") as f:
            write_chunk(f, None, [node("SSIC", [node("NONR", [leaf("VSCT", table)], align=4)])])

    def test_make(self):
        out = self.tmp / "game" / "dsfforza" / "cars" / "car.sp"
        self.assertEqual(car_package.make(self.chnk, 700, 205, self.dsf, out), 1)
        with out.open("rb") as f:
            root = read_chunk(f, 0)
            self.assertEqual([e.tag for e in root.entries], ["VEHC", "VEHC", "VEHC", "VSCT"])
            self.assertEqual(car_package.car_id(f, root.entries[0]), 700)
            self.assertTrue(all(e.offset % 0x800 == 0 for e in root.entries[:3]))
            f.seek(root.entries[3].offset)
            vsct = f.read(root.entries[3].size)
        self.assertEqual(len(vsct), 3 * VSCT_RECORD.size)
        self.assertEqual({struct.unpack_from("<H", vsct, i * VSCT_RECORD.size)[0] for i in range(3)}, {205})

    def test_list(self):
        cars = self.tmp / "game" / "dsfforza" / "cars"
        cars.mkdir(parents=True)
        lst = cars / "cars.txt"
        car_package.add_to_list(lst, 700, cars / "a.sp")
        car_package.add_to_list(lst, 701, cars / "b.sp")
        car_package.add_to_list(lst, 700, cars / "c.sp")
        self.assertEqual(lst.read_text().splitlines(), ["701 dsfforza\\cars\\b.sp", "700 dsfforza\\cars\\c.sp"])

    def test_bad_id(self):
        with self.assertRaises(ValueError):
            car_package.make(self.chnk, 1000, 205, self.dsf, self.tmp / "x.sp")


if __name__ == "__main__":
    unittest.main()
