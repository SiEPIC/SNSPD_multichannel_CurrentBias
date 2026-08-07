# SNSPD_multichannel_CurrentBias
Multichannel current source for SNSPD biasing



# GUI Launch:

install uv python environment: https://docs.astral.sh/uv/getting-started/installation 

## MacOS and Linux:

MacOS: brew install libusb \
Linux: sudo apt install libusb-1.0-0 (use distro specific package manager) 

cd GUI/ \
uv venv --python 3.10 \
source .venv/bin/activate \
uv pip install -r requirements.txt 

## Windows:

uv venv --python 3.10 \
source .venv\Scripts\activate \
uv pip install -r requirements.txt 

## Nix flake (For Linux or MacOS):

Nix installation guide: https://nix.dev/install-nix.html \
No need to install uv or libusb 

cd GUI/ \
nix develop 

