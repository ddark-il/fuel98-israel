#!/usr/bin/env python3
"""
PoC client for Waze's own fuel-price API.

The fuel prices are NOT served by any REST endpoint: the Android app fetches them
inside its native core (libwaze.so) over Waze's binary "rt" protocol and hands the
result to Java as NearbyStationsResultProto. The wire format was recovered from
Waze 5.24.5.0 (com.waze) - see PROTOCOL.md for every field number used below.

Transport (from RealtimeNetDefs.cc / libwaze.so string pool):

    POST https://rt.waze.com/rtserver/distrib?sessionid=<id>&cookie=<cookie>
    body: linqmap.proto.rt.Batch { repeated Element element = 1001; }

Session bootstrap: send Element{client_info = 2184} -> server answers with the
session id / cookie used by every later request. Logged-in features additionally
use Element{login_request = 2744}.

Gas prices are read from venue search results:
    Element{search_v2_request = 2741 | venue_search_request = 2053}
  -> Batch/Element{venue_list = 2049 | search_v2_response = 2742}
  -> Venue3.original_products (138) = repeated ProductPricePair
         key   (600) = product id, e.g. "96"/"98"/"diesel"  (=> fuel type)
         value (601) = ProductPrice { price = 610 (float); updateTime = 611 }

Without official access the distributor refuses the connection (HTTP 403
"FORBIDDEN."), which is exactly what `--probe` demonstrates.

Usage:
    python3 waze_rt_poc.py --probe                    # bootstrap attempt, decoded
    python3 waze_rt_poc.py --lat 32.08 --lon 34.78    # venue search + prices
"""
from __future__ import annotations

import argparse
import json
import struct
import sys
import urllib.error
import urllib.request

# ---------------------------------------------------------------- endpoints
DISTRIB_URLS = [
    "https://rt.waze.com/rtserver/distrib",
    "https://rt.gcp.wazestg.com:443/rtserver/distrib",
]

# ------------------------------------------------- field numbers (extracted)
# linqmap.proto.rt.Batch / Element  (Container.proto)
BATCH_ELEMENT = 1001
EL_CLIENT_INFO = 2184
EL_LOGIN_REQUEST = 2744
EL_LOGIN_RESPONSE = 2745
EL_SEARCH_CONFIG_REQUEST = 2066
EL_SEARCH_CONFIG_RESPONSE = 2065
EL_SEARCH_REQUEST = 2062
EL_SEARCH_RESPONSE = 2063
EL_SEARCH_V2_REQUEST = 2741
EL_SEARCH_V2_RESPONSE = 2742
EL_VENUE_SEARCH_REQUEST = 2053
EL_VENUE_LIST = 2049
EL_VENUE_STATUS_RESPONSE = 2044
EL_ERROR = 2003

# anonymous bootstrap / session (RegisterCommands.proto, LoginCommands.proto, ConfigCommands)
EL_REGISTER = 2219
EL_REGISTER_SUCCESSFUL = 2220
EL_REGISTER_ERROR = 2223
EL_LOGIN = 2222
EL_LOGIN_SUCCESSFUL = 2225
EL_LOGIN_ERROR = 2224
EL_CLIENT_AUTH_TOKEN = 2231
EL_GENERATE_TOKEN = 2232
EL_GET_TOKEN_REQUEST = 2146
EL_GET_TOKEN_RESPONSE = 2145
EL_AUTHENTICATE = 2338

# linqmap.proto.rt.ClientInfo (ClientInfoCommand.proto)
CI = dict(protocol=1, device=2, client_version=3, last_position=4, manufacturer=5,
          model=6, name=7, width=8, height=9, application_type=10, os_version=11,
          applicatin_mode_bitmap=12, return_text=14, simulated=15, locale=16,
          installation_id=17,
          device_type=18, app_type=19, client_api=20, environment=21, user_agent=23,
          os_language_id=25, session_uuid=26, is_jailbroken=27,
          current_time_millis=28, device_brand=29)

# linqmap.proto.Coordinate (Types.proto)
COORD = dict(lon_times_1000000=101, lat_times_1000000=102)

# linqmap.proto.search.v2.SearchRequest / SearchArea / SearchFilter (SearchV2.proto)
S2 = dict(search_area=1, search_filters=2, venue_preference=3, merge_user_updates=4,
          page_size=5, origin=6, search_ads_details=7)
S2_AREA = dict(session_based_geometry=1, coordinate=2, route=3, max_distance_meters=4,
               polygon=6, venue_location=7, viewport=8)
