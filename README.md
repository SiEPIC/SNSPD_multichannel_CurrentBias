# SNSPD_multichannel_CurrentBias
Multichannel current source for SNSPD biasing



# GUI Launch:

install uv python environment: https://docs.astral.sh/uv/getting-started/installation 

## MacOS and Linux:

**MacOS:**

```bash
brew install libusb
```

**Linux:**

```bash
sudo apt install libusb-1.0-0  
```

(Use distro specific package manager)


```bash
cd GUI/ 
uv venv --python 3.10 
source .venv/bin/activate 
uv pip install -r requirements.txt 
```

## Windows:

```bash
uv venv --python 3.10 
source .venv\Scripts\activate
uv pip install -r requirements.txt 
```

## Nix flake (For Linux or MacOS):

Nix installation guide: https://nix.dev/install-nix.html \
No need to separately install uv or libusb

```bash
cd GUI/ 
nix develop 
```


# Firmware Flashing:

