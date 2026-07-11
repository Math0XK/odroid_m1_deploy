"""Configuration pytest du paquet de déploiement (autonome).

Rend `tools/` importable (`import spi_core`, `clone_core`, `check_deploy`…) sans
installer le paquet. Fonctionne aussi bien depuis le repo Harvest que depuis un
futur repo séparé qui ne contiendrait que `odroid_m1_deploy/`.
"""
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS))
