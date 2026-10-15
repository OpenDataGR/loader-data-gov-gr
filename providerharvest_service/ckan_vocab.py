"""The controlled vocabularies of data.gov.gr's *data service* entries (a CKAN dataset of type ``data-service``): access rights, licences, formats,
themes, high-value-dataset categories, applicable legislation and publisher types. Their values are URIs of the EU's own vocabularies; people see
the short labels and the form sends the URI. Every code here was looked up in the EU vocabulary itself (or is one the portal already uses); a value
that is not listed can still be given as a full URI where the field allows it.
"""

from __future__ import annotations

_AUTHORITY = "http://publications.europa.eu/resource/authority/"
_BNA = "http://data.europa.eu/bna/"
_ADMS = "http://purl.org/adms/publishertype/"

ACCESS_RIGHTS = [("PUBLIC", "Public"), ("RESTRICTED", "Restricted"), ("NON_PUBLIC", "Non-public")]
LICENCES = [("CC_BY_4_0", "CC BY 4.0"), ("CC0", "CC0 (public domain)"), ("CC_BYSA_4_0", "CC BY-SA 4.0"), ("CC_BYNC_4_0", "CC BY-NC 4.0"),
            ("CC_BYND_4_0", "CC BY-ND 4.0"), ("ODC_BY", "ODC-By"), ("ODC_ODBL", "ODbL")]
FORMATS = [("JSON", "JSON"), ("XML", "XML"), ("CSV", "CSV"), ("REST", "REST API")]
THEMES = [("AGRI", "Agriculture, fisheries, forestry and food"), ("ECON", "Economy and finance"), ("EDUC", "Education, culture and sport"), ("ENER", "Energy"),
          ("ENVI", "Environment"), ("GOVE", "Government and public sector"), ("HEAL", "Health"), ("INTR", "International issues"),
          ("JUST", "Justice, legal system and public safety"), ("REGI", "Regions and cities"), ("SOCI", "Population and society"),
          ("TECH", "Science and technology"), ("TRAN", "Transport")]
HVD_CATEGORIES = [("c_ac64a52d", "Geospatial"), ("c_dd313021", "Earth observation and environment"), ("c_164e0bf5", "Meteorological"),
                  ("c_a9135398", "Companies and company ownership"), ("c_b79e35eb", "Mobility")]
LEGISLATION = [("https://eur-lex.europa.eu/eli/dir/2019/1024/oj/eng", "Open Data Directive (EU) 2019/1024"),
               ("http://data.europa.eu/eli/reg_impl/2023/138/oj", "High-value datasets regulation (EU) 2023/138")]
PUBLISHER_TYPES = [("NationalAuthority", "National authority"), ("RegionalAuthority", "Regional authority"), ("LocalAuthority", "Local authority"),
                   ("SupraNationalAuthority", "Supra-national authority"), ("Academia-ScientificOrganisation", "Academia / scientific organisation"),
                   ("Company", "Company"), ("NonProfitOrganisation", "Non-profit organisation"), ("NonGovernmentalOrganisation", "Non-governmental organisation")]

_KINDS = {
    "access_rights": (_AUTHORITY + "access-right/", ACCESS_RIGHTS),
    "license": (_AUTHORITY + "licence/", LICENCES),
    "format": (_AUTHORITY + "file-type/", FORMATS),
    "theme": (_AUTHORITY + "data-theme/", THEMES),
    "hvd_category": (_BNA, HVD_CATEGORIES),
    "publisher_type": (_ADMS, PUBLISHER_TYPES),
}


def codes(kind: str) -> list[str]:
    return [c for c, _ in _KINDS[kind][1]]


def uri(kind: str, value: str) -> str:
    """The URI of a code (``CC_BY_4_0`` -> the EU licence URI); a value that is already a URI is kept as it is (for a licence the portal also
    has ones outside the EU list)."""
    value = (value or "").strip()
    if value.startswith("http://") or value.startswith("https://"):
        return value
    return _KINDS[kind][0] + value


def code_of(kind: str, value: str) -> str:
    """The short code of a URI the other way round (a URI outside the list stays a URI)."""
    value = (value or "").strip()
    base = _KINDS[kind][0]
    return value[len(base):] if value.startswith(base) else value


def is_known(kind: str, value: str) -> bool:
    return (value or "").strip() in codes(kind)
