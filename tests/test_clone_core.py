"""Tests de la logique PURE du clonage (clone_core) — sans GUI ni matériel.

Couvre le nommage de partitions, la génération d'identité, la réécriture du
script sfdisk (identité neuve + racine étendue) et le repérage des points de
montage système. `clone_core` n'importe que la stdlib : ces tests tournent
partout, sans cv2/tkinter/paramiko.
"""
import re

import clone_core as cc

# Dump sfdisk réaliste (2 partitions dos, comme une image ODROID).
SFDISK_DUMP = """label: dos
label-id: 0x103fe0ed
device: /dev/sdc
unit: sectors

/dev/sdc1 : start=        2048, size=     1048576, type=83, bootable
/dev/sdc2 : start=     1050624, size=   123684864, type=83
"""


def test_part_name_gere_tous_les_types():
    assert cc.part_name("/dev/sda", 1) == "/dev/sda1"
    assert cc.part_name("/dev/nvme0n1", 2) == "/dev/nvme0n1p2"
    assert cc.part_name("/dev/mmcblk0", 1) == "/dev/mmcblk0p1"
    assert cc.part_name("/dev/loop0", 3) == "/dev/loop0p3"


def test_gen_label_id_dos_est_8_hex():
    v = cc.gen_label_id("dos")
    assert re.fullmatch(r"[0-9a-f]{8}", v)


def test_gen_label_id_gpt_est_un_uuid():
    v = cc.gen_label_id("gpt")
    assert re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                        r"[0-9a-f]{4}-[0-9a-f]{12}", v)


def test_gen_label_id_distinct_a_chaque_appel():
    assert cc.gen_label_id("dos") != cc.gen_label_id("dos")


def test_build_dst_script_identite_neuve_et_racine_etendue():
    out = cc.build_dst_script(SFDISK_DUMP, new_label_id="389f9ac6")
    # identité source ignorée, nouvelle identité posée (préfixe 0x pour dos)
    assert "0x103fe0ed" not in out
    assert "label-id: 0x389f9ac6" in out
    assert "label: dos" in out and "unit: sectors" in out
    # p1 conservée (taille + bootable) ; l'espacement interne du dump est gardé
    assert re.search(r"start=\s*2048,\s*size=\s*1048576,\s*type=83, bootable", out)
    # DERNIÈRE partition étendue : son size= a disparu (plus aucune trace de sa valeur)
    assert "123684864" not in out
    assert re.search(r"start=\s*1050624, type=83\s*$", out.strip())


def test_build_dst_script_sans_partition_leve():
    import pytest
    with pytest.raises(RuntimeError, match="Aucune partition"):
        cc.build_dst_script("label: dos\nunit: sectors\n")


def test_parse_start_size_extrait_les_secteurs():
    assert cc.parse_start_size(SFDISK_DUMP, "/dev/sdc1") == (2048, 1048576)
    assert cc.parse_start_size(SFDISK_DUMP, "/dev/sdc2") == (1050624, 123684864)


def test_parse_start_size_partition_absente_leve():
    import pytest
    with pytest.raises(RuntimeError, match="introuvable"):
        cc.parse_start_size(SFDISK_DUMP, "/dev/sdc9")


def test_is_system_mp():
    assert cc.is_system_mp("/")
    assert cc.is_system_mp("/boot")
    assert cc.is_system_mp("/boot/efi")
    assert cc.is_system_mp("/usr")
    assert not cc.is_system_mp("/home")
    assert not cc.is_system_mp("/media/odroid/BOOT")
    assert not cc.is_system_mp("/mnt/clone_dst_root")


# --- bootloader_gap_present : purement informatif (repérer un vestige) ---
def test_bootloader_gap_present_detecte_le_magic_rk():
    assert cc.bootloader_gap_present(cc.RK_IDBLOADER_MAGIC + b"\x00" * 508)


def test_bootloader_gap_present_faux_si_absent_ou_court():
    assert not cc.bootloader_gap_present(b"\x00" * 512)   # secteur nul
    assert not cc.bootloader_gap_present(b"")             # rien lu
    assert not cc.bootloader_gap_present(b"\x55\xaa")     # trop court


# --- image_size_bytes : dimensionnement d'une image compacte ---
def test_image_size_bytes_compacte_et_alignee():
    # racine : 20 Go utilisés ; p2 démarre au secteur 1050624 (~513 MiO)
    p2_start, used = 1050624, 20 * 10**9
    size = cc.image_size_bytes(p2_start, used)
    assert size % 2**20 == 0                       # aligné au MiO
    assert size >= p2_start * 512 + used           # jamais plus petit que le contenu
    assert size <= p2_start * 512 + int(used * 1.3)  # compact (pas la taille du disque)


def test_image_size_bytes_marge_couvre_le_garde_fou_du_moteur():
    # Le moteur refuse si used*1.05 > capacité racine : la marge par défaut
    # (×1.25) doit laisser de la place même avec ~5 % de métadonnées mkfs.
    used = 25 * 10**9
    size = cc.image_size_bytes(1050624, used)
    root_capacity = (size - 1050624 * 512) * 0.95  # -5 % métadonnées ext4
    assert used * 1.05 < root_capacity


def test_image_size_bytes_plancher_pour_racine_quasi_vide():
    size = cc.image_size_bytes(2048, 10 * 2**20)   # racine 10 MiO seulement
    assert size >= 2048 * 512 + cc.IMG_ROOT_FLOOR_BYTES


# --- extract_root_ids : repérer l'identifiant racine réel du boot.scr ---
def test_extract_root_ids_uuid_depuis_corps_brut():
    body = b"setenv bootargs root=UUID=eee2b90d-659e-4c1a-97cd-44f881d34d45 ro\n"
    ids = cc.extract_root_ids(body)
    assert ("UUID", "eee2b90d-659e-4c1a-97cd-44f881d34d45") in ids
    # « UUID= » ne doit PAS matcher à l'intérieur d'un « PARTUUID= »
    assert all(k != "PARTUUID" for k, _ in ids)


def test_extract_root_ids_partuuid_pas_double_compte():
    body = b"root=PARTUUID=103fe0ed-02 rootwait rw\n"
    assert cc.extract_root_ids(body) == [("PARTUUID", "103fe0ed-02")]


def test_extract_root_ids_dedup_et_vide():
    body = b"root=UUID=aa11-bb22 ... UUID=aa11-bb22 ...\n"
    assert cc.extract_root_ids(body) == [("UUID", "aa11-bb22")]
    assert cc.extract_root_ids(b"root=/dev/nvme0n1p2 ro\n") == []
