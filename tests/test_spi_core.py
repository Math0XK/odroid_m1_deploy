"""Tests de la logique PURE de la chaîne SPI (spi_core) — sans flashrom ni puce.

Couvre les garde-fous anti-brique (`looks_like_bootloader`), les constructeurs de
commandes (`flashrom_cmd`, `fw_setenv_commands`), la détection `ethaddr`, et les
parsers de vérification post-déploiement. `spi_core` n'importe que la stdlib
(hashlib/re) : ces tests tournent partout, y compris sous Windows.
"""
import hashlib

import spi_core as sc


# --------------------------------------------------------------------------
# Empreintes / garde-fou anti-brique
# --------------------------------------------------------------------------
def test_spi_size_est_16_mio():
    assert sc.SPI_SIZE == 16 * 1024 * 1024


def test_sha256_bytes():
    data = b"golden-ish payload"
    assert sc.sha256_bytes(data) == hashlib.sha256(data).hexdigest()


def _valid_image():
    """16 MiO non uniforme avec une bannière « U-Boot »."""
    data = bytearray(sc.SPI_SIZE)
    for i in range(0, 8192, 7):
        data[i] = i & 0xFF
    data[0x4000:0x4010] = b"U-Boot 2026.01\x00\x00"   # après le remplissage
    return bytes(data)


def test_looks_like_bootloader_accepte_image_plausible():
    ok, _ = sc.looks_like_bootloader(_valid_image())
    assert ok


def test_looks_like_bootloader_refuse_mauvaise_taille():
    ok, reason = sc.looks_like_bootloader(b"\x00" * 1024)
    assert not ok and "taille" in reason


def test_looks_like_bootloader_refuse_toute_nulle():
    ok, reason = sc.looks_like_bootloader(bytes(sc.SPI_SIZE))
    assert not ok and "nulle" in reason


def test_looks_like_bootloader_refuse_puce_effacee():
    ok, reason = sc.looks_like_bootloader(b"\xff" * sc.SPI_SIZE)
    assert not ok and "0xFF" in reason


def test_looks_like_bootloader_refuse_sans_banniere_uboot():
    data = bytearray(sc.SPI_SIZE)
    for i in range(0, 8192, 7):
        data[i] = (i & 0x7F) or 1      # non uniforme, mais pas de « U-Boot »
    ok, reason = sc.looks_like_bootloader(bytes(data))
    assert not ok and "U-Boot" in reason


# --------------------------------------------------------------------------
# Construction de commandes
# --------------------------------------------------------------------------
def test_flashrom_cmd_operations():
    assert sc.flashrom_cmd("read", "ch341a_spi", "g.bin") == \
        ["flashrom", "-p", "ch341a_spi", "-r", "g.bin"]
    assert sc.flashrom_cmd("write", "internal", "g.bin") == \
        ["flashrom", "-p", "internal", "-w", "g.bin"]
    assert sc.flashrom_cmd("verify", "linux_mtd:dev=0", "g.bin") == \
        ["flashrom", "-p", "linux_mtd:dev=0", "-v", "g.bin"]


def test_fw_setenv_commands_couvre_les_4_vars():
    cmds = sc.fw_setenv_commands()
    assert len(cmds) == 4
    names = [c[1] for c in cmds]
    assert set(names) == set(sc.CRITICAL_ENV_VARS)
    for name, value, cmd in ((c[1], c[2], c) for c in cmds):
        assert cmd[0] == "fw_setenv"
        assert value == sc.CRITICAL_ENV_VARS[name]


def test_env_has_ethaddr():
    assert sc.env_has_ethaddr("bootcmd=run distro_bootcmd\nethaddr=00:1e:06:aa:bb:cc\n")
    assert sc.env_has_ethaddr("  ETHADDR = 00:1e:06:11:22:33")   # casse/espaces
    assert not sc.env_has_ethaddr("bootcmd=run distro_bootcmd\nboot_targets=nvme\n")


# --------------------------------------------------------------------------
# Parsers de vérification post-déploiement
# --------------------------------------------------------------------------
def test_check_kernel():
    ok, _ = sc.check_kernel(sc.EXPECTED_KERNEL + "\n")
    assert ok
    ok, msg = sc.check_kernel("6.1.0-generic")
    assert not ok and sc.EXPECTED_KERNEL in msg


def test_root_on_nvme():
    assert sc.root_on_nvme("/dev/nvme0n1p2\n")
    assert not sc.root_on_nvme("/dev/mmcblk0p2")
    assert not sc.root_on_nvme("/dev/sda2")
    assert not sc.root_on_nvme("")


def test_scan_dmesg_npu_errors():
    clean = "rockchip-pm-domain: ok\nRKNPU: probe ok\n"
    assert sc.scan_dmesg_npu_errors(clean) == []
    bad = ("power-controller: failed to get ack on domain 'npu'\n"
           "RKNPU: failed to allocate 6389760 buffer\n")
    hits = sc.scan_dmesg_npu_errors(bad)
    assert len(hits) == 2


def test_missing_dri_nodes():
    assert sc.missing_dri_nodes(sc.REQUIRED_DRI_NODES) == []
    missing = sc.missing_dri_nodes(["/dev/dri/card0"])
    assert missing == ["/dev/dri/renderD128"]


def test_parse_inference_ms():
    assert sc.parse_inference_ms("run 1: 61.2 ms\nrun 2: 63.9 ms\nmoyenne 62.5 ms") == 62.5
    assert sc.parse_inference_ms("aucune latence ici") is None
