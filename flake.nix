{
  description = "Download images from YouTube Community posts and channel post feeds";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "x86_64-darwin"
        "aarch64-darwin"
      ];
      eachSystem = f: nixpkgs.lib.genAttrs systems f;
    in
    {
      packages = eachSystem (system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in
        {
          default = pkgs.python3Packages.buildPythonApplication {
            pname = "yt-community-images";
            version = "0.2.0";
            src = ./.;
            pyproject = true;

            build-system = with pkgs.python3Packages; [ setuptools ];
            dependencies = with pkgs.python3Packages; [ requests ];

            # The unit tests are also exposed via `nix flake check`.
            doCheck = false;
            pythonImportsCheck = [ "yt_community_images" ];
            meta = {
              description = "Download full-resolution images from public YouTube posts or channels";
              mainProgram = "yt-community-images";
              platforms = pkgs.lib.platforms.unix;
            };
          };
        });

      apps = eachSystem (system: {
        default = {
          type = "app";
          program = "${self.packages.${system}.default}/bin/yt-community-images";
        };
      });

      devShells = eachSystem (system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in
        {
          default = pkgs.mkShell {
            packages = [ self.packages.${system}.default pkgs.python3 ];
          };
        });

      checks = eachSystem (system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          python = pkgs.python3.withPackages (ps: [ ps.requests ]);
        in
        {
          unit-tests = pkgs.runCommand "yt-community-images-unit-tests" {
            nativeBuildInputs = [ python ];
          } ''
            export PYTHONPATH=${./src}
            python -m unittest discover -s ${./tests} -v
            touch "$out"
          '';
        });
    };
}
