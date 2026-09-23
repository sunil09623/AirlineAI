"""Static reference data used by the local NDC AirShopping engine.

Small, deterministic tables so the agent and the training-data generator behave
identically across runs and machines.
"""

from __future__ import annotations

# IATA passenger type codes understood by the local engine.
PASSENGER_TYPES: dict[str, str] = {
    "ADT": "Adult",
    "CHD": "Child",
    "INF": "Infant",
    "YTH": "Youth",
    "SRC": "Senior Citizen",
}

# Cabin codes -> IATA cabin names.
CABINS: dict[str, str] = {
    "Y": "Economy",
    "S": "Premium Economy",
    "C": "Business",
    "J": "Business",
    "F": "First",
}

# Airport -> (city, country, region). A regional subset is enough to exercise
# the shopping engine end-to-end.
AIRPORTS: dict[str, tuple[str, str, str]] = {
    "LHR": ("London", "GB", "Europe"),
    "LGW": ("London", "GB", "Europe"),
    "CDG": ("Paris", "FR", "Europe"),
    "AMS": ("Amsterdam", "NL", "Europe"),
    "FRA": ("Frankfurt", "DE", "Europe"),
    "MAD": ("Madrid", "ES", "Europe"),
    "FCO": ("Rome", "IT", "Europe"),
    "IST": ("Istanbul", "TR", "Europe"),
    "JFK": ("New York", "US", "North America"),
    "LAX": ("Los Angeles", "US", "North America"),
    "ORD": ("Chicago", "US", "North America"),
    "YYZ": ("Toronto", "CA", "North America"),
    "DXB": ("Dubai", "AE", "Middle East"),
    "DOH": ("Doha", "QA", "Middle East"),
    "SIN": ("Singapore", "SG", "Asia"),
    "HKG": ("Hong Kong", "HK", "Asia"),
    "NRT": ("Tokyo", "JP", "Asia"),
    "BOM": ("Mumbai", "IN", "Asia"),
    "DEL": ("Delhi", "IN", "Asia"),
    "SYD": ("Sydney", "AU", "Oceania"),
    "GRU": ("Sao Paulo", "BR", "South America"),
    "JNB": ("Johannesburg", "ZA", "Africa"),
}

# Carrier -> (marketing name, cabin list it sells).
CARRIERS: dict[str, tuple[str, tuple[str, ...]]] = {
    "AA": ("American Airlines", ("Y", "S", "C", "F")),
    "BA": ("British Airways", ("Y", "S", "C", "F")),
    "LH": ("Lufthansa", ("Y", "S", "C", "F")),
    "AF": ("Air France", ("Y", "S", "C")),
    "KL": ("KLM", ("Y", "S", "C")),
    "EK": ("Emirates", ("Y", "S", "C", "F")),
    "QR": ("Qatar Airways", ("Y", "S", "C", "F")),
    "SQ": ("Singapore Airlines", ("Y", "S", "C", "F")),
    "TK": ("Turkish Airlines", ("Y", "S", "C")),
    "UA": ("United Airlines", ("Y", "S", "C", "F")),
}

# Currency metadata used when building ResponseParameters / prices.
CURRENCIES: dict[str, int] = {
    "USD": 2,
    "EUR": 2,
    "GBP": 2,
    "AED": 2,
    "SGD": 2,
    "JPY": 0,
    "INR": 2,
}

# Fare basis prefixes per cabin, used to synthesize FareBasisCode values.
FARE_BASIS_PREFIX: dict[str, str] = {
    "Y": "Y",
    "S": "W",
    "C": "C",
    "J": "J",
    "F": "F",
}
