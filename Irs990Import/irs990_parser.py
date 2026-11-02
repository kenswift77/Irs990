"""
irs990_parser.py
----------------
Parse IRS 990-series XML files into structured Python dicts.
Handles 990, 990-EZ, and 990-PF form variants.

The IRS XML uses a namespace like:
  xmlns="http://www.irs.gov/efile"

All XPath calls must use the {ns} prefix for the efile namespace.
"""

from __future__ import annotations
from decimal import Decimal, InvalidOperation
from typing import Optional
import xml.etree.ElementTree as ET


NS = "http://www.irs.gov/efile"
_NS = f"{{{NS}}}"

# ---------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------

def _text(el: Optional[ET.Element], *path_parts: str) -> Optional[str]:
    """Walk a path from el and return stripped text, or None."""
    node = el
    for part in path_parts:
        if node is None:
            return None
        node = node.find(f"{_NS}{part}")
    if node is None or node.text is None:
        return None
    return node.text.strip() or None


def _decimal(el: Optional[ET.Element], *path_parts: str) -> Optional[Decimal]:
    raw = _text(el, *path_parts)
    if raw is None:
        return None
    try:
        return Decimal(raw.replace(",", ""))
    except InvalidOperation:
        return None


def _int(el: Optional[ET.Element], *path_parts: str) -> Optional[int]:
    raw = _text(el, *path_parts)
    if raw is None:
        return None
    try:
        return int(raw.replace(",", ""))
    except ValueError:
        return None


def _bool_yn(el: Optional[ET.Element], *path_parts: str) -> Optional[bool]:
    raw = _text(el, *path_parts)
    if raw is None:
        return None
    return raw.upper() in ("TRUE", "1", "YES", "X")


# ---------------------------------------------------------------
# Header (common to all return types)
# ---------------------------------------------------------------

def parse_header(root: ET.Element) -> dict:
    hdr = root.find(f"{_NS}ReturnHeader")
    if hdr is None:
        return {}

    filer = hdr.find(f"{_NS}Filer")
    biz   = filer.find(f"{_NS}BusinessName") if filer is not None else None
    addr  = filer.find(f"{_NS}USAddress") if filer is not None else None
    if addr is None and filer is not None:
        addr = filer.find(f"{_NS}ForeignAddress")

    result = {
        "return_type":           _text(hdr, "ReturnTypeCd"),
        "tax_year":              _int(hdr,  "TaxYr"),
        "tax_period_begin_date": _text(hdr, "TaxPeriodBeginDt"),
        "tax_period_end_date":   _text(hdr, "TaxPeriodEndDt"),
        "submission_date":       _text(hdr, "ReturnTs"),   # ISO timestamp
        "form_version":          root.attrib.get("returnVersion"),
        "ein": _text(filer, "EIN") if filer is not None else None,
        "organization_name": (
            _text(biz, "BusinessNameLine1Txt") if biz is not None else None
        ),
        "dba_name": (
            _text(biz, "BusinessNameLine2Txt") if biz is not None else None
        ),
        "org_address_line1": _text(addr, "AddressLine1Txt") if addr is not None else None,
        "org_city":          _text(addr, "CityNm")          if addr is not None else None,
        "org_state":         _text(addr, "StateAbbreviationCd") if addr is not None else None,
        "org_zip":           _text(addr, "ZIPCd")           if addr is not None else None,
        "org_country":       _text(addr, "CountryCd")       if addr is not None else "US",
    }
    return result


# ---------------------------------------------------------------
# 990 (full form)
# ---------------------------------------------------------------