S2_FILTER = dict(query=1, category_group_id=2, category_filter=3,
                 advertiser_brand_filter=4, service_filter=5, evcs_network_filter=6,
                 ev_connector_type_filter=7, min_charge_power_kw_filter=8,
                 price_level_filter=9, brand_filter=10, product_filter=11,
                 open_now_filter=12, min_rating_filter=13)

# linqmap.proto.venue.VenueSearchRequest (Venues.proto)
VSR = dict(session=1, geometry=2, category=3, product_type=4, brands=5, providers=6,
           products=7, along_line=8, near_by=9, sort_by=10, sort_order=11,
           distribute=12, max_age=13, max_results=14, approved_only=15,
           residential_too=16, unlisted_too=17, minimal_filtering=18, minimal_area=19,
           version=20, thumbs_only=21, user_info=22, merge_user_updates=23,
           protocol=24)

# linqmap.proto.venue.Venue / Venue3 (Venues.proto)
V_NAME = 104
V_BRAND = 125
V_CURRENCY = 149
V_PRICE_TYPE = 112
V_PRODUCT = 150                 # repeated Product{id=1, last_updated=2, updated_by=3, price=4}
PROD_ID = 1
PROD_UPDATED = 2
PROD_UPDATED_BY = 3
PROD_PRICE = 4
V3_NAME = 104
V3_BRAND = 125
V3_ORIGINAL_PRODUCTS = 138      # repeated ProductPricePair  <- community prices
V3_CHANGED_PRODUCTS = 139
V3_CURRENCY = 178

# linqmap.proto.rt.RegisterSuccessful (RegisterCommands.proto)
RS_USERNAME, RS_PASSWORD, RS_TOKEN, RS_USER_ID = 1, 2, 3, 4

PP_PAIR_KEY = 600               # product id (fuel type)
PP_PAIR_VALUE = 601             # ProductPrice
PP_PRICE = 610                  # float
PP_UPDATE_TIME = 611            # int64 (unix seconds)


# ------------------------------------------------------------- protobuf mini
def varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def tag(field: int, wire: int) -> bytes:
    return varint((field << 3) | wire)


def pb_int(field: int, value: int) -> bytes:
    return tag(field, 0) + varint(value)


def pb_bool(field: int, value: bool) -> bytes:
    return pb_int(field, 1 if value else 0)


def pb_str(field: int, value: str) -> bytes:
    b = value.encode("utf-8")
    return tag(field, 2) + varint(len(b)) + b


def pb_bytes(field: int, value: bytes) -> bytes:
    return tag(field, 2) + varint(len(value)) + value


def pb_float(field: int, value: float) -> bytes:
    return tag(field, 5) + struct.pack("<f", value)


def pb_double(field: int, value: float) -> bytes:
    return tag(field, 1) + struct.pack("<d", value)


def read_varint(buf: bytes, i: int):
    shift = val = 0
    while True:
        b = buf[i]
        i += 1
        val |= (b & 0x7F) << shift
        if not b & 0x80:
            return val, i
        shift += 7


def pb_decode(buf: bytes) -> dict:
    """field number -> list of (wire, value); value is int | bytes."""
    out: dict[int, list] = {}
    i = 0
    while i < len(buf):
        key, i = read_varint(buf, i)
        field, wire = key >> 3, key & 7
        if wire == 0:
            v, i = read_varint(buf, i)
        elif wire == 2:
            ln, i = read_varint(buf, i)
            v, i = buf[i:i + ln], i + ln
        elif wire == 5:
            v, i = buf[i:i + 4], i + 4
        elif wire == 1:
            v, i = buf[i:i + 8], i + 8
        else:
            break
        out.setdefault(field, []).append((wire, v))
    return out


def as_str(v) -> str:
    return v.decode("utf-8", "replace") if isinstance(v, bytes) else str(v)


def as_float(v) -> float:
    return struct.unpack("<f", v)[0] if isinstance(v, bytes) else float(v)


# ------------------------------------------------------------ request build
def coord(lat: float, lon: float) -> bytes:
    return (pb_int(COORD["lon_times_1000000"], int(round(lon * 1_000_000)))
            + pb_int(COORD["lat_times_1000000"], int(round(lat * 1_000_000))))


