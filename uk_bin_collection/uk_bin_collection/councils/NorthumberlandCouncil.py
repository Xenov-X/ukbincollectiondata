import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from uk_bin_collection.uk_bin_collection.common import *
from uk_bin_collection.uk_bin_collection.get_bin_data import AbstractGetBinDataClass

BASE_URL = "https://bincollection.northumberland.gov.uk"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/138.0.0.0 Safari/537.36"
)
TIMEOUT = 30

# The council's wording when an address has no round data (e.g. new builds).
NO_COLLECTIONS_MARKER = "no upcoming bin collection days available"


class CouncilClass(AbstractGetBinDataClass):
    """
    Concrete classes have to implement all abstract operations of the
    base class. They can also override some operations with a default
    implementation.

    The council site has no API, but it does not need a browser either. State
    is held in a server side session and every form carries a rotating CSRF
    token, so a lookup replays this flow with plain HTTP requests:

      1. GET  /postcode        harvest the _csrf input and the session cookie
      2. POST /postcode        submit the postcode; the 302 lands on
                               /address-select, whose body carries the UPRN
                               list and a fresh _csrf
      3. POST /address-select  store the chosen UPRN in the session; returns
                               the results page (next few dates, no year)
      4. GET  /calendarPrint   several months of dates, with the year

    There is no shortcut: /calendarPrint?uprn=... redirects, because the UPRN
    lives in the session rather than the query string.
    """

    @staticmethod
    def get_csrf(soup: BeautifulSoup) -> str:
        """
        Extract the CSRF token from a page. The token rotates on every page
        render, so it must be re-read for each POST rather than cached.
        """
        token = soup.find("input", {"name": "_csrf"})
        if token is None or not token.get("value"):
            raise ValueError("No _csrf token found; the page layout may have changed")
        return token["value"]

    @staticmethod
    def post_form(session: requests.Session, path: str, form: dict) -> BeautifulSoup:
        """
        Submit a form and return the parsed body of the final response, after
        any redirects have been followed.
        """
        response = session.post(
            f"{BASE_URL}{path}",
            data=form,
            # The site sets form-action 'self' and checks the origin on POST.
            headers={"Origin": BASE_URL, "Referer": f"{BASE_URL}{path}"},
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        return BeautifulSoup(response.text, features="html.parser")

    @staticmethod
    def parse_rows(soup: BeautifulSoup, now: datetime) -> list:
        """
        Extract (date, bin type) pairs from every collection table on a page.

        Each data row is: <th>date</th><td>day name</td><td>bin type</td>.
        /calendarPrint dates carry the year ("2 September 2026"); the results
        page omits it ("2 September"), so the year is inferred there.
        """
        collections = []
        for row in soup.find_all("tr"):
            header = row.find("th")
            cells = row.find_all("td")
            if header is None or not cells:
                continue

            # The day is sometimes written "22nd" rather than "22".
            match = re.match(
                r"^(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)(?:\s+(\d{4}))?$",
                " ".join(header.get_text().split()),
            )
            if match is None:
                continue
            day, month_name, year = match.groups()

            try:
                month = datetime.strptime(month_name, "%B").month
                if year is None:
                    # No year given: a month well behind the current one must
                    # belong to next year.
                    year = now.year + 1 if month < now.month - 2 else now.year
                collection_date = datetime(int(year), month, int(day))
            except ValueError:
                continue

            bin_type = " ".join(cells[-1].get_text().split())
            if bin_type:
                collections.append((collection_date, bin_type))
        return collections

    def parse_data(self, page: str, **kwargs) -> dict:
        """
        Fetch bin collection dates from the Northumberland council lookup.

        Parameters:
            page (str): Ignored; the council lookup flow is replayed instead.
            **kwargs:
                postcode (str): UK postcode to query.
                uprn (str|int): Property UPRN; padded to 12 digits before use.

        Returns:
            dict: A dictionary with a "bins" key mapping to a list of entries,
            each with a "type" (e.g. "General waste", "Recycling",
            "Garden waste") and a "collectionDate" in the module's date_format.

        Raises:
            ValueError: if the postcode or UPRN is not recognised, the council
            has no upcoming collections for the address, or the page layout
            has changed.
        """
        user_postcode = kwargs.get("postcode")
        user_uprn = kwargs.get("uprn")

        check_postcode(user_postcode)
        check_uprn(user_uprn)
        user_uprn = str(user_uprn).zfill(12)

        session = requests.Session()
        session.headers.update(
            {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
        )

        # 1. GET /postcode for a CSRF token and a session cookie.
        response = session.get(f"{BASE_URL}/postcode", timeout=TIMEOUT)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, features="html.parser")

        # 2. POST /postcode. The redirect to /address-select is followed, so
        #    the body returned is already the address picker.
        soup = self.post_form(
            session,
            "/postcode",
            {"_csrf": self.get_csrf(soup), "postcode": user_postcode},
        )
        select = soup.find("select", {"id": "address"})
        if select is None:
            raise ValueError(
                f"No addresses found for postcode {user_postcode}; "
                "it may be outside Northumberland"
            )
        uprns = [option.get("value") for option in select.find_all("option")]
        if user_uprn not in uprns:
            raise ValueError(
                f"UPRN {user_uprn} is not offered for postcode {user_postcode}"
            )

        # 3. POST /address-select stores the UPRN in the session and returns
        #    the results page, which lists only the next few dates.
        results_soup = self.post_form(
            session,
            "/address-select",
            {"_csrf": self.get_csrf(soup), "address": user_uprn},
        )

        # 4. GET /calendarPrint for the full multi-month list. Fall back to
        #    the results page if it is unavailable.
        now = datetime.now()
        collections = []
        pages = [results_soup]
        try:
            response = session.get(f"{BASE_URL}/calendarPrint", timeout=TIMEOUT)
            response.raise_for_status()
            calendar_soup = BeautifulSoup(response.text, features="html.parser")
            pages.append(calendar_soup)
            collections = self.parse_rows(calendar_soup, now)
        except requests.RequestException:
            pass
        if not collections:
            collections = self.parse_rows(results_soup, now)

        if not collections:
            # The council has no round data for some addresses (new builds,
            # for example). Say so, rather than blaming the page layout.
            if any(
                NO_COLLECTIONS_MARKER in " ".join(p.get_text().split()).lower()
                for p in pages
            ):
                raise ValueError(
                    f"Northumberland has no upcoming collections for UPRN {user_uprn}"
                )
            raise ValueError(
                "No collection rows found; the page layout may have changed"
            )

        data = {"bins": []}
        for collection_date, bin_type in sorted(set(collections)):
            data["bins"].append(
                {
                    "type": bin_type,
                    "collectionDate": collection_date.strftime(date_format),
                }
            )
        return data