def _parse_990(data_el: ET.Element) -> dict:
    f990 = data_el.find(f"{_NS}IRS990")
    if f990 is None:
        return {}

    # Principal officer
    principal = f990.find(f"{_NS}BusinessOfficerGrp")

    # Activities & Governance section
    result = {
        "exempt_status_code":       _text(f990, "Desc501c3Org"),
        "gross_receipts":           _decimal(f990, "GrossReceiptsAmt"),
        "total_revenue":            _decimal(f990, "TotalRevenueAmt"),
        "total_expenses":           _decimal(f990, "TotalFunctionalExpensesAmt"),
        "total_functional_expenses":_decimal(f990, "TotalFunctionalExpensesAmt"),
        "net_assets_eoy":           _decimal(f990, "NetAssetsOrFundBalancesEOYAmt"),
        "total_assets_eoy":         _decimal(f990, "TotalAssetsEOYAmt"),
        "total_liabilities_eoy":    _decimal(f990, "TotalLiabilitiesEOYAmt"),
        "contributions_grants":     _decimal(f990, "CYContributionsGrantsAmt"),
        "program_service_revenue":  _decimal(f990, "CYProgramServiceRevenueAmt"),
        "investment_income":        _decimal(f990, "CYInvestmentIncomeAmt"),
        "employee_count":           _int(f990,     "TotalEmployeeCnt"),
        "volunteer_count":          _int(f990,     "TotalVolunteersCnt"),
        "mission_description":      _text(f990, "ActivityOrMissionDesc"),
        "program_service_desc":     _text(f990, "Desc"),
        "principal_officer_name":   _text(principal, "PersonNm") if principal else None,
        "principal_officer_title":  _text(principal, "TitleTxt") if principal else None,
        "group_exemption_num":      _text(f990, "GroupExemptionNum"),
    }

    # Officers (Part VII)
    officers = []
    for ofcr_grp in data_el.findall(f".//{_NS}OfficerDirectorTrusteeKeyEmployee"):
        person_name = (
            _text(ofcr_grp, "PersonNm") or
            _text(ofcr_grp.find(f"{_NS}BusinessName"), "BusinessNameLine1Txt")
            if ofcr_grp.find(f"{_NS}BusinessName") is not None else None
        )
        comp_grp = ofcr_grp.find(f"{_NS}IndividualTrusteeOrDirectorInd")
        officers.append({
            "person_name":             person_name,
            "title":                   _text(ofcr_grp, "TitleTxt"),
            "average_hours_per_week":  _decimal(ofcr_grp, "AverageHrsPerWkDevotedToPosRt"),
            "reportable_comp_org":     _decimal(ofcr_grp, "ReportableCompFromOrgAmt"),
            "reportable_comp_related": _decimal(ofcr_grp, "ReportableCompFromRltdOrgAmt"),
            "other_comp":              _decimal(ofcr_grp, "OtherCompensationAmt"),
            "is_officer":              _bool_yn(ofcr_grp, "OfficerInd"),
            "is_director":             _bool_yn(ofcr_grp, "IndividualTrusteeOrDirectorInd"),
            "is_trustee":              False,
            "is_key_employee":         _bool_yn(ofcr_grp, "KeyEmployeeInd"),
            "is_highest_compensated":  _bool_yn(ofcr_grp, "HighestCompensatedEmployeeInd"),
            "is_former":               _bool_yn(ofcr_grp, "FormerOfcrDirectorTrusteeInd"),
        })

    result["officers"] = officers

    # Which schedules are attached?
    schedules = []
    sched_map = {
        "IRS990ScheduleA": "A", "IRS990ScheduleB": "B",
        "IRS990ScheduleC": "C", "IRS990ScheduleD": "D",
        "IRS990ScheduleE": "E", "IRS990ScheduleF": "F",
        "IRS990ScheduleG": "G", "IRS990ScheduleH": "H",
        "IRS990ScheduleI": "I", "IRS990ScheduleJ": "J",
        "IRS990ScheduleK": "K", "IRS990ScheduleL": "L",
        "IRS990ScheduleM": "M", "IRS990ScheduleN": "N",
        "IRS990ScheduleO": "O", "IRS990ScheduleR": "R",
    }
    for tag, code in sched_map.items():
        if data_el.find(f"{_NS}{tag}") is not None:
            schedules.append(code)
    result["schedules"] = schedules

    return result