def client_info(installation_id: str, device_id: str) -> bytes:
    """linqmap.proto.rt.ClientInfo - the mandatory first packet of a session."""
    msg = pb_int(CI["protocol"], 4)
    msg += pb_str(CI["device"], device_id)
    msg += pb_str(CI["client_version"], "5.24.5.0")
    msg += pb_str(CI["manufacturer"], "Google")
    msg += pb_str(CI["model"], "sdk_gphone64_arm64")
    msg += pb_int(CI["width"], 1080)
    msg += pb_int(CI["height"], 2340)
    msg += pb_int(CI["application_type"], 1)
    msg += pb_str(CI["os_version"], "13")
    msg += pb_str(CI["locale"], "he_IL")
    msg += pb_str(CI["installation_id"], installation_id)
    msg += pb_str(CI["os_language_id"], "iw")
    msg += pb_str(CI["session_uuid"], installation_id)
    msg += pb_bool(CI["return_text"], True)
    msg += pb_bool(CI["simulated"], False)
    msg += pb_int(CI["current_time_millis"], 0)
    msg += pb_str(CI["user_agent"], UA)
    msg += pb_str(CI["device_brand"], "google")
    return msg


def search_v2(lat: float, lon: float, query: str = "", page_size: int = 25) -> bytes:
    """linqmap.proto.search.v2.SearchRequest - modern venue search entry point."""
    area = pb_bytes(S2_AREA["coordinate"], coord(lat, lon))
    flt = b""
    if query:
        flt += pb_str(S2_FILTER["query"], query)
    req = pb_bytes(S2["search_area"], area)
    if flt:
        req += pb_bytes(S2["search_filters"], flt)
    req += pb_int(S2["page_size"], page_size)
    return req


def venue_search(lat: float, lon: float, category: str = "GAS_STATION",
                 max_results: int = 25, products: tuple = (), sort_by_price: bool = False) -> bytes:
    """linqmap.proto.venue.VenueSearchRequest - the *gas layer* search (Element 2053 -> VenueList).

    Returns gas stations as `Venue3` records, each with `original_products` (138) - the community
    price list. One request, every station around a point, prices included: this is what the app's
    "gas stations" list is built from, and it is a different path from a per-venue GetRequest.

    The coordinate type is a trap: `linqmap.proto.venue.Coordinate` is **double degrees** at fields
    300/301, while the RT `linqmap.proto.Coordinate` elsewhere in this file is int degrees*1e6. This
    function used to send varints, which is why element 2053 answered
    `500 UninitializedMessageException` and the whole legacy search was written off as unusable.
    """
    req = pb_bytes(VSR["near_by"], pb_double(300, lon) + pb_double(301, lat))
    if category:
        req += pb_str(VSR["category"], category)
    for prod in products:
        req += pb_str(VSR["products"], prod)
    if sort_by_price:
        req += pb_int(VSR["sort_by"], 1)          # SortBy.PRICE
    req += pb_int(VSR["max_results"], max_results)
    req += pb_int(VSR["protocol"], 3)
    return req


def element(**kwargs) -> bytes:
    """Element{ <field_number>: <serialized_message> } - see PROTOCOL.md."""
    out = b""
    for field, payload in kwargs.items():
        out += pb_bytes(int(field), payload)
    return out


def register_payload(code: str = "") -> bytes:
    """linqmap.proto.rt.Register - the anonymous first-launch registration."""
    return pb_str(1, code) if code else b""


def generate_token_payload() -> bytes:
    """linqmap.proto.rt.GenerateToken - empty request."""
    return b""


def search_config_request(country_code: str = "IL") -> bytes:
    """linqmap.proto.search_config.SearchConfigRequest - carries the fuel-type catalog."""
    return pb_str(3, country_code)      # user_country_code = 3


def bootstrap_batches(installation_id: str, device_id: str) -> tuple[bytes, bytes]:
    """(no-session bootstrap, follow-up token request) for the anonymous client."""
    first = batch(element(**{str(EL_CLIENT_INFO): client_info(installation_id, device_id)}),
                  element(**{str(EL_REGISTER): register_payload()}))
    second = batch(element(**{str(EL_GENERATE_TOKEN): generate_token_payload()}))
    return first, second


def batch(*elements: bytes) -> bytes:
    return b"".join(pb_bytes(BATCH_ELEMENT, e) for e in elements)


# ------------------------------------------------------------ response parse
def parse_products(pairs: dict) -> dict:
    """ProductPricePair{600:key, 601:ProductPrice{610:price,611:updateTime}} -> dict"""
    prices = {}
    key = None
    for wire, val in pairs.get(PP_PAIR_KEY, []):
        key = as_str(val)
    for wire, val in pairs.get(PP_PAIR_VALUE, []):
        inner = pb_decode(val)
        price = inner.get(PP_PRICE, [(0, None)])[0][1]
        upd = inner.get(PP_UPDATE_TIME, [(0, None)])[0][1]
        if key is not None:
            prices[key] = {"price": as_float(price) if price is not None else None,
                           "updated": upd}
    return prices


