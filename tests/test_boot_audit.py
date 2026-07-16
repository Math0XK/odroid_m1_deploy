"""Tests de l'audit de boot (`boot_audit`) et du journal (`report`) — sans
matériel. L'audit est né d'un échec réel : boot.scr régénéré par flash-kernel
avec un root=UUID périmé -> clone en shell (initramfs). Ces tests verrouillent
chaque contrôle qui aurait attrapé ce cas AVANT de déclarer le clone bon.
"""
import struct
import zlib

import boot_audit as ba
import clone_core as cc
from report import Reporter

NEW_UUID = "890332bc-429c-4303-848e-e06840d88bcf"
OLD_UUID = "eee2b90d-659e-4c1a-97cd-44f881d34d45"
VALID_IDS = {NEW_UUID}


def _make_uboot(body: bytes) -> bytes:
    """Image legacy uImage (type script) avec CRC corrects."""
    header = bytearray(64)
    struct.pack_into(">I", header, 0x00, 0x27051956)     # ih_magic
    struct.pack_into(">I", header, 0x0C, len(body))      # ih_size
    struct.pack_into(">B", header, 0x1E, 6)              # ih_type = script
    struct.pack_into(">I", header, 0x18, zlib.crc32(body) & 0xffffffff)  # ih_dcrc
    struct.pack_into(">I", header, 0x04, 0)
    struct.pack_into(">I", header, 0x04, zlib.crc32(bytes(header)) & 0xffffffff)
    return bytes(header) + body


# --------------------------------------------------------------------------
# CRC uImage
# --------------------------------------------------------------------------
def test_uimage_crc_ok_image_saine():
    assert ba.uimage_crc_ok(_make_uboot(b"setenv bootargs quiet\n"))


def test_uimage_crc_ok_detecte_corruption():
    img = bytearray(_make_uboot(b"setenv bootargs quiet\n"))
    img[70] ^= 0xFF                       # corrompt le corps
    assert not ba.uimage_crc_ok(bytes(img))


def test_uimage_crc_ok_refuse_non_uimage():
    assert not ba.uimage_crc_ok(b"#!/bin/sh\n")
    assert not ba.uimage_crc_ok(b"")


# --------------------------------------------------------------------------
# boot.scr : root= et jetons attendus
# --------------------------------------------------------------------------
def test_bootscr_root_du_clone_est_go():
    img = _make_uboot(b"setenv bootargs root=UUID=" + NEW_UUID.encode()
                      + b" rootwait cma=128M rw\n")
    checks = ba.check_bootscr(img, VALID_IDS, expect_tokens=("cma=128M",))
    assert all(c.ok for c in checks)
    go, n_fail, _ = ba.verdict(checks)
    assert go and n_fail == 0


def test_bootscr_root_etranger_est_nogo():
    # LE cas vécu : boot.scr régénéré avec l'UUID périmé du master.
    img = _make_uboot(b"setenv bootargs root=UUID=" + OLD_UUID.encode()
                      + b" rootwait rw\n")
    checks = ba.check_bootscr(img, VALID_IDS)
    root_check = next(c for c in checks if "root=" in c.name)
    assert not root_check.ok and root_check.severity == ba.SEV_FAIL
    go, n_fail, _ = ba.verdict(checks)
    assert not go and n_fail >= 1


def test_bootscr_cma_absent_est_signale():
    img = _make_uboot(b"setenv bootargs root=UUID=" + NEW_UUID.encode() + b" rw\n")
    checks = ba.check_bootscr(img, VALID_IDS, expect_tokens=("cma=128M",))
    cma = next(c for c in checks if "cma=128M" in c.name)
    assert not cma.ok and cma.severity == ba.SEV_WARN
    go, _, n_warn = ba.verdict(checks)
    assert go and n_warn == 1          # boote, mais NPU dégradé -> avertissement


def test_bootscr_crc_faux_est_nogo():
    img = bytearray(_make_uboot(b"root=UUID=" + NEW_UUID.encode() + b"\n"))
    img[70] ^= 0xFF
    checks = ba.check_bootscr(bytes(img), VALID_IDS)
    crc = next(c for c in checks if "CRC" in c.name)
    assert not crc.ok and crc.severity == ba.SEV_FAIL


# --------------------------------------------------------------------------
# fstab / configs de régénération
# --------------------------------------------------------------------------
def test_fstab_coherent_et_etranger():
    ok_checks = ba.check_fstab(f"UUID={NEW_UUID} / ext4 defaults 0 1\n", VALID_IDS)
    assert all(c.ok for c in ok_checks)
    bad_checks = ba.check_fstab(f"UUID={OLD_UUID} / ext4 defaults 0 1\n", VALID_IDS)
    assert not bad_checks[0].ok and bad_checks[0].severity == ba.SEV_FAIL


def test_fstab_ignore_les_commentaires_sans_id():
    checks = ba.check_fstab("# commentaire\ntmpfs /tmp tmpfs defaults 0 0\n",
                            VALID_IDS)
    assert checks[0].ok                # aucun UUID -> pas d'échec


def test_config_regen_avec_id_perime_est_signalee():
    texts = {"etc/default/flash-kernel":
             f'LINUX_KERNEL_CMDLINE="root=UUID={OLD_UUID} cma=128M"\n'}
    checks = ba.check_regen_config(texts, VALID_IDS)
    assert not checks[0].ok and checks[0].severity == ba.SEV_WARN


