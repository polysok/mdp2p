"""PyInstaller entry point.

Importing mdp2p_client.app (not __main__) avoids the relative-import
trap where PyInstaller treats the launched script as the synthetic
__main__ module, breaking `from .foo import bar` inside the package.
"""

from mdp2p_client.app import run

if __name__ == "__main__":
    run()
