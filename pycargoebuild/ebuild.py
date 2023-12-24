import datetime
import logging
import re
import shlex
import tarfile
import typing
import urllib.parse

from functools import partial
from pathlib import Path

import license_expression

from pycargoebuild import __version__
from pycargoebuild.cargo import (Crate,
                                 FileCrate,
                                 GitCrate,
                                 PackageMetadata,
                                 get_package_metadata,
                                 )
from pycargoebuild.format import format_license_var
from pycargoebuild.license import spdx_to_ebuild


EBUILD_TEMPLATE_START = """\
# Copyright {year} Gentoo Authors
# Distributed under the terms of the GNU General Public License v2

# Autogenerated by pycargoebuild {prog_version}

EAPI=8

CRATES="{crates}"{opt_git_crates}

inherit cargo

DESCRIPTION="{description}"
HOMEPAGE="{homepage}"
SRC_URI="
\t${{CARGO_CRATE_URIS}}
"{opt_crate_tarball}

LICENSE="{pkg_license}"
"""

EBUILD_TEMPLATE_CRATE_TARBALL = """
if [[ ${{PKGBUMPING}} != ${{PVR}} ]]; then
\tSRC_URI+="
\t\t{}
\t"
fi\
"""

EBUILD_TEMPLATE_CRATE_LICENSE = """\
# Dependent crate licenses
LICENSE+="{crate_licenses}"
"""

EBUILD_TEMPLATE_END = """\
SLOT="0"
KEYWORDS="~amd64"
"""


def get_CRATES(crates: typing.Iterable[Crate],
               ) -> str:
    """
    Return the value of CRATES for the given crate list
    """
    if not crates:
        # cargo.eclass rejects empty crates, we need some whitespace
        return "\n"
    return ("\n" +
            "\n".join(sorted(f"\t{c.crate_entry}"
                             for c in crates
                             if isinstance(c, FileCrate))) +
            "\n")


def get_GIT_CRATES(crates: typing.Iterable[Crate],
                   distdir: Path,
                   ) -> str:
    """
    Return the complete GIT_CRATES assignment for the given crate list
    """

    values = "\n".join(
        sorted(f"\t[{c.name}]={shlex.quote(c.get_git_crate_entry(distdir))}"
               for c in crates if isinstance(c, GitCrate)))
    if values:
        return f"\n\ndeclare -A GIT_CRATES=(\n{values}\n)"
    return ""


def get_package_LICENSE(license_str: typing.Optional[str]) -> str:
    """
    Get the value of package's LICENSE string
    """

    spdx = license_expression.get_spdx_licensing()
    if license_str is not None:
        parsed_pkg_license = spdx.parse(license_str, strict=True).simplify()
        return format_license_var(spdx_to_ebuild(parsed_pkg_license),
                                  prefix='LICENSE="')
    return ""


def get_license_from_crate(crate: Crate,
                           distdir: Path,
                           ) -> typing.Optional[str]:
    """
    Read the metadata from specified crate and return its license string
    """

    filename = crate.filename
    base_dir = crate.get_package_directory(distdir)
    workspace_toml = crate.get_workspace_toml(distdir)
    with tarfile.open(distdir / filename, "r:gz") as crate_tar:
        tarf = crate_tar.extractfile(str(base_dir / "Cargo.toml"))
        if tarf is None:
            raise RuntimeError(
                f"{base_dir}/Cargo.toml not found in {filename}")
        with tarf:
            # tarfile.ExFileObject() is IO[bytes] while tomli/tomllib
            # expects BinaryIO -- but it actually is compatible
            # https://github.com/hukkin/tomli/issues/214
            crate_metadata = get_package_metadata(
                tarf, workspace_toml)  # type: ignore
            if crate_metadata.license_file is not None:
                logging.warning(
                    f"Crate {filename} (in {base_dir}) uses license-file="
                    f"{crate_metadata.license_file!r}, please inspect "
                    "the license manually and add it *separately* from crate "
                    "licenses")
            elif crate_metadata.license is None:
                logging.warning(
                    f"Crate {filename} (in {base_dir}, "
                    f"name={crate_metadata.name!r}) does not specify "
                    "a license!")
            return crate_metadata.license


def get_crate_LICENSE(crates: typing.Iterable[Crate],
                      distdir: Path,
                      license_overrides: typing.Dict[str, str] = {},
                      ) -> str:
    """
    Get the value of LICENSE string for crates
    """

    spdx = license_expression.get_spdx_licensing()
    crate_licenses = set(get_license_from_crate(crate, distdir)
                         if crate.name not in license_overrides
                         else license_overrides[crate.name]
                         for crate in crates)
    crate_licenses.discard(None)

    # combine crate licenses and simplify the result
    combined_license = " AND ".join(f"( {x} )" for x in crate_licenses)
    parsed_license = spdx.parse(combined_license, strict=True)
    if parsed_license is None:
        return ""
    final_license = parsed_license.simplify()
    crate_licenses_str = format_license_var(spdx_to_ebuild(final_license),
                                            prefix='LICENSE+=" ')
    # if it's not a multiline string, we need to prepend " "
    if not crate_licenses_str.startswith("\n"):
        crate_licenses_str = " " + crate_licenses_str
    return crate_licenses_str


