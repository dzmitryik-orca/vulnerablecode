# Copyright (c) nexB Inc. and others. All rights reserved.
# http://nexb.com and https://github.com/nexB/vulnerablecode/
# The VulnerableCode software is licensed under the Apache License version 2.0.
# Data generated with VulnerableCode require an acknowledgment.
#
# You may not use this software except in compliance with the License.
# You may obtain a copy of the License at: http://apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software distributed
# under the License is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR
# CONDITIONS OF ANY KIND, either express or implied. See the License for the
# specific language governing permissions and limitations under the License.
#
# When you publish or redistribute any data created with VulnerableCode or any VulnerableCode
# derivative work, you must accompany this data with the following acknowledgment:
#
#  Generated with VulnerableCode and provided on an "AS IS" BASIS, WITHOUT WARRANTIES
#  OR CONDITIONS OF ANY KIND, either express or implied. No content created from
#  VulnerableCode should be considered or used as legal advice. Consult an Attorney
#  for any legal advice.
#  VulnerableCode is a free software tool from nexB Inc. and others.
#  Visit https://github.com/nexB/vulnerablecode/ for support and download.

import asyncio
import logging
from typing import Iterable
from typing import NamedTuple

import requests
from bs4 import BeautifulSoup
from django.db.models.query import QuerySet
from packageurl import PackageURL
from univers.version_range import NginxVersionRange
from univers.versions import SemverVersion

from vulnerabilities.helpers import evolve_purl
from vulnerabilities.importer import AdvisoryData
from vulnerabilities.importer import AffectedPackage
from vulnerabilities.importer import Importer
from vulnerabilities.importer import Reference
from vulnerabilities.importer import UnMergeablePackageError
from vulnerabilities.importer import VulnerabilitySeverity
from vulnerabilities.improver import Improver
from vulnerabilities.improver import Inference
from vulnerabilities.models import Advisory
from vulnerabilities.package_managers import GitHubTagsAPI
from vulnerabilities.package_managers import Version
from vulnerabilities.severity_systems import GENERIC

logger = logging.getLogger(__name__)


class NginxImporter(Importer):

    url = "https://nginx.org/en/security_advisories.html"

    # TODO: Populate this properly
    spdx_license_expression = "TODO"

    def advisory_data(self) -> Iterable[AdvisoryData]:
        text = requests.get(self.url).content
        yield from advisory_data_from_text(text)


def advisory_data_from_text(text):
    """
    Yield AdvisoryData from the ``text`` of the nginx security advisories HTML
    web page.
    """
    soup = BeautifulSoup(text, features="lxml")
    vuln_list = soup.select("li p")
    for vuln_info in vuln_list:
        ngnix_adv = parse_advisory_data_from_paragraph(vuln_info)
        yield to_advisory_data(ngnix_adv)


class NginxAdvisory(NamedTuple):
    aliases: list
    summary: str
    advisory_severity: str
    not_vulnerable: str
    vulnerable: str
    references: list

    def to_dict(self):
        return self._asdict()


def to_advisory_data(ngnx_adv: NginxAdvisory) -> AdvisoryData:
    """
    Return AdvisoryData from an NginxAdvisory tuple.
    """
    package_name = "nginx"
    package_type = "nginx"
    qualifiers = {}

    _, _, affected_version_range = ngnx_adv.vulnerable.partition(":")
    if "nginx/Windows" in affected_version_range:
        qualifiers["os"] = "windows"
        affected_version_range = affected_version_range.replace("nginx/Windows", "")

    purl = PackageURL(type=package_type, name=package_name, qualifiers=qualifiers)

    affected_version_range = NginxVersionRange.from_native(affected_version_range)

    affected_packages = []
    _, _, fixed_versions = ngnx_adv.not_vulnerable.partition(":")

    for fixed_version in fixed_versions.split(","):
        fixed_version = fixed_version.rstrip("+")

        # TODO: Mail nginx for this anomaly (create ticket on our side)
        if "none" in fixed_version:
            affected_packages.append(
                AffectedPackage(
                    package=purl,
                    affected_version_range=affected_version_range,
                )
            )
            break

        fixed_version = SemverVersion(fixed_version)
        affected_packages.append(
            AffectedPackage(
                package=purl,
                affected_version_range=affected_version_range,
                fixed_version=fixed_version,
            )
        )

    return AdvisoryData(
        aliases=ngnx_adv.aliases,
        summary=ngnx_adv.summary,
        affected_packages=affected_packages,
        references=ngnx_adv.references,
    )