# ---------------------------------------------------------------
# 990-EZ
# ---------------------------------------------------------------

def _parse_990ez(data_el: ET.Element) -> dict:
    ez = data_el.find(f"{_NS}IRS990EZ")
    if ez is None:
        return {}
    return {
        "gross_receipts":        _decimal(ez, "GrossReceiptsAmt"),
        "total_revenue":         _decimal(ez, "TotalRevenueAmt"),
        "total_expenses":        _decimal(ez, "TotalExpensesAmt"),
        "net_assets_eoy":        _decimal(ez, "NetAssetsOrFundBalancesEOYAmt"),
        "total_assets_eoy":      _decimal(ez, "TotalAssetsEOYAmt"),
        "total_liabilities_eoy": _decimal(ez, "TotalLiabilitiesEOYAmt"),
        "contributions_grants":  _decimal(ez, "ContributionsGiftsGrantsEtcAmt"),
        "program_service_revenue": _decimal(ez, "ProgramServiceRevenueAmt"),
        "mission_description":   _text(ez, "ActivityOrMissionDesc"),
        "principal_officer_name": _text(ez, "NameOfPrincipalOfficerPersonNm"),
        "officers": [],
        "schedules": [],
    }


# ---------------------------------------------------------------
# 990-PF
# ---------------------------------------------------------------

def _parse_990pf(data_el: ET.Element) -> dict:
    pf = data_el.find(f"{_NS}IRS990PF")
    if pf is None:
        return {}
    return {
        "gross_receipts":        _decimal(pf, "TotalGrossInvestIncomeAmt"),
        "total_revenue":         _decimal(pf, "TotalRevAndExpnssAmt"),
        "total_expenses":        _decimal(pf, "TotalExpensesRevAndExpnssAmt"),
        "net_assets_eoy":        _decimal(pf, "FairMktValueAsstEndOfYrAmt"),
        "total_assets_eoy":      _decimal(pf, "TotalAssetsEOYAmt"),
        "total_liabilities_eoy": _decimal(pf, "TotalLiabilitiesEOYAmt"),
        "contributions_grants":  _decimal(pf, "ContriRcvdRevAndExpnssAmt"),
        "mission_description":   _text(pf, "PurposeOfGrantOrContributionTxt"),
        "officers": [],
        "schedules": [],
    }


# ---------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------

def parse_990_xml(xml_bytes: bytes, file_name: str = "") -> dict:
    """
    Parse a raw 990-series XML file.

    Returns a dict with:
      - All header fields
      - Form-specific financial fields
      - 'officers' list of dicts
      - 'schedules' list of schedule codes present
      - 'raw_xml' — the original bytes (for storing in DB)
      - 'xml_file_name' — the source file name
    """
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        return {"_error": f"XML parse error: {exc}", "xml_file_name": file_name}

    # Strip the namespace prefix from the root tag check
    local_tag = root.tag.replace(f"{_NS}", "")
    if local_tag != "Return":
        return {"_error": f"Unexpected root element: {root.tag}", "xml_file_name": file_name}

    result = parse_header(root)
    result["xml_file_name"] = file_name
    result["raw_xml"] = xml_bytes.decode("utf-8", errors="replace")

    data_el = root.find(f"{_NS}ReturnData")
    if data_el is not None:
        return_type = result.get("return_type", "")
        if return_type in ("990", "990O"):
            result.update(_parse_990(data_el))
        elif return_type == "990EZ":
            result.update(_parse_990ez(data_el))
        elif return_type == "990PF":
            result.update(_parse_990pf(data_el))
        else:
            # 990T, 990N, etc. — header only
            result.setdefault("officers", [])
            result.setdefault("schedules", [])

    return result
