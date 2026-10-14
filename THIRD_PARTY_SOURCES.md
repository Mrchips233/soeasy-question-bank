# Third-party source and replacement information

This document concerns bundled third-party software. SoEasy's own application
code and the included `packaging_support.py` are released under the MIT license
recorded in the project's `LICENSE`. That license does not relicense the
third-party components or authorize redistribution of question banks.
Original upstream copyright notices and license texts are retained in the
following locations, which have different purposes:

- Application source repository: `third_party_licenses/`.
- Extracted application ZIP: `许可证/third_party/`.
- Companion ThirdPartySources ZIP: `licenses/`, plus the original notices
  within each source archive.
- Extracted application's `_internal/licenses/`: the generated GEOS import
  modification JSON; this is not the general third-party license directory.

The companion
[04-SoEasy-v1.2.2-beta.2-ThirdPartySources.zip](https://github.com/Mrchips233/soeasy-question-bank/releases/download/v1.2.2-beta.2/04-SoEasy-v1.2.2-beta.2-ThirdPartySources.zip)
is offered as an attachment alongside the executable ZIP in the
[same GitHub Release](https://github.com/Mrchips233/soeasy-question-bank/releases/tag/v1.2.2-beta.2).

The companion archive is for modification and rebuilding of the relevant
third-party components; it is not required to run the application. Refer to
`SOURCE_MANIFEST.json` for original download URLs and SHA-256 hashes. PyPI sdist
hashes were checked against the exact-version PyPI metadata. Other hashes record
the downloaded official upstream files; an independent upstream signature check
has not been performed.

## Components covered

| Bundled component | Corresponding materials | Version evidence |
| --- | --- | --- |
| GEOS 3.14.1, loaded by Shapely 2.2.0 | GEOS release tarball, Shapely sdist, CMake and package build files; `build_materials/packaging_support.py` reproduces the import-name adjustment | Installed Shapely wheel `DELVEWHEEL` names `geos-3.14.1`; the final package records original and modified hashes separately |
| certifi 2026.7.22 | Exact-version sdist, including the certificate source data shipped by this version | Installed distribution metadata and exact PyPI version |
| tqdm 4.70.1 | Exact-version sdist | Installed distribution metadata; original package declares MPL 2.0 and MIT |
| lxml 6.1.3 combined extension | Exact-version sdist with generated `src/lxml/etree.c`, build scripts and documentation; libxml2, libxslt, zlib and libiconv sources | Installed metadata; runtime compiled-version constants; Windows dependency versions in the matching lxml sdist |
| Windows libiconv 1.17.1 | Full `winlibs/libiconv` source tree at `880a1fa8b5581e37e136a7b051947d3ea39097b6`, including MSVC projects and LGPL text; GNU libiconv 1.17 reference tarball | The lxml Windows build config names `1.17.1`; the compiled macro reports `(1,17)` |
| Windows libxml2 2.11.9 | GNU/GNOME release and Windows source tree at `bb84678855e3407fc88180295c992f9c984e28e0` | Compiled-version constant and official Windows dependency release |
| Windows libxslt 1.1.45 | GNOME release and Windows source tree at `c0076eaa6e0d1f99e01107e8db225a308eb1e543` | Compiled-version constant and official Windows dependency release |
| Windows zlib 1.3.2 | Windows source tree at `0089522e62e6b5d4950e62bd20f0cb06a9288413` | lxml Windows sdist build configuration and dependency release |
| NumPy 1.26.4 / OpenBLAS | NumPy sdist, full OpenBLAS tree at `c2f4bdbbb43a1d20a7342f40122e18e573ce436a`, original Windows build recipes | NumPy DLL filename contains the commit prefix and `gcc_10_3_0`; NumPy `tools/openblas_support.py` pins this OpenBLAS commit |
| GCC libquadmath / libgfortran | Full GCC 10.3.0 release source, including runtime sources, configure/build scripts, LGPL/GPL texts and GCC Runtime Library Exception | Version in the OpenBLAS DLL filename; no source modification of these runtimes was independently established |
| pyclipper 1.4.0 / Clipper 6.4.2 | Exact-version pyclipper sdist with the Clipper source, original header attribution and Boost Software License 1.0 | Installed metadata and original Clipper source header |

## LGPL requirements and the approach used here

The relevant LGPL 2.1 text is preserved verbatim. Section 4 addresses distribution
of library object code, including corresponding source availability from the
same place when binaries are offered for download. Section 6 requires permission
to modify the relevant work for the customer's own use and reverse engineering
for debugging such modifications, plus one of its source/relinking mechanisms.

GEOS is loaded as separate DLLs. A user may replace compatible modified GEOS DLLs
in `_internal/Shapely.libs`, retaining the filenames expected by the extension.
Full GEOS and Shapely sources are included. This source provision is not a
three-year written offer.

## GEOS binary import-name modification

The public build does not distribute copies of MSVC runtime DLLs,
`ucrtbase.dll`, `msvcrt.dll`, `api-ms-win-*`, or `ext-ms-win-*` DLLs. Windows 10/11
provides the system UCRT and API-set/extension-set support on the target computer.
This excludes file copies collected from dependency folders as well as copies
from Windows or Python directories. The original Windows Shapely wheel uses a
hash-suffixed Microsoft runtime DLL name for dependency isolation. The application publisher modified affected
GEOS PE import-name strings to request the normal system runtime filename
(`msvcp140.dll`), then updated the PE checksum. GEOS algorithm code was not
recompiled or changed.

The MIT-licensed `build_materials/packaging_support.py` is an exact copy of the
application's build helper. In `public_binaries`, `is_windows_runtime_copy()`
excludes the listed runtime file copies. Separately, `is_msvc_runtime()` identifies
hash-suffixed MSVC import names for normalization; API-set imports are not
normalized by this rule. The helper returns a JSON-serializable record with the
package-relative filename, original
SHA-256, modified SHA-256, old/new import names, and a modification notice. It
requires `pefile` (the reviewed build environment used 2024.8.26). Original wheel
and Python installation files are not edited in place; changed copies are written
to the build staging directory.

The application's PyInstaller specification writes those records to
`_internal/licenses/GEOS_IMPORT_MODIFICATIONS.json` in the final application
folder. That final build record, rather than a fabricated example checksum,
identifies the actual binary edits. The file was not available when these source
materials were first assembled, so the final release build must supply it and
check its hashes against the emitted binaries. It is not necessary to have a
record for a binary with no changed import.

Recipients can reproduce the transformation from the unmodified matching
Shapely wheel or rebuild GEOS 3.14.1 from the supplied source and CMake files.
Use compatible Windows x64/MSVC ABI settings, retain the filenames expected by
the Shapely extension, and ensure its imports use the Microsoft-installed system
runtime. The complete helper is supplied as the modification's preferred source
form; no additional patch to the GEOS algorithm source is claimed.

On a computer missing the runtime, install it from
https://aka.ms/vs/17/release/vc_redist.x64.exe or Microsoft's documented download
page. The installer and runtime license come from Microsoft; this source archive
and the public application ZIP do not include copies of those runtime DLLs.
Microsoft-authored ONNX Runtime binaries remain under their MIT license. Company
version resources and Authenticode publisher identities are provenance evidence,
not sufficient grounds for classifying every Microsoft binary as proprietary.

## Statically linked LGPL components

The bundled lxml extension contains a statically linked libiconv; OpenBLAS
contains statically linked GCC runtime material, including the libquadmath
component identified by NumPy's original notice. For these combinations the
source provision aims to use LGPL 2.1 section 6(a): complete source of the
library and the work that uses it, plus rebuilding materials, rather than
claiming that separate `.o` files were supplied. lxml sources contain generated C
code; OpenBLAS sources contain the code that combines with the GCC runtime.
This does not require applying the GPL to an application that merely loads a
separate, compatible combined DLL. The final public package excludes MouseInfo,
which has separate GPLv3+ terms and is not needed by the application's collection
functions.

The distributor permits replacement of these third-party native binaries and
reverse engineering to the extent needed to debug modifications to LGPL-covered
components. That permission does not extend the license of unrelated components.

## Rebuilding lxml with a modified libiconv

Use a separate development folder and a licensed Windows C/C++ toolchain. The
included source directories and MSVC project files are upstream materials; the
steps below were independently written from their documented interfaces.

1. Unpack the Windows source archives into directories named `libiconv`, `zlib`,
   `libxml2`, and `libxslt`. Unpack `lxml-6.1.3.tar.gz` separately. Preserve the
   copyright notices and describe any modifications you make.
2. Build the x64 Release static libiconv library using
   `libiconv/MSVC17/libiconv_static/libiconv_static.vcxproj`. Adjust the Windows SDK
   version in the project to the installed toolchain if needed. The x64 output
   supplies the static library and the headers in `libiconv/source/include`.
3. Build the zlib static library from its `win32/Makefile.msc`, then build static
   libxml2, libxslt and libexslt using their `win32/configure.js` and makefiles.
   Configure their include/library paths to your rebuilt libiconv and zlib.
   Supply those libraries and headers to lxml's documented static build.
4. For lxml, use the included `doc/build.txt`, `setup.py`, `setupinfo.py` and
   `buildlibxml.py`. `LXML_STATIC_INCLUDE_DIRS`, `LXML_STATIC_LIBRARY_DIRS`,
   `LXML_STATIC_CFLAGS` and `LXML_STATIC_BINARIES` provide explicit inputs for
   a static build. Do not silently substitute a prebuilt unmodified iconv.
   The included generated `etree.c` avoids needing to reproduce the original
   Cython generation step solely to relink a compatible extension.
5. Build for CPython 3.12, Windows x64; replace the corresponding
   `_internal/lxml/etree.cp312-win_amd64.pyd` with the rebuilt compatible extension.

The official external dependency build reference is
https://github.com/lxml/libxml2-win-binaries/tree/2026.05.17
Its four source submodule commits are listed above. The outer repository's own
`build.ps1` is linked as an upstream reference, not copied into this public source
archive: no explicit license for that separate outer repository was established.
The licensed source trees include their own build projects and scripts.

## Rebuilding OpenBLAS with a modified libquadmath

The licensed `MacPython/openblas-libs` source snapshot at
`1949e84a97655ed586a2cb4b469e98efec8651c0` records the matching OpenBLAS commit and
Windows recipes in `tools/build_openblas.sh` and `.github/workflows/build.yml`.
The full GCC 10.3.0 tree includes libquadmath/libgfortran source and their
configure/build files. Rebuild the changed GCC runtime, then rebuild OpenBLAS
against that runtime using the matching Windows x64 toolchain and ILP64 interface.
NumPy's corresponding build files specify the `64_` symbol suffix and provide its
import library and build configuration.

The build recipe also contains cleanup and upload steps for the upstream CI
environment. Review it in a disposable development directory; automatic execution
or upload is not part of this release. When using source archives rather than a
Git checkout, adapt the recipe's Git-only checkout/reset steps to the unpacked
source directories. Retain the actual compile/link commands and ABI settings.

A compatible rebuilt DLL can replace the OpenBLAS DLL in `_internal/numpy.libs`;
retain the existing DLL filename or rebuild the NumPy extensions/import libraries
for a deliberately changed filename. No lock or signature check restricts this
replacement.

## Verification limits

The source versions, original notices, source archive contents and relevant build
files were inspected. Exact PyPI archive hashes were verified. The sources were
not compiled again to reproduce the already published upstream Windows wheels,
and a modified-library replacement was not exercised on another computer.
Consequently this document does not claim bit-for-bit reproducibility or certify
that every upstream patch/toolchain input was identified. Compiler-runtime version
evidence and the Windows dependency release mapping are recorded so that any
remaining correspondence questions are explicit. A successful clean rebuild and
relink test would provide stronger verification of the section 6(a) mechanism.

MPL-covered certifi and tqdm source is supplied separately under its original
terms; no modifications to those sources were made by this application package.
Other package licenses and embedded third-party notices are retained in the
runtime license collection. No claim of legal advice or guaranteed noninfringement
is made.
