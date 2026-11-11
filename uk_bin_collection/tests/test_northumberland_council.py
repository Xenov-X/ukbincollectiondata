"""Unit tests for NorthumberlandCouncil, which replays the council's form flow.

The site keeps the postcode and UPRN in a server side session and rotates a
CSRF token on every page, so the scraper makes four requests in order. These
tests pin that flow against canned pages, with no network and no Selenium.
"""

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from bs4 import BeautifulSoup

from uk_bin_collection.uk_bin_collection.councils import NorthumberlandCouncil

UPRN = "010096302588"

POSTCODE_PAGE = """
<form method="post"><input type="hidden" name="_csrf" value="token-one">
<input id="postcode" name="postcode"></form>
"""

ADDRESS_PAGE = f"""
<form method="post"><input type="hidden" name="_csrf" value="token-two">
<select id="address" name="address">
<option value="">Select an address</option>
<option value="{UPRN}">1 Example Road</option>
</select></form>
"""

NO_ADDRESS_PAGE = """
<form method="post"><input type="hidden" name="_csrf" value="token-two">
<p>We could not find that postcode</p></form>
"""

# The results page lists the next few dates, without a year.
RESULTS_PAGE = """
<table class="govuk-table"><tbody class="govuk-table__body">
<tr class="govuk-table__row"><th scope="row">22nd December</th>
<td>Tuesday</td><td>Recycling</td></tr>
<tr class="govuk-table__row"><th scope="row">5 January</th>
<td>Tuesday</td><td>General waste</td></tr>
</tbody></table>
"""

# The printable calendar has one table per month, with the year.
CALENDAR_PAGE = """
<table><thead><tr><th>Date</th><th>Day</th><th>Collection</th></tr></thead>
<tbody><tr><th scope="row">22 December 2026</th><td>Tuesday</td>
<td>Recycling</td></tr></tbody></table>
<table><tbody>
<tr><th scope="row">5 January 2027</th><td>Tuesday</td><td>General waste</td></tr>
<tr><th scope="row">12 January 2027</th><td>Tuesday</td><td>Recycling</td></tr>
</tbody></table>
"""

NO_COLLECTIONS_PAGE = """
<p>No upcoming bin collection days available for your address.</p>
"""


def page(text, status=200):
    response = MagicMock(text=text, status_code=status)
    if status >= 400:
        response.raise_for_status.side_effect = (
            NorthumberlandCouncil.requests.HTTPError(f"{status}")
        )
    return response


def run(gets, posts, uprn=UPRN):
    """Run parse_data against canned GET and POST responses, in order."""
    session = MagicMock()
    session.get.side_effect = gets
    session.post.side_effect = posts
    with (
        patch.object(NorthumberlandCouncil.requests, "Session", return_value=session),
        patch.object(NorthumberlandCouncil, "check_postcode"),
    ):
        data = NorthumberlandCouncil.CouncilClass().parse_data(
            "", postcode="NE65 0ZP", uprn=uprn
        )
    return data, session


def test_does_not_use_selenium():
    with open(NorthumberlandCouncil.__file__, encoding="utf-8") as source:
        assert "create_webdriver(" not in source.read()


def test_reads_the_full_calendar_with_years():
    data, session = run(
        gets=[page(POSTCODE_PAGE), page(CALENDAR_PAGE)],
        posts=[page(ADDRESS_PAGE), page(RESULTS_PAGE)],
    )
    assert data == {
        "bins": [
            {"type": "Recycling", "collectionDate": "22/12/2026"},
            {"type": "General waste", "collectionDate": "05/01/2027"},
            {"type": "Recycling", "collectionDate": "12/01/2027"},
        ]
    }


def test_each_post_carries_the_token_from_the_page_before_it():
    _, session = run(
        gets=[page(POSTCODE_PAGE), page(CALENDAR_PAGE)],
        posts=[page(ADDRESS_PAGE), page(RESULTS_PAGE)],
    )
    first, second = session.post.call_args_list
    assert first.args[0].endswith("/postcode")
    assert first.kwargs["data"] == {"_csrf": "token-one", "postcode": "NE65 0ZP"}
    assert second.args[0].endswith("/address-select")
    assert second.kwargs["data"] == {"_csrf": "token-two", "address": UPRN}


def test_short_uprn_is_padded_to_twelve_digits():
    _, session = run(
        gets=[page(POSTCODE_PAGE), page(CALENDAR_PAGE)],
        posts=[page(ADDRESS_PAGE), page(RESULTS_PAGE)],
        uprn="10096302588",
    )
    assert session.post.call_args_list[1].kwargs["data"]["address"] == UPRN


def test_falls_back_to_the_results_page_when_the_calendar_fails():
    data, _ = run(
        gets=[page(POSTCODE_PAGE), page("", status=500)],
        posts=[page(ADDRESS_PAGE), page(RESULTS_PAGE)],
    )
    assert [b["type"] for b in data["bins"]] == ["Recycling", "General waste"]
    assert all(b["collectionDate"][:5] in ("22/12", "05/01") for b in data["bins"])


def test_results_page_dates_roll_into_next_year():
    soup = BeautifulSoup(RESULTS_PAGE, features="html.parser")
    rows = NorthumberlandCouncil.CouncilClass.parse_rows(soup, datetime(2026, 12, 20))
    assert rows == [
        (datetime(2026, 12, 22), "Recycling"),
        (datetime(2027, 1, 5), "General waste"),
    ]


def test_postcode_outside_northumberland_is_a_clear_error():
    with pytest.raises(ValueError, match="No addresses found for postcode NE65 0ZP"):
        run(gets=[page(POSTCODE_PAGE)], posts=[page(NO_ADDRESS_PAGE)])


def test_uprn_not_at_the_postcode_is_a_clear_error():
    with pytest.raises(ValueError, match="UPRN 000000000001 is not offered"):
        run(gets=[page(POSTCODE_PAGE)], posts=[page(ADDRESS_PAGE)], uprn="1")


def test_address_with_no_rounds_is_a_clear_error():
    with pytest.raises(ValueError, match=f"no upcoming collections for UPRN {UPRN}"):
        run(
            gets=[page(POSTCODE_PAGE), page(NO_COLLECTIONS_PAGE)],
            posts=[page(ADDRESS_PAGE), page(NO_COLLECTIONS_PAGE)],
        )


def test_changed_layout_is_reported_as_such():
    with pytest.raises(ValueError, match="page layout may have changed"):
        run(
            gets=[page(POSTCODE_PAGE), page("<p>Welcome</p>")],
            posts=[page(ADDRESS_PAGE), page("<p>Welcome</p>")],
        )


def test_missing_csrf_token_is_reported():
    with pytest.raises(ValueError, match="No _csrf token found"):
        run(gets=[page("<form></form>")], posts=[])