def parse_venue(blob: bytes) -> dict:
    f = pb_decode(blob)
    name = as_str(f[V3_NAME][0][1]) if V3_NAME in f else (
        as_str(f[V_NAME][0][1]) if V_NAME in f else None)
    brand = as_str(f[V3_BRAND][0][1]) if V3_BRAND in f else (
        as_str(f[V_BRAND][0][1]) if V_BRAND in f else None)
    prices = {}
    for wire, val in f.get(V3_ORIGINAL_PRODUCTS, []):
        prices.update(parse_products(pb_decode(val)))
    # Venue.product (150) = provider/brand-sourced prices, a second, separate list
    provider_prices = {}
    for wire, val in f.get(V_PRODUCT, []):
        p = pb_decode(val)
        pid = as_str(p[PROD_ID][0][1]) if PROD_ID in p else None
        if pid is None:
            continue
        provider_prices[pid] = {
            "price": as_float(p[PROD_PRICE][0][1]) if PROD_PRICE in p else None,
            "updated": p[PROD_UPDATED][0][1] if PROD_UPDATED in p else None,
            "by": as_str(p[PROD_UPDATED_BY][0][1]) if PROD_UPDATED_BY in p else None,
        }
    curr = f.get(V3_CURRENCY) or f.get(V_CURRENCY)
    return {"name": name, "brand": brand, "prices": prices,
            "provider_prices": provider_prices,
            "currency": as_str(curr[0][1]) if curr else None,
            "changed": len(f.get(V3_CHANGED_PRODUCTS, []))}


def parse_response(body: bytes) -> dict:
    """Batch{element=1001} -> {client_info, session, venues, catalog, errors}"""
    result = {"venues": [], "errors": [], "client_info_seen": False, "session": None,
              "registered": None, "auth_token": None, "catalog": []}
    for wire, el in pb_decode(body).get(BATCH_ELEMENT, []):
        e = pb_decode(el)
        if EL_CLIENT_INFO in e:
            result["client_info_seen"] = True
        if EL_ERROR in e:
            for _, v in e[EL_ERROR]:
                result["errors"].append({as_str(k): as_str(vv) for k, vv in pb_decode(v).items()})
        if EL_REGISTER_SUCCESSFUL in e:
            for _, v in e[EL_REGISTER_SUCCESSFUL]:
                r = pb_decode(v)
                result["registered"] = {
                    "username": as_str(r[RS_USERNAME][0][1]) if RS_USERNAME in r else None,
                    "password": as_str(r[RS_PASSWORD][0][1]) if RS_PASSWORD in r else None,
                    "token": as_str(r[RS_TOKEN][0][1]) if RS_TOKEN in r else None,
                    "user_id": r[RS_USER_ID][0][1] if RS_USER_ID in r else None}
        if EL_REGISTER_ERROR in e:
            for _, v in e[EL_REGISTER_ERROR]:
                result["errors"].append({"register_error": as_str(vv)
                                         for _, vv in pb_decode(v).values()})
        if EL_CLIENT_AUTH_TOKEN in e:
            for _, v in e[EL_CLIENT_AUTH_TOKEN]:
                inner = pb_decode(v)
                if 1 in inner:
                    result["auth_token"] = as_str(inner[1][0][1])
                    result["session"] = result["auth_token"]
        if EL_SEARCH_CONFIG_RESPONSE in e:
            for _, v in e[EL_SEARCH_CONFIG_RESPONSE]:
                result["catalog"].extend(parse_catalog(pb_decode(v)))
        if EL_VENUE_LIST in e:
            for _, v in e[EL_VENUE_LIST]:
                inner = pb_decode(v)
                for _, vb in inner.get(1, []):        # VenueList.venue  -> Venue
                    result["venues"].append(parse_venue(vb))
                for _, vb in inner.get(2, []):        # VenueList.venue3 -> Venue3
                    result["venues"].append(parse_venue(vb))
        for field in (EL_SEARCH_V2_RESPONSE, EL_SEARCH_RESPONSE):
            for _, v in e.get(field, []):
                inner = pb_decode(v)
                for _, r in inner.get(1, []):        # SearchResponse.result -> SearchResult
                    sub = pb_decode(r)
                    for key in (1, 3):               # v2 SearchResult.venue=1, legacy=3
                        for _, vb in sub.get(key, []):
                            result["venues"].append(parse_venue(vb))
    return result