# --------------------------------------------------------------------------
# Features ext (mkfs récent vs noyau 5.10 de l'unité)
# --------------------------------------------------------------------------
def test_features_identiques_est_ok():
    feats = {"ext_attr", "dir_index", "filetype", "extent", "64bit"}
    checks = ba.check_ext_features(feats, set(feats), "racine (p2)")
    assert checks[0].ok


def test_feature_risquee_en_plus_est_nogo():
    src = {"ext_attr", "extent"}
    dst = src | {"orphan_file"}        # défaut e2fsprogs >= 1.47
    checks = ba.check_ext_features(src, dst, "racine (p2)")
    assert not checks[0].ok and checks[0].severity == ba.SEV_FAIL
    assert "orphan_file" in checks[0].msg


def test_feature_anodine_en_plus_est_warn():
    checks = ba.check_ext_features({"extent"}, {"extent", "dir_nlink"}, "p2")
    assert not checks[0].ok and checks[0].severity == ba.SEV_WARN


def test_parse_ext_features():
    out = ("dumpe2fs 1.46.5 (30-Dec-2021)\n"
           "Filesystem volume name:   rootfs\n"
           "Filesystem features:      has_journal ext_attr resize_inode "
           "dir_index filetype extent 64bit\n")
    assert cc.parse_ext_features(out) == {
        "has_journal", "ext_attr", "resize_inode", "dir_index", "filetype",
        "extent", "64bit"}
    assert cc.parse_ext_features("") == set()


# --------------------------------------------------------------------------
# Fichiers de boot
# --------------------------------------------------------------------------
def test_boot_files_kernel_et_initrd():
    checks = ba.check_boot_files(["vmlinuz-5.10.0", "uInitrd-5.10.0", "boot.scr"])
    assert all(c.ok for c in checks)
    checks = ba.check_boot_files(["boot.scr"])
    assert not any(c.ok for c in checks if c.severity == ba.SEV_FAIL)


# --------------------------------------------------------------------------
# run_audit de bout en bout (arborescence tmp_path = clone monté)
# --------------------------------------------------------------------------
def test_run_audit_clone_sain_est_go(tmp_path):
    boot = tmp_path / "boot"; boot.mkdir()
    root = tmp_path / "root"; (root / "etc").mkdir(parents=True)
    scr = _make_uboot(b"setenv bootargs root=UUID=" + NEW_UUID.encode()
                      + b" rootwait cma=128M rw\n")
    (boot / "boot.scr").write_bytes(scr)
    (boot / "vmlinuz-5.10.0").write_bytes(b"k")
    (boot / "uInitrd-5.10.0").write_bytes(b"i")
    (root / "etc" / "fstab").write_text(f"UUID={NEW_UUID} / ext4 defaults 0 1\n")
    checks = ba.run_audit(str(root), str(boot), VALID_IDS,
                          expect_tokens=("cma=128M",))
    go, n_fail, _ = ba.verdict(checks)
    assert go and n_fail == 0


def test_run_audit_bootscr_perime_est_nogo(tmp_path):
    # Reproduit l'échec vécu : boot.scr régénéré avec l'UUID du master,
    # config flash-kernel encore périmée.
    boot = tmp_path / "boot"; boot.mkdir()
    root = tmp_path / "root"; (root / "etc" / "default").mkdir(parents=True)
    scr = _make_uboot(b"setenv bootargs root=UUID=" + OLD_UUID.encode() + b" rw\n")
    (boot / "boot.scr").write_bytes(scr)
    (boot / "vmlinuz-5.10.0").write_bytes(b"k")
    (boot / "uInitrd-5.10.0").write_bytes(b"i")
    (root / "etc" / "fstab").write_text(f"UUID={NEW_UUID} / ext4 defaults 0 1\n")
    (root / "etc" / "default" / "flash-kernel").write_text(
        f'LINUX_KERNEL_CMDLINE="root=UUID={OLD_UUID}"\n')
    checks = ba.run_audit(str(root), str(boot), VALID_IDS)
    go, n_fail, n_warn = ba.verdict(checks)
    assert not go and n_fail >= 1      # root= étranger : bloquant
    assert n_warn >= 1                 # config flash-kernel périmée : signalée


def test_run_audit_sans_bootscr_est_nogo(tmp_path):
    boot = tmp_path / "boot"; boot.mkdir()
    root = tmp_path / "root"; (root / "etc").mkdir(parents=True)
    (root / "etc" / "fstab").write_text(f"UUID={NEW_UUID} / ext4 defaults 0 1\n")
    checks = ba.run_audit(str(root), str(boot), VALID_IDS)
    go, _, _ = ba.verdict(checks)
    assert not go


# --------------------------------------------------------------------------
# Reporter (journal structuré)
# --------------------------------------------------------------------------
def test_reporter_numerotation_et_collecte():
    lines = []
    r = Reporter(sink=lambda level, text: lines.append((level, text)))
    r.begin(3)
    r.step("Préparation")
    r.step("Copie")
    r.warn("attention")
    r.error("boum")
    assert ("step", "ÉTAPE 1/3 — Préparation") in lines
    assert ("step", "ÉTAPE 2/3 — Copie") in lines
    assert r.warnings == ["attention"] and r.errors == ["boum"]
    summary = "\n".join(r.summary_lines())
    assert "attention" in summary and "boum" in summary


def test_reporter_begin_remet_a_zero():
    r = Reporter(sink=lambda level, text: None)
    r.begin(2)
    r.step("a"); r.warn("w")
    r.begin(5)
    assert r.step_no == 0 and r.warnings == []
