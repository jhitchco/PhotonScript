"""`python -m photonscript ...` = the photonscript CLI (the remote CLI runs it so)."""
from photonscript.cli import app

if __name__ == "__main__":
    app(prog_name="photonscript")
