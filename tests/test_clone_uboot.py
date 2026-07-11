"""Test du patch d'image script u-boot (`boot.scr`) par clone_core.

`rewrite_uboot_script` remplace l'UUID/PARTUUID/label-id de la source par ceux
du clone DANS un `boot.scr` binaire (là où vit `root=UUID=…` sur ODROID), à
longueur constante, puis recalcule les CRC du legacy uImage. Un CRC faux ferait
rejeter l'image par u-boot -> ce test verrouille la bonne remise à jour des CRC.
"""
import struct
import zlib

import clone_core as cc


def _make_uboot(body: bytes) -> bytes:
    """Fabrique une image legacy uImage (type script) avec CRC corrects."""
    header = bytearray(64)
    struct.pack_into(">I", header, 0x00, 0x27051956)     # ih_magic
    struct.pack_into(">I", header, 0x0C, len(body))      # ih_size
    struct.pack_into(">B", header, 0x1E, 6)              # ih_type = script
    struct.pack_into(">I", header, 0x18, zlib.crc32(body) & 0xffffffff)  # ih_dcrc
    struct.pack_into(">I", header, 0x04, 0)
    struct.pack_into(">I", header, 0x04, zlib.crc32(bytes(header)) & 0xffffffff)
    return bytes(header) + body


def _crc_ok(img: bytes) -> bool:
    header, body = bytearray(img[:64]), img[64:]
    if struct.unpack(">I", header[0x18:0x1C])[0] != (zlib.crc32(body) & 0xffffffff):
        return False
    stored = struct.unpack(">I", header[0x04:0x08])[0]
    struct.pack_into(">I", header, 0x04, 0)
    return stored == (zlib.crc32(bytes(header)) & 0xffffffff)


def test_patch_remplace_uuid_et_refait_les_crc():
    old = "eee2b90d-659e-4c1a-97cd-44f881d34d45"
    new = "890332bc-429c-4303-848e-e06840d88bcf"
    img = _make_uboot(b"setenv bootargs root=UUID=" + old.encode() + b" quiet\n")
    assert _crc_ok(img)                                  # image de départ saine

    out = cc.rewrite_uboot_script(img, [(old, new)])
    assert out is not None
    assert new.encode() in out and old.encode() not in out
    assert len(out) == len(img)                          # longueur préservée
    assert _crc_ok(out)                                  # CRC data + header refaits


def test_patch_ordre_partuuid_avant_label_id():
    # label-id nu contenu dans le PARTUUID : le plus long doit passer en premier
    subs = {"103fe0ed": "389f9ac6", "103fe0ed-02": "389f9ac6-02"}
    ordered = sorted(subs.items(), key=lambda kv: -len(kv[0]))
    img = _make_uboot(b"root=PARTUUID=103fe0ed-02 ro\n")
    out = cc.rewrite_uboot_script(img, ordered)
    assert out is not None and b"389f9ac6-02" in out and b"103fe0ed" not in out
    assert _crc_ok(out)


def test_pas_une_image_uboot_ignoree():
    assert cc.rewrite_uboot_script(b"#!/bin/sh\nexit 0\n", [("a", "b")]) is None


def test_aucune_correspondance_retourne_none():
    img = _make_uboot(b"setenv bootargs root=LABEL=rootfs ro\n")
    assert cc.rewrite_uboot_script(img, [("deadbeef", "cafebabe")]) is None


def test_cma_preserve_a_travers_la_reecriture():
    # Le fix NPU (cma=128M dans les bootargs) doit survivre à la réécriture de
    # l'UUID racine du clone — la substitution est à longueur constante.
    old = "eee2b90d-659e-4c1a-97cd-44f881d34d45"
    new = "890332bc-429c-4303-848e-e06840d88bcf"
    body = (b"setenv bootargs root=UUID=" + old.encode() +
            b" rootwait cma=128M rw quiet\n")
    out = cc.rewrite_uboot_script(_make_uboot(body), [(old, new)])
    assert out is not None
    assert b"cma=128M" in out                    # le fix NPU n'a pas bougé
    assert new.encode() in out and old.encode() not in out
    assert _crc_ok(out)


def test_extract_root_ids_sur_image_uboot_complete():
    # extract_root_ids accepte aussi l'en-tête uImage (magic 0x27051956).
    old = "eee2b90d-659e-4c1a-97cd-44f881d34d45"
    img = _make_uboot(b"root=UUID=" + old.encode() + b" ro\n")
    assert ("UUID", old) in cc.extract_root_ids(img)