DQUOTE_SPECIAL_RE = re.compile(r'([$`"\\])')
DQUOTE_SPECIAL_PLUS_WS_RE = re.compile(r'[$`"\\\s]')


def collapse_whitespace(value: str) -> str:
    """Collapse sequences of whitespace into a single space"""
    return " ".join(value.split())


def bash_dquote_escape(value: str) -> str:
    """Escape all characters with special meaning in bash double-quotes"""
    return DQUOTE_SPECIAL_RE.sub(r"\\\1", value)


def url_dquote_escape(value: str) -> str:
    """URL-encode whitespace and special chars to use in bash double-quotes"""
    return DQUOTE_SPECIAL_PLUS_WS_RE.sub(
        lambda x: urllib.parse.quote_plus(x.group(0)), value)


def get_ebuild(pkg_meta: PackageMetadata,
               crates: typing.Iterable[Crate],
               distdir: Path,
               *,
               crate_license: bool = True,
               crate_tarball: typing.Optional[Path] = None,
               license_overrides: typing.Dict[str, str] = {},
               ) -> str:
    """
    Get ebuild contents for passed contents of Cargo.toml and Cargo.lock.
    """

    template = EBUILD_TEMPLATE_START
    if crate_license:
        template += EBUILD_TEMPLATE_CRATE_LICENSE
    template += EBUILD_TEMPLATE_END

    return template.format(
        crates=get_CRATES(crates if crate_tarball is None else ()),
        crate_licenses=get_crate_LICENSE(crates, distdir, license_overrides),
        description=bash_dquote_escape(collapse_whitespace(
            pkg_meta.description or "")),
        homepage=url_dquote_escape(pkg_meta.homepage or ""),
        opt_crate_tarball=EBUILD_TEMPLATE_CRATE_TARBALL.format(
            crate_tarball.name) if crate_tarball is not None else "",
        opt_git_crates=get_GIT_CRATES(crates, distdir),
        pkg_license=get_package_LICENSE(pkg_meta.license),
        prog_version=__version__,
        year=datetime.date.today().year)


CRATES_RE = re.compile(
    r"^(?P<start>CRATES=(?P<delim>['\"])).*?(?P=delim)$",
    re.DOTALL | re.MULTILINE)

GIT_CRATES_RE = re.compile(
    r"(?P<ws>\n\n?)declare -A GIT_CRATES=[(].*?[)]$",
    re.DOTALL | re.MULTILINE)

CRATE_LICENSE_RE = re.compile(
    r"^(?P<start># Dependent crate licenses\n"
    r"LICENSE[+]=(?P<delim>['\"])).*?(?P=delim)$",
    re.DOTALL | re.MULTILINE)

GIT_CRATES_APPEND_RE = re.compile(
    r"^(?P<start>CRATES=(?P<delim2>['\"]).*?(?P=delim2))(?P<delim>)$",
    re.DOTALL | re.MULTILINE)


class CountingSubst:
    def __init__(self, repl: typing.Callable[[], str]
                 ) -> None:
        self.count = 0
        self.repl = repl

    def __call__(self, match: re.Match) -> str:
        self.count += 1
        return match.group("start") + self.repl() + match.group("delim")

    def assert_count(self, desc: str, expected: int) -> None:
        if self.count != expected:
            raise RuntimeError(
                f"{desc} matched {self.count} times, {expected} expected")


class GitCratesSubst(CountingSubst):
    def __call__(self, match: re.Match) -> str:
        self.count += 1
        if repl := self.repl().lstrip():
            return match.group("ws") + repl
        return ""


def update_ebuild(ebuild: str,
                  pkg_meta: PackageMetadata,
                  crates: typing.Iterable[Crate],
                  distdir: Path,
                  *,
                  crate_license: bool = True,
                  crate_tarball: typing.Optional[Path] = None,
                  license_overrides: typing.Dict[str, str] = {},
                  ) -> str:
    """
    Update the CRATES, GIT_CRATES and LICENSE in an existing ebuild
    """

    crates_repl = CountingSubst(
        partial(get_CRATES, crates if crate_tarball is None else ()))
    git_crates_repl = GitCratesSubst(partial(get_GIT_CRATES, crates, distdir))
    crate_license_repl = (
        CountingSubst(partial(get_crate_LICENSE, crates, distdir,
                              license_overrides)))

    for regex, repl in ((CRATES_RE, crates_repl),
                        (GIT_CRATES_RE, git_crates_repl),
                        (CRATE_LICENSE_RE, crate_license_repl)):
        ebuild = regex.sub(repl, ebuild)

    crates_repl.assert_count("CRATES=", 1)
    crate_license_repl.assert_count(
        "Crate LICENSE+= (with marker comment)", 1 if crate_license else 0)

    if git_crates_repl.count == 0:
        if git_crates := git_crates_repl.repl():
            git_crates_append = CountingSubst(lambda: git_crates)
            ebuild = GIT_CRATES_APPEND_RE.sub(git_crates_append, ebuild)
            git_crates_append.assert_count(
                "CRATES= (while appending GIT_CRATES=)", 1)
    else:
        git_crates_repl.assert_count("GIT_CRATES=", 1)

    return ebuild