# ------------------------------------------------------------------ network
UA = ("Waze/5.24.5.0 (com.waze; Android 13; he_IL) "
      "Mozilla/5.0 (Linux; Android 13; sdk_gphone64_arm64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36")


def send(payload: bytes, sessionid: int = -1, cookie: str = "",
         base: str = DISTRIB_URLS[0], timeout: int = 20, verbose: bool = False):
    url = f"{base}?sessionid={sessionid}&cookie={cookie}"
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/x-protobuf")
    req.add_header("User-Agent", UA)
    req.add_header("Accept-Encoding", "identity")
    if verbose:
        print(f"-> POST {url}\n-> body {len(payload)} bytes: {payload.hex()}", file=sys.stderr)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)
    except Exception as e:                                   # noqa: BLE001
        return None, repr(e).encode(), {}


def parse_catalog(cfg: dict) -> list:
    """SearchConfigResponse{1003 category = Category{2001 id, 2002 name, 2005 product}}
    -> [{'category':..,'product_id':..,'name':..,'type':..}]  (the fuel-type catalog)"""
    out = []
    for _, cat in cfg.get(1003, []):
        c = pb_decode(cat)
        cid = as_str(c[2001][0][1]) if 2001 in c else None
        cname = as_str(c[2002][0][1]) if 2002 in c else None
        for _, prod in c.get(2005, []):
            p = pb_decode(prod)
            out.append({"category_id": cid, "category": cname,
                        "product_id": as_str(p[5001][0][1]) if 5001 in p else None,
                        "name": as_str(p[5002][0][1]) if 5002 in p else None,
                        "type": as_str(p[5004][0][1]) if 5004 in p else None})
    return out


