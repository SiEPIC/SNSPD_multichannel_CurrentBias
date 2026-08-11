{
  description = "SNSPD voltage source GUI (Python 3.10 + uv)";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-24.11";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
      in {
        devShells.default = pkgs.mkShell {
          buildInputs = [
            pkgs.python310
            pkgs.uv
            pkgs.libusb1
          ] ++ pkgs.lib.optional pkgs.stdenv.isLinux pkgs.gcc14.cc.lib;

          shellHook = ''
            ${pkgs.lib.optionalString pkgs.stdenv.isLinux ''
              export LD_LIBRARY_PATH=${pkgs.gcc14.cc.lib}/lib:${pkgs.libusb1.out}/lib:$LD_LIBRARY_PATH
            ''}

            if [ ! -d .venv ]; then
              uv venv --python ${pkgs.python310}/bin/python3.10
            fi
            source .venv/bin/activate
            uv pip install -q -r requirements.txt
            echo "Python: $(python --version)"
          '';
        };
      }
    );
}
