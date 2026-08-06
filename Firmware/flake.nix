{
  description = "ESP-IDF development shell";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    esp-dev.url = "github:mirrexagon/nixpkgs-esp-dev";
  };

  outputs = {
    self,
    nixpkgs,
    esp-dev,
  }: let
    system = "x86_64-linux";
    pkgs = import nixpkgs {
      inherit system;
      config.permittedInsecurePackages = [
        "python3.13-ecdsa-0.19.1"
        "python3.10-ecdsa-0.19.1"
      ];
    };

    esp-idf = esp-dev.packages.${system}.esp-idf-full;
  in {
    devShells.${system}.default = pkgs.mkShell {
      name = "esp-project";
      buildInputs = [
        esp-idf
      ];
      shellHook = ''
        alias esp32='idf.py set-target esp32'
        alias b='idf.py build'
        alias fm='idf.py -p /dev/ttyUSB0 flash && idf.py monitor'
        alias f='idf.py -p /dev/ttyUSB0 flash'
        alias m='idf.py monitor'
        alias bf='idf.py build && idf.py -p /dev/ttyUSB0 flash'
      '';
    };
  };
}