def selftest() -> int:
    """Prove the read path end-to-end on a synthetic payload (no network involved).

    This is the honest state of the PoC: the *decoder* is finished and verified; the
    *transport* is refused by Waze's edge (403), so no real bytes have ever been read.
    """
    def pair(key, price, ts):
        return (pb_bytes(PP_PAIR_KEY, key.encode())
                + pb_bytes(PP_PAIR_VALUE, pb_float(PP_PRICE, price) + pb_int(PP_UPDATE_TIME, ts)))

    station = (pb_str(V3_NAME, 'סונול בית שקמה') + pb_str(V3_BRAND, 'סונול')
               + pb_str(V3_CURRENCY, 'ILS')
               + pb_bytes(V3_ORIGINAL_PRODUCTS, pair('95', 8.34, 1750000000))
               + pb_bytes(V3_ORIGINAL_PRODUCTS, pair('98', 10.64, 1750000000))
               + pb_bytes(V3_CHANGED_PRODUCTS, pair('98', 10.90, 1750001000)))
    provider = (pb_str(V_NAME, 'פז גלילות') + pb_str(V_BRAND, 'פז') + pb_str(V_CURRENCY, 'ILS')
                + pb_bytes(V_PRODUCT, pb_str(PROD_ID, '98') + pb_float(PROD_PRICE, 10.79)
                           + pb_int(PROD_UPDATED, 1750002000) + pb_str(PROD_UPDATED_BY, 'brand-feed')))
    catalog = pb_bytes(1003,
        pb_str(2001, 'gas') + pb_str(2002, 'Gas station')
        + pb_bytes(2005, pb_str(5001, '95') + pb_str(5002, 'בנזין 95') + pb_str(5004, 'fuel'))
        + pb_bytes(2005, pb_str(5001, '98') + pb_str(5002, 'בנזין 98') + pb_str(5004, 'fuel')))

    body = batch(
        element(**{str(EL_VENUE_LIST): pb_bytes(2, station) + pb_bytes(1, provider)}),
        element(**{str(EL_SEARCH_CONFIG_RESPONSE): catalog}),
        element(**{str(EL_REGISTER_SUCCESSFUL): pb_str(RS_USERNAME, 'anonymous-abc')
                   + pb_str(RS_PASSWORD, '•••') + pb_str(RS_TOKEN, 'tok')
                   + pb_int(RS_USER_ID, 123456)}))
    out = parse_response(body)
    print('anonymous registration :', out['registered'])
    print('fuel-type catalog      :', [(p['product_id'], p['name']) for p in out['catalog']])
    for v in out['venues']:
        print(f"venue                  : {v['brand']} / {v['name']} ({v['currency']})")
        for pid, p in v['prices'].items():
            print(f"    community {pid:<4} {p['price']:.2f}  ts={p['updated']}   pending_edits={v['changed']}")
        for pid, p in v['provider_prices'].items():
            print(f"    provider  {pid:<4} {p['price']:.2f}  ts={p['updated']} by={p['by']}")
    print('\nOK: request builders and decoders are exercised end-to-end.')
    print('NOT OK: this used synthetic bytes. Live reads return 403 FORBIDDEN.')
    print('        see FINDINGS.md blocker 2 / ANON_SESSION.md.')
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Waze fuel-price API PoC")
    ap.add_argument("--probe", action="store_true", help="only try the session bootstrap")
    ap.add_argument("--register", action="store_true",
                    help="run the anonymous ClientInfo+Register+GenerateToken sequence")
    ap.add_argument("--dump-products", action="store_true",
                    help="print every product id seen per venue (look for the 98 id)")
    ap.add_argument("--selftest", action="store_true",
                    help="decode a synthetic payload: proves the decoder, touches no network")
    ap.add_argument("--lat", type=float, default=32.0853)
    ap.add_argument("--lon", type=float, default=34.7818)
    ap.add_argument("--installation-id", default="00000000-0000-4000-8000-000000000000")
    ap.add_argument("--device-id", default="0000000000000000")
    ap.add_argument("--base", default=DISTRIB_URLS[0])
    ap.add_argument("--raw-out", default="poc_raw_response.bin")
    a = ap.parse_args()

    if a.selftest:
        return selftest()

    print(f"Waze rt fuel-price PoC - target {a.base}")
    first, second = bootstrap_batches(a.installation_id, a.device_id)
    payload = first if a.register else batch(
        element(**{str(EL_CLIENT_INFO): client_info(a.installation_id, a.device_id)}))
    status, body, headers = send(payload, base=a.base, verbose=True)
    print(f"bootstrap: HTTP {status}, {len(body)} bytes, headers {list(headers)[:6]}")
    if body:
        print("bootstrap body:", body[:160])
    open(a.raw_out, "wb").write(body)

    if status != 200 or not body:
        print("\n== no usable session: the distributor refused the request ==")
        print("All paths under rt.waze.com answer 403 FORBIDDEN., before any protobuf is")
        print("parsed - see FINDINGS.md (blocker 2) and ANON_SESSION.md. The request")
        print("below is the real wire format ("
              f"ClientInfo element {EL_CLIENT_INFO} + Register element {EL_REGISTER}).")
        print("Next step is an on-device capture or the official partner endpoint.")
        return 1

    decoded = parse_response(body)
    print("decoded bootstrap:", json.dumps(decoded, ensure_ascii=False)[:600])

    if a.register and decoded.get("registered"):
        print("\nanonymous account:", decoded["registered"])
        status, body, _ = send(second, base=a.base, verbose=True)
        print(f"generate_token: HTTP {status}, {len(body)} bytes")
        tok = parse_response(body)
        print("client_auth_token:", tok.get("auth_token"))

    if a.probe:
        return 0

    print(f"\nvenue search around {a.lat},{a.lon}")
    s2 = batch(element(**{str(EL_SEARCH_V2_REQUEST): search_v2(a.lat, a.lon)}),
               element(**{str(EL_VENUE_SEARCH_REQUEST): venue_search(a.lat, a.lon)}),
               element(**{str(EL_SEARCH_CONFIG_REQUEST): search_config_request("IL")}))
    status, body, _ = send(s2, base=a.base, verbose=True)
    print(f"search: HTTP {status}, {len(body)} bytes")
    res = parse_response(body)
    if res["catalog"]:
        print(f"fuel-type catalog ({len(res['catalog'])} entries):")
        for p in res["catalog"]:
            print(f"   {p['category']}: id={p['product_id']!r} name={p['name']!r} type={p['type']!r}")
    for v in res["venues"]:
        print(f"  {v['brand'] or '-':<12} {v['name']}  currency={v['currency']}")
        if a.dump_products:
            for pid, p in v["prices"].items():
                print(f"      community key={pid!r} price={p['price']} updated={p['updated']}")
            for pid, p in v["provider_prices"].items():
                print(f"      provider  key={pid!r} price={p['price']} updated={p['updated']}")
        else:
            print(f"      prices={v['prices']}")
    if not res["venues"]:
        print("  (no venues decoded)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
