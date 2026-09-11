# Third-party components

This Windows executable embeds CPython and third-party libraries. It does not require a separately installed Python runtime.

- VSOA 1.0.4: Apache-2.0. Copyright and license notices are retained in the packaged third-party license files.
- psutil 7.2.2: license text copied from the installed distribution.
- PyYAML 6.0.3: license text copied from the installed distribution.
- CPython 3.13.9: Python license text copied from the build interpreter.
- Bundled OpenSSL, libffi and other CPython runtime components: see the additional standard-library license document and Python notices in `licenses/`.
- PyInstaller 6.22.2: build tool and bootloader; its distribution license, including its applicable bootloader exception, is retained.
- Additional build-tool package notices are retained where provided by their installed distributions.

See `licenses/` for authoritative license texts rather than relying on this summary. See `build_manifest.json` for versions and source hashes. No third-party library source was modified by this benchmark.

The executable is not Authenticode-signed. SHA-256 checksums provide integrity comparison against the delivered archive, not a publisher identity certificate or malware-free guarantee.
