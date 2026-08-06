{
  description = "Iris dev environment with Python 3.10";

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
            pkgs.stdenv.cc.cc.lib                   
            pkgs.python310Packages.pyqt5            
            pkgs.qt5.qtbase
            pkgs.qt5.qtwayland
            pkgs.libGL
            pkgs.libusb1
            pkgs.usbutils
          ];

          shellHook = ''
            export LD_LIBRARY_PATH=${pkgs.stdenv.cc.cc.lib}/lib:${pkgs.qt5.qtbase}/lib:${pkgs.libGL}/lib:${pkgs.libusb1.out}/lib:$LD_LIBRARY_PATH
            export QT_PLUGIN_PATH=${pkgs.qt5.qtbase}/${pkgs.qt5.qtbase.qtPluginPrefix}

            export PYTHONPATH=${pkgs.python310Packages.pyqt5}/${pkgs.python310.sitePackages}:$PYTHONPATH

            if [ ! -d .venv ]; then
              uv venv --python ${pkgs.python310}/bin/python3.10
            fi

            source .venv/bin/activate
            echo "Python: $(python --version)"
          '';
        };
      }
    );
}