def parse_advisory_data_from_paragraph(vuln_info):
    """
    Return an NginxAdvisory from a ``vuln_info`` bs4 paragraph.

    An advisory paragraph, without html markup, looks like this:

        1-byte memory overwrite in resolver
        Severity: medium
        Advisory
        CVE-2021-23017
        Not vulnerable: 1.21.0+, 1.20.1+
        Vulnerable: 0.6.18-1.20.0
        The patch  pgp

    """
    aliases = []
    summary = None
    advisory_severity = None
    not_vulnerable = None
    vulnerable = None
    references = []
    is_first = True

    # we iterate on the children to accumulate values in variables
    # FIXME: using an explicit xpath-like query could be simpler
    for child in vuln_info.children:
        if is_first:
            summary = child
            is_first = False
            continue

        text = child.text.strip()
        text_low = text.lower()

        if text.startswith(
            (
                "CVE-",
                "CORE-",
                "VU#",
            )
        ):
            aliases.append(text)
            if text.startswith("CVE-"):

                # always keep the CVE as a reference too
                link = f"https://nvd.nist.gov/vuln/detail/{text}"
                reference = Reference(reference_id=text, url=link)
                references.append(reference)

        elif "severity" in text_low:
            advisory_severity = build_severity(severity=text)

        elif "not vulnerable" in text_low:
            not_vulnerable = text

        elif "vulnerable" in text_low:
            vulnerable = text

        elif hasattr(child, "attrs"):
            link = child.attrs.get("href")
            if link:
                if "cve.mitre.org" in link:
                    references.append(Reference(reference_id=text, url=link))
                elif "mailman.nginx.org" in link:
                    if advisory_severity:
                        severities = [advisory_severity]
                    else:
                        severities = []
                    references.append(Reference(url=link, severities=severities))
                else:
                    link = requests.compat.urljoin("https://nginx.org", link)
                    references.append(Reference(url=link))

    return NginxAdvisory(
        aliases=aliases,
        summary=summary,
        advisory_severity=advisory_severity,
        not_vulnerable=not_vulnerable,
        vulnerable=vulnerable,
        references=references,
    )


def build_severity(severity):
    """
    Return a VulnerabilitySeverity built from a ``severity`` string, or None.

    For example::
    >>> severity = "Severity: medium"
    >>> expected = VulnerabilitySeverity(system=GENERIC, value="medium")
    >>> assert build_severity(severity) == expected
    """
    if severity.startswith("Severity:"):
        _, _, severity = severity.partition("Severity:")

    severity = severity.strip()
    if severity:
        return VulnerabilitySeverity(system=GENERIC, value=severity)


class NginxBasicImprover(Improver):
    def __init__(self):
        self.set_api()

    @property
    def interesting_advisories(self) -> QuerySet:
        return Advisory.objects.filter(created_by=NginxImporter.qualified_name)

    def get_inferences(self, advisory_data: AdvisoryData) -> Iterable[Inference]:
        """
        Yield Inference from the ``advisory data`` AdvisoryData.
        """
        try:
            purl, affected_version_ranges, fixed_versions = AffectedPackage.merge(
                advisory_data.affected_packages
            )
        except UnMergeablePackageError:
            logger.error(f"Cannot merge with different purls {advisory_data.affected_packages!r}")
            return iter([])

        all_versions = self.version_api.get("nginx/nginx").valid_versions
        affected_purls = []
        for affected_version_range in affected_version_ranges:
            for version in all_versions:
                version = SemverVersion(version)
                if is_vulnerable(
                    version=version,
                    affected_version_range=affected_version_range,
                    fixed_versions=fixed_versions,
                ):
                    new_purl = evolve_purl(purl=purl, version=version)
                    affected_purls.append(new_purl)

        for fixed_version in fixed_versions:
            # TODO: This also yields with a lower fixed version, maybe we should
            # only yield fixes that are upgrades ?
            fixed_purl = evolve_purl(purl=purl, version=fixed_version)
            yield Inference.from_advisory_data(
                advisory_data,
                confidence=90,  # TODO: Decide properly
                affected_purls=affected_purls,
                fixed_purl=fixed_purl,
            )

    def set_api(self):
        self.version_api = GitHubTagsAPI()
        asyncio.run(self.version_api.load_api(["nginx/nginx"]))

        normalized_versions = set()
        while self.version_api.cache["nginx/nginx"]:
            version = self.version_api.cache["nginx/nginx"].pop()
            cleaned = clean_nginx_git_tag(version.value)
            normalized_version = Version(value=cleaned, release_date=version.release_date)
            normalized_versions.add(normalized_version)
        self.version_api.cache["nginx/nginx"] = normalized_versions


def clean_nginx_git_tag(tag):
    """
    Return a cleaned ``version`` string from an nginx git tag.

    Nginx tags git release as in `release-1.2.3`
    This removes the the `release-` prefix.

    For example:
    >>> clean_nginx_git_tag("release-1.2.3") == "1.2.3"
    True
    >>> clean_nginx_git_tag("1.2.3") == "1.2.3"
    True
    """
    if tag.startswith("release-"):
        _, _, tag = tag.partition("release-")
    return tag


def is_vulnerable(version, affected_version_range, fixed_versions):
    """
    Return True if the ``version`` Version for nginx is vulnerable according to
    the nginx approach.

    A ``version`` is vulnerable as explained by @mdounin
    in https://marc.info/?l=nginx&m=164070162912710&w=2 :

        "Note that it is generally trivial to find out if a version is
        vulnerable or not from the information about a vulnerability,
        without any knowledge about nginx branches.  That is:

        - Check if the version is in "Vulnerable" range.  If it's not, the
          version is not vulnerable.

        - If it is, check if the branch is explicitly listed in the "Not
          vulnerable".  If it's not, the version is vulnerable.  If it
          is, check the minor number: if it's greater or equal to the
          version listed as not vulnerable, the version is not vulnerable,
          else the version is vulnerable."

    """
    if version in NginxVersionRange.from_string(affected_version_range.to_string()):
        for fixed_version in fixed_versions:
            if version.value.minor == fixed_version.value.minor and version >= fixed_version:
                return False
        return True
    return False
